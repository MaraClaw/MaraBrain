"""End-to-end cancellation tests for memory command subprocesses."""

import json
import os
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError

from marabrain.config import Settings
from marabrain.crystalline import run_json
from marabrain.engine_context import EngineContext
from marabrain.llm_config import LLMSettings
from marabrain.server import create_server
from memory_fake import RECEIPT, WORKSPACE_ID, calls, external_cli, respond, workspace_registry


async def test_mcp_cancellation_reaps_running_command(cli: Path) -> None:
    """Cancel an MCP request after its child starts and verify the child is reaped."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    executable = cli / "crystalline"
    context = EngineContext.capture(max_transient_processes=2, max_capture_bytes=1024)
    server = create_server(Settings(), context=context)
    send, receive = anyio.create_memory_object_stream[int](1)

    async def started(connection: SocketStream) -> None:
        """Report the child PID and keep its connection open until cancellation."""
        async with connection:
            await send.send(int(await connection.receive()))
            await anyio.sleep_forever()

    pid = 0
    async with send, receive, listener, Client(server) as client:
        await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "ready", "domains": ["notes"]})
        executable.write_text(
            external_cli(
                f"import os, socket, time\ns = socket.create_connection(('127.0.0.1', {port}))\n"
                "s.sendall(str(os.getpid()).encode())\ns.recv(1)\ntime.sleep(60)\n"
            )
        )

        async def request() -> None:
            """Run the request until its cancel scope is cancelled."""
            await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "team/plan"})

        with anyio.fail_after(15):
            async with anyio.create_task_group() as group:
                group.start_soon(listener.serve, started)
                group.start_soon(request)
                pid = await receive.receive()
                group.cancel_scope.cancel()

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not [worker for worker in threading.enumerate() if worker.name == "marabrain-cli"]
    executable.write_text(f"#!{sys.executable}\nprint('{{}}')\n")
    assert await run_json(["read"], context=context) == {}


async def test_concurrent_servers_use_independent_command_deadlines(cli: Path) -> None:
    """Allow a delayed command on one server while another server times out."""
    fast = create_server(Settings(command_timeout_seconds=0.2))
    slow = create_server(Settings(command_timeout_seconds=5.0))

    async def request(client: Client[FastMCPTransport], succeeds: bool) -> None:
        """Verify the request result under one captured deadline."""
        if succeeds:
            assert (await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "plan"})).structured_content == {
                "ok": True
            }
        else:
            with pytest.raises(ToolError, match="may or may not have been applied"):
                await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "plan"})

    with anyio.fail_after(15):
        async with Client(fast) as fast_client, Client(slow) as slow_client:
            for client in (fast_client, slow_client):
                await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "ready", "domains": ["notes"]})
            (cli / "crystalline").write_text(external_cli("import time\ntime.sleep(0.5)\nprint('{\"ok\": true}')\n"))
            async with anyio.create_task_group() as group:
                group.start_soon(request, fast_client, False)
                group.start_soon(request, slow_client, True)


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
        ("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "new", "title": "note", "content": "body"}),
        ("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "note"}),
        ("edit_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "note", "operation": {"type": "append", "content": "body"}}),
        ("delete_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "note"}),
        ("search_memories", {"workspace_id": WORKSPACE_ID, "domains": ["notes"], "query": "body"}),
    ],
)
async def test_operation_budget_includes_registry_lock_wait(cli: Path, tool: str, arguments: dict[str, object]) -> None:
    """Expire each MCP operation while waiting for the registry without dispatching a command."""
    registry = workspace_registry()
    server = create_server(Settings(operation_timeout_seconds=0.1))
    async with Client(server) as client:
        await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "ready", "domains": ["notes"]})
        (cli / "calls.jsonl").unlink()
        async with registry.storage.locked():
            with pytest.raises(ToolError, match="operation deadline expired"):
                await client.call_tool(tool, arguments)
    assert calls(cli) == []


async def test_concurrent_servers_keep_independent_operation_budgets(cli: Path) -> None:
    """Expire one operation while a concurrent server completes under its own longer budget."""
    fast = create_server(Settings(operation_timeout_seconds=0.1))
    slow = create_server(Settings(operation_timeout_seconds=5))

    async def request(client: Client[FastMCPTransport], succeeds: bool) -> None:
        """Check each response against the server's operation budget."""
        if succeeds:
            assert (await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "body", "domains": ["notes"]})).structured_content == {
                "ok": True
            }
        else:
            with pytest.raises(ToolError, match="may or may not have been applied"):
                await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "body", "domains": ["notes"]})

    async with Client(fast) as fast_client, Client(slow) as slow_client:
        for client in (fast_client, slow_client):
            await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "ready", "domains": ["notes"]})
        (cli / "crystalline").write_text(external_cli("import time\ntime.sleep(0.3)\nprint('{\"ok\": true}')\n"))
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(request, fast_client, False)
            tasks.start_soon(request, slow_client, True)


