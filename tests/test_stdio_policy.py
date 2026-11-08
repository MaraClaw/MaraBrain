"""Real stdio transport tests for domain isolation and request cancellation."""

import os
import sys
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from memory_fake import RECEIPT, calls, domain_list


async def test_stdio_servers_keep_separate_domain_policies(cli: Path) -> None:
    """Apply each module-launched server's exact policy without running denied commands."""
    domain_list(cli, ["Notes"])
    common = {**os.environ, "MARABRAIN_ENGINE_MODE": "external", "MARABRAIN_TRANSPORT": "stdio"}
    allowed = StdioTransport(sys.executable, ["-m", "marabrain"], env={**common, "MARABRAIN_ALLOWED_DOMAINS": '["Notes"]'})
    denied = StdioTransport(sys.executable, ["-m", "marabrain"], env={**common, "MARABRAIN_ALLOWED_DOMAINS": "[]"})
    with anyio.fail_after(15):
        async with Client(allowed) as allowed_client, Client(denied) as denied_client:
            for client, domain in ((allowed_client, "notes"), (denied_client, "Notes")):
                with pytest.raises(ToolError):
                    await client.call_tool("read_memory", {"domain": domain, "permalink": "plan"})
            assert [record["argv"] for record in calls(cli)] == [["--json", "domain", "list"]] * 2
            result = await allowed_client.call_tool("read_memory", {"domain": "Notes", "permalink": "plan"})
            assert result.structured_content == RECEIPT
            assert len(calls(cli)) == 3


async def test_stdio_cancellation_reaps_child_before_disconnect(cli: Path) -> None:
    """Cancel a running request and reuse its connection after the child is reaped."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    (cli / "crystalline").write_text(
        f"#!{sys.executable}\n"
        "import os, socket, sys\n"
        "if sys.argv[1:] == ['--json', 'domain', 'list']:\n"
        '    print(\'{"domains":[{"name":"notes"}]}\')\n'
        "    sys.exit(0)\n"
        "if 'hang' in sys.argv:\n"
        f"    s = socket.create_connection(('127.0.0.1', {port}))\n"
        "    s.sendall(str(os.getpid()).encode())\n"
        "    s.recv(1)\n"
        "else:\n"
        "    print('{\"ok\": true}')\n"
    )
    send, receive = anyio.create_memory_object_stream[int](1)
    exited = anyio.Event()

    async def started(connection: SocketStream) -> None:
        """Receive the PID and observe the child's socket close on exit."""
        async with connection:
            await send.send(int(await connection.receive()))
            try:
                await connection.receive()
            except anyio.EndOfStream:
                exited.set()

    transport = StdioTransport(
        sys.executable,
        ["-m", "marabrain"],
        env={**os.environ, "MARABRAIN_ENGINE_MODE": "external", "MARABRAIN_TRANSPORT": "stdio", "MARABRAIN_COMMAND_TIMEOUT_SECONDS": "60"},
    )
    with anyio.fail_after(15):
        async with send, receive, listener, Client(transport) as client, anyio.create_task_group() as tasks:
            tasks.start_soon(listener.serve, started)
            scope = anyio.CancelScope()

            async def request() -> None:
                """Issue one request whose cancellation sends an MCP notification."""
                with scope:
                    await client.call_tool("read_memory", {"domain": "notes", "permalink": "hang"})

            tasks.start_soon(request)
            pid = await receive.receive()
            os.kill(pid, 0)
            scope.cancel()
            await exited.wait()
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await anyio.lowlevel.checkpoint()
            result = await client.call_tool("read_memory", {"domain": "notes", "permalink": "after"})
            assert result.structured_content == {"ok": True}
            tasks.cancel_scope.cancel()
