"""Exercise bounded domain registration and durable recovery through real process boundaries."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
import sys
import threading

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream
from domain_fake import SCRIPT, domain_cli as domain_cli
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from test_domain_registry import argv

from marabrain.domain_registry import DomainRegistry
from marabrain.memory_tools import register_memory_tools


async def test_registration_commands_share_one_operation_budget(domain_cli: Path) -> None:
    """Consume one cumulative budget across listing, initialization, and registration before any write."""
    (domain_cli / "crystalline").write_text(SCRIPT.replace("import json, os, pathlib, sys", "import json, os, pathlib, sys, time\ntime.sleep(0.4)"))
    server = FastMCP("bounded")
    register_memory_tools(server, command_timeout_seconds=5, operation_timeout_seconds=1)
    async with Client(server) as client:
        with pytest.raises(ToolError, match="may or may not have been applied"):
            await client.call_tool("create_memory", {"domain": "new", "title": "note", "content": "body"})
    assert all(arguments[1] != "write" for arguments in argv(domain_cli))
    assert not (DomainRegistry.from_environment(5).storage.root / "domains/new/note.md").exists()


@pytest.mark.parametrize("interrupt", ["timeout", "cancel"])
async def test_uncertain_registration_invalidates_durably_and_recovers_after_restart(domain_cli: Path, interrupt: str) -> None:
    """Reconcile cancelled or expired registration before trusting cached routes or serving a restarted process."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    gated = SCRIPT.replace("import json, os, pathlib, sys", "import json, os, pathlib, sys, socket")
    gated = gated.replace('domains.append({"name": name, "path": str(path)})', 'domains.append({"name": name, "path": str(path), "aliases": ["shortcut"]})')
    gated = gated.replace(
        '    if mode == "add-uncertain":',
        f"    connection = socket.create_connection(('127.0.0.1', {port}))\n"
        "    connection.sendall(str(os.getpid()).encode())\n"
        "    connection.recv(1)\n"
        '    if mode == "add-uncertain":',
    )
    (domain_cli / "crystalline").write_text(gated)
    server = FastMCP("registration")
    register_memory_tools(server, operation_timeout_seconds=2 if interrupt == "timeout" else None)
    send, receive = anyio.create_memory_object_stream[int](1)
    completed = anyio.Event()
    scope = anyio.CancelScope()
    pid = 0

    async def started(connection: SocketStream) -> None:
        """Report when the engine has persisted its uncertain registration."""
        async with connection:
            await send.send(int(await connection.receive()))
            await anyio.sleep_forever()

    async with send, receive, listener, Client(server) as client:

        async def create() -> None:
            """Finish the original request only after deadline or cancellation cleanup."""
            try:
                with scope:
                    if interrupt == "timeout":
                        with pytest.raises(ToolError, match="may or may not have been applied"):
                            await client.call_tool("create_memory", {"domain": "new", "title": "note", "content": "body"})
                    else:
                        await client.call_tool("create_memory", {"domain": "new", "title": "note", "content": "body"})
            finally:
                completed.set()

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(listener.serve, started)
                tasks.start_soon(create)
                pid = await receive.receive()
                if interrupt == "cancel":
                    scope.cancel()
                await completed.wait()
                tasks.cancel_scope.cancel()

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    registry = DomainRegistry.from_environment(5)
    with closing(sqlite3.connect(registry.storage.root / "domains.sqlite3")) as database:
        assert database.execute("SELECT value FROM metadata WHERE key = 'synchronized'").fetchone() is None
    assert all(arguments[1] != "write" for arguments in argv(domain_cli))
    assert not (registry.storage.root / "domains/new/note.md").exists()
    assert await registry.resolve(["SHORTCUT"]) == ["new"]
    transport = StdioTransport(sys.executable, ["-m", "marabrain"], env={**os.environ, "MARABRAIN_ENGINE_MODE": "external", "MARABRAIN_TRANSPORT": "stdio"})
    async with Client(transport) as restarted:
        response = await restarted.call_tool("search_memories", {"query": "body", "domains": ["Shortcut"]})
    assert response.structured_content == {"arguments": ["search", "--domain=new", "--limit=10", "--page=1", "body"]}
    assert sum(arguments[:3] == ["--json", "domain", "add"] for arguments in argv(domain_cli)) == 1


async def test_cancelled_connection_closes_before_registry_lock_release(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain ownership until a cancelled SQLite connector finishes and its connection is closed."""
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = sqlite3.connect

    class RecordedConnection(sqlite3.Connection):
        """Observe actual connection closure across cancellation during acquisition."""

        def close(self) -> None:
            """Close the actual SQLite handle before reporting completion."""
            super().close()
            closed.set()

    def gated_connect(database: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
        """Block a real connector after acquisition until the request has been cancelled."""
        connection = original(database, check_same_thread=check_same_thread, factory=RecordedConnection)
        started.set()
        release.wait()
        return connection

    monkeypatch.setattr("marabrain.domain_registry.sqlite3.connect", gated_connect)
    registry = DomainRegistry.from_environment(5)
    scope = anyio.CancelScope()
    completed = anyio.Event()

    async def resolve() -> None:
        """Cancel while the SQLite worker is acquiring its real connection."""
        try:
            with scope:
                await registry.resolve(["notes"])
        finally:
            completed.set()

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(resolve)
            try:
                await anyio.to_thread.run_sync(started.wait)
                scope.cancel()
                await anyio.lowlevel.checkpoint()
                assert not completed.is_set()
            finally:
                release.set()
            await completed.wait()
    assert closed.is_set()
    async with registry.storage.locked():
        assert completed.is_set()
