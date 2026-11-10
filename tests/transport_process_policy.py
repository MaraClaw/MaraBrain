"""Share real child barriers and lifecycle assertions across MCP transports."""

import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream
from anyio.streams.memory import MemoryObjectReceiveStream
from anyio.streams.buffered import BufferedByteReceiveStream
from fastmcp import Client
from fastmcp.client.transports import ClientTransport
from memory_fake import RECEIPT, WORKSPACE_ID
from pydantic import BaseModel, ConfigDict

CAPTURE_BYTES: Final = 512
PRIVATE_OUTPUT: Final = "private-child-output-must-stay-hidden"


class Launch(BaseModel):
    """Parse the fake executable's process launch record."""

    model_config = ConfigDict(frozen=True)
    pid: int
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Child:
    """Expose a real child's release barrier and socket exit notification."""

    pid: int
    connection: SocketStream
    exited: anyio.Event

    async def assert_reaped(self) -> None:
        """Verify the command response follows child exit and process reaping."""
        await self.exited.wait()
        with pytest.raises(ProcessLookupError):
            os.kill(self.pid, 0)


@dataclass(frozen=True, slots=True)
class ProcessProbe:
    """Expose child-start notifications and the external launch audit."""

    cli: Path
    started: MemoryObjectReceiveStream[Child]

    def launches(self) -> list[Launch]:
        """Return every real invocation without inspecting runtime internals."""
        log = self.cli / "calls.jsonl"
        return [Launch.model_validate_json(line) for line in log.read_text().splitlines()] if log.exists() else []


@asynccontextmanager
async def process_probe(cli: Path) -> AsyncGenerator[ProcessProbe]:
    """Install a socket-gated executable and close its owned listener on exit."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    (cli / "crystalline").write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, socket, sys\n"
        "root = pathlib.Path(__file__).parent\n"
        "args = sys.argv[1:]\n"
        "with (root / 'calls.jsonl').open('a') as log:\n"
        "    log.write(json.dumps({'pid': os.getpid(), 'argv': args}) + '\\n')\n"
        f"sys.stderr.write({PRIVATE_OUTPUT!r})\n"
        "if args == ['--json', 'domain', 'list']:\n"
        "    print(json.dumps({'domains': json.loads((root / 'domains.json').read_text())}))\n"
        "    sys.exit(0)\n"
        "health = args == ['ctl', 'status']\n"
        "overflow = 'overflow' in args or (health and (root / 'overflow-health').exists())\n"
        "if 'held' in args or overflow:\n"
        f"    with socket.create_connection(('127.0.0.1', {port})) as barrier:\n"
        "        barrier.sendall((str(os.getpid()) + '\\n').encode())\n"
        "        if overflow:\n"
        f"            payload = json.dumps({{'private': {PRIVATE_OUTPUT!r}}})\n"
        f"            sys.stdout.write(payload.ljust({CAPTURE_BYTES + 1}))\n"
        "            sys.stdout.flush()\n"
        "        barrier.recv(1)\n"
        "    if overflow:\n"
        "        sys.exit(0)\n"
        f"payload = json.dumps({RECEIPT!r})\n"
        f"sys.stdout.write('{{}}' if health else payload.ljust({CAPTURE_BYTES} + ('larger' in args)))\n"
    )
    send, receive = anyio.create_memory_object_stream[Child](1)

    async def started(connection: SocketStream) -> None:
        """Publish child admission and observe exit independently of MCP responses."""
        async with connection:
            incoming = BufferedByteReceiveStream(connection)
            child = Child(int(await incoming.receive_until(b"\n", 32)), connection, anyio.Event())
            await send.send(child)
            try:
                await incoming.receive()
            except anyio.EndOfStream:
                child.exited.set()

    async with listener, send, receive, anyio.create_task_group() as tasks:
        tasks.start_soon(listener.serve, started)
        try:
            yield ProcessProbe(cli, receive)
        finally:
            tasks.cancel_scope.cancel()


async def assert_overflow_reaped[T: ClientTransport](client: Client[T], probe: ProcessProbe, *, health: bool = False) -> None:
    """Reject valid JSON plus excess bytes before EOF and allow the same lane's reuse."""
    marker = probe.cli / "overflow-health"
    before = len(probe.launches())
    if health:
        marker.touch()
    try:
        async with anyio.create_task_group() as tasks:
            arguments = {} if health else {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "overflow"}
            result = tasks.create_task(client.call_tool("health" if health else "read_memory", arguments, raise_on_error=False))
            child = await probe.started.receive()
            with anyio.fail_after(5):
                failure = await result
                assert failure.is_error is True
                assert failure.structured_content is None
                assert PRIVATE_OUTPUT not in str(failure.content)
                await child.assert_reaped()
                assert len(probe.launches()) == before + 1
    finally:
        marker.unlink(missing_ok=True)
    result = await client.call_tool(
        "health" if health else "read_memory", {} if health else {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "after"}
    )
    assert result.structured_content == ({"status": "ok", "version": "0.4.5"} if health else RECEIPT)
    assert len(probe.launches()) == before + 2


async def assert_transport_process_policy[T: ClientTransport](client: Client[T], probe: ProcessProbe, independent: Client[T] | None = None) -> None:
    """Verify shared admission, reserved health, overflow cleanup, and native response reuse."""
    inventory = ["health", "create_memory", "read_memory", "edit_memory", "delete_memory", "search_memories"]
    assert [tool.name for tool in await client.list_tools()] == inventory
    async with anyio.create_task_group() as tasks:
        held = tasks.create_task(client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "held"}))
        child = await probe.started.receive()
        try:
            before = probe.launches()
            with anyio.fail_after(1):
                rejected = await client.call_tool(
                    "search_memories", {"workspace_id": WORKSPACE_ID, "query": "rejected", "domains": ["notes"]}, raise_on_error=False
                )
            assert rejected.is_error is True
            assert rejected.structured_content is None
            assert PRIVATE_OUTPUT not in str(rejected.content)
            with anyio.fail_after(1):
                again = await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "rejected"}, raise_on_error=False)
            assert again.is_error is True
            assert again.structured_content is None
            assert PRIVATE_OUTPUT not in str(again.content)
            assert probe.launches() == before
            with anyio.fail_after(1):
                health = await client.call_tool("health", {})
            assert health.structured_content == {"status": "ok", "version": "0.4.5"}
            assert len(probe.launches()) == len(before) + 1
            assert probe.launches()[-1].argv == ("ctl", "status")
            os.kill(child.pid, 0)
            if independent is not None:
                result = await independent.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "larger"})
                assert result.structured_content == RECEIPT
        finally:
            await child.connection.send(b"r")
        assert (await held).structured_content == RECEIPT
        await child.assert_reaped()
    await assert_overflow_reaped(client, probe)
    await assert_overflow_reaped(client, probe, health=True)
    assert [tool.name for tool in await client.list_tools(cache_mode="bypass")] == inventory
    for launch in probe.launches():
        with pytest.raises(ProcessLookupError):
            os.kill(launch.pid, 0)
