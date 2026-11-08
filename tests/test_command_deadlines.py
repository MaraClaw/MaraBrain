"""End-to-end cancellation tests for memory command subprocesses."""

import os
import sys
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from marabrain.config import Settings
from marabrain.memory_tools import register_memory_tools
from marabrain.server import create_server


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
