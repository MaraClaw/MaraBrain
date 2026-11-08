"""End-to-end cancellation tests for memory command subprocesses."""

import os
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from marabrain.config import Settings
from marabrain.crystalline import run_json
from marabrain.domain_registry import DomainRegistry
from marabrain.memory_tools import register_memory_tools
from marabrain.server import create_server
from memory_fake import calls, respond


async def test_mcp_cancellation_reaps_running_command(cli: Path) -> None:
    """Cancel an MCP request after its child starts and verify the child is reaped."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    executable = cli / "crystalline"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, socket, time\n"
        f"s = socket.create_connection(('127.0.0.1', {port}))\n"
        "s.sendall(str(os.getpid()).encode())\n"
        "s.recv(1)\n"
        "time.sleep(60)\n"
    )
    server = FastMCP("test")
    register_memory_tools(server)
    send, receive = anyio.create_memory_object_stream[int](1)

    async def started(connection: SocketStream) -> None:
        """Report the child PID and keep its connection open until cancellation."""
        async with connection:
            await send.send(int(await connection.receive()))
            await anyio.sleep_forever()

    pid = 0
    async with send, receive, listener, Client(server) as client:

        async def request() -> None:
            """Run the request until its cancel scope is cancelled."""
            await client.call_tool("read_memory", {"domain": "notes", "permalink": "team/plan"})

        with anyio.fail_after(15):
            async with anyio.create_task_group() as group:
                group.start_soon(listener.serve, started)
                group.start_soon(request)
                pid = await receive.receive()
                group.cancel_scope.cancel()

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_concurrent_servers_use_independent_command_deadlines(cli: Path) -> None:
    """Allow a delayed command on one server while another server times out."""
    (cli / "crystalline").write_text(f"#!{sys.executable}\nimport time\ntime.sleep(0.5)\nprint('{{\"ok\": true}}')\n")
    fast = create_server(Settings(command_timeout_seconds=0.2))
    slow = create_server(Settings(command_timeout_seconds=5.0))

    async def request(server: FastMCP, succeeds: bool) -> None:
        """Verify the request result under one captured deadline."""
        async with Client(server) as client:
            if succeeds:
                assert (await client.call_tool("read_memory", {"domain": "notes", "permalink": "plan"})).structured_content == {"ok": True}
            else:
                with pytest.raises(ToolError, match="may or may not have been applied"):
                    await client.call_tool("read_memory", {"domain": "notes", "permalink": "plan"})

    with anyio.fail_after(15):
        async with anyio.create_task_group() as group:
            group.start_soon(request, fast, False)
            group.start_soon(request, slow, True)


async def test_expired_shared_deadline_refuses_cli_dispatch(cli: Path) -> None:
    """Reject an already expired operation before launching any Crystalline command."""
    with pytest.raises(ToolError, match="may or may not have been applied"):
        await run_json(["delete", "note", "notes", "--force"], deadline=anyio.current_time() - 1)
    assert calls(cli) == []


@pytest.mark.parametrize("command_cap,operation_cap", [(0.5, 5.0), (5.0, 0.5)])
async def test_cli_obeys_earlier_command_or_shared_deadline_and_reaps_child(cli: Path, command_cap: float, operation_cap: float) -> None:
    """Stop and reap a hanging CLI at the earlier command or operation cap without retrying."""
    respond(cli, hang=True)
    with anyio.fail_after(2.5):
        with pytest.raises(ToolError, match="may or may not have been applied"):
            await run_json(["read", "note", "--domain=notes"], timeout_seconds=command_cap, deadline=anyio.current_time() + operation_cap)
    [record] = calls(cli)
    pid = record["pid"]
    assert isinstance(pid, int)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("create_memory", {"domain": "new", "title": "note", "content": "body"}),
        ("read_memory", {"domain": "notes", "permalink": "note"}),
        ("edit_memory", {"domain": "notes", "permalink": "note", "operation": {"type": "append", "content": "body"}}),
        ("delete_memory", {"domain": "notes", "permalink": "note"}),
        ("search_memories", {"domains": ["notes"], "query": "body"}),
    ],
)
async def test_operation_budget_includes_registry_lock_wait(cli: Path, tool: str, arguments: dict[str, object]) -> None:
    """Expire each MCP operation while waiting for the registry without dispatching a command."""
    registry = DomainRegistry.from_environment(5)
    server = FastMCP("bounded")
    register_memory_tools(server, operation_timeout_seconds=0.1)
    async with registry.storage.locked(), Client(server) as client:
        with pytest.raises(ToolError, match="operation deadline expired"):
            await client.call_tool(tool, arguments)
    assert calls(cli) == []


async def test_concurrent_servers_keep_independent_operation_budgets(cli: Path) -> None:
    """Expire one operation while a concurrent server completes under its own longer budget."""
    (cli / "crystalline").write_text(f"#!{sys.executable}\nimport time\ntime.sleep(0.3)\nprint('{{\"ok\": true}}')\n")
    fast, slow = FastMCP("fast"), FastMCP("slow")
    register_memory_tools(fast, operation_timeout_seconds=0.1)
    register_memory_tools(slow, operation_timeout_seconds=5)

    async def request(server: FastMCP, succeeds: bool) -> None:
        """Check each response against the server's operation budget."""
        async with Client(server) as client:
            if succeeds:
                assert (await client.call_tool("search_memories", {"query": "body", "domains": ["notes"]})).structured_content == {"ok": True}
            else:
                with pytest.raises(ToolError, match="may or may not have been applied"):
                    await client.call_tool("search_memories", {"query": "body", "domains": ["notes"]})

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(request, fast, False)
        tasks.start_soon(request, slow, True)


async def test_cancellation_during_launch_reaps_child_before_return(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancel after OS creation but before the launch call returns without orphaning the child."""
    launched, release = threading.Event(), threading.Event()
    pids: list[int] = []
    original = subprocess.Popen

    class GatedLaunch(original[bytes]):
        """Pause delivery of a real child handle to exercise cancellation during launch."""

        def __init__(self, args: Sequence[str], *, stdin: int, stdout: int, stderr: int, env: Mapping[str, str]) -> None:
            """Create the real CLI child and hold its handle until cancellation is pending."""
            super().__init__(args, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
            pids.append(self.pid)
            launched.set()
            release.wait()

    (cli / "crystalline").write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    monkeypatch.setattr("marabrain.crystalline.subprocess.Popen", GatedLaunch)
    scope = anyio.CancelScope()

    async def request() -> None:
        """Run one command inside the externally cancellable request scope."""
        with scope:
            await run_json(["read", "note", "--domain=notes"])

    try:
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(request)
                await anyio.to_thread.run_sync(launched.wait)
                scope.cancel()
                await anyio.lowlevel.checkpoint()
                release.set()
    finally:
        release.set()
    [pid] = pids
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