@pytest.mark.parametrize("limited_by", ["global", "domain"])
async def test_cancellation_during_launch_reaps_child_before_return(cli: Path, monkeypatch: pytest.MonkeyPatch, limited_by: str) -> None:
    """Cancel after OS creation but before the launch call returns without orphaning the child."""
    launched, release = threading.Event(), threading.Event()
    pids: list[int] = []
    original = subprocess.Popen
    context = EngineContext.capture(max_transient_processes=2 if limited_by == "global" else 3, max_capture_bytes=1024)
    server = create_server(Settings(domain_command_capacity=1 if limited_by == "domain" else None), context=context)

    class GatedLaunch(original[bytes]):
        """Pause delivery of a real child handle to exercise cancellation during launch."""

        def __init__(self, args: Sequence[str], *, stdin: int, stdout: int, stderr: int, env: Mapping[str, str]) -> None:
            """Create the real CLI child and hold its handle until cancellation is pending."""
            super().__init__(args, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
            pids.append(self.pid)
            launched.set()
            release.wait()

    scope = anyio.CancelScope()

    async with Client(server) as client:
        await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "ready", "domains": ["notes"]})
        (cli / "crystalline").write_text(external_cli("import signal\nsignal.pause()\n"))
        monkeypatch.setattr("marabrain.crystalline.subprocess.Popen", GatedLaunch)

        async def request() -> None:
            """Run one MCP command inside the externally cancellable request scope."""
            with scope:
                await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "note"})

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(request)
                try:
                    assert await anyio.to_thread.run_sync(launched.wait, 5)
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()
                    error = "domain command capacity" if limited_by == "domain" else "transient process capacity"
                    with pytest.raises(ToolError, match=error):
                        await client.call_tool("delete_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "note"})
                    assert len(pids) == 1
                finally:
                    release.set()
    [pid] = pids
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not [worker for worker in threading.enumerate() if worker.name == "marabrain-cli"]
    monkeypatch.setattr("marabrain.crystalline.subprocess.Popen", original)
    (cli / "crystalline").write_text(external_cli("print('{}')\n"))
    async with Client(server) as client:
        assert (await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "note"})).structured_content == {}


async def test_read_releases_domain_before_waiting_for_compression(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dispatch another same-domain command while an admitted read awaits its LLM phase."""
    started, release = anyio.Event(), anyio.Event()
    settings = LLMSettings.model_validate({"model": "model", "provider_url": "https://provider.test/v1"})
    native = {"domain": "notes", "permalink": "note", "content": "source", "checksum": "checksum"}
    respond(cli, stdout=json.dumps(native))

    async def compress(settings: LLMSettings, query: str, content: str) -> str:
        """Hold the downstream LLM at an event after native read cleanup has completed."""
        assert query == "topic" and content == "source"
        started.set()
        await release.wait()
        return "compressed"

    monkeypatch.setattr("marabrain.memory_tools.compress_memory", compress)
    server = create_server(Settings(llm=settings, domain_command_capacity=1))
    async with Client(server) as client:
        await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "ready", "domains": ["notes"]})
        (cli / "calls.jsonl").unlink()

        async def request() -> None:
            """Complete the original compressed request after a second command has dispatched."""
            result = await client.call_tool(
                "read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "note", "compress": True, "query": "topic"}
            )
            assert result.structured_content == {**native, "content": "compressed", "compressed": True}

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(request)
                await started.wait()
                try:
                    respond(cli)
                    assert (
                        await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "topic", "domains": ["notes"]})
                    ).structured_content == RECEIPT
                    assert len(calls(cli)) == 2
                finally:
                    release.set()
