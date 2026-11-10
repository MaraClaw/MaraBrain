"""Provide managed fake children and socket barriers at real subprocess boundaries."""

import os
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream
from anyio.streams.memory import MemoryObjectReceiveStream
from pydantic import BaseModel, ConfigDict
from workspace_fake import SCRIPT
from test_workspaces import records

from marabrain.config import EngineMode, Settings
from marabrain.engine_context import EngineContext

MANAGED = '''
import signal
import socket

def gate(phase):
    """Hold an opted-in child phase at the test's socket barrier."""
    if (state / ("gate-" + phase)).exists():
        with socket.create_connection(("127.0.0.1", int(os.environ["RESIDENCY_TEST_PORT"]))) as connection:
            connection.sendall((json.dumps({"phase": phase, "pid": os.getpid(), "state": str(state)}) + "\\n").encode())
            connection.recv(1)

owner = state / "owner"
if args == ["serve", "--http", "off"]:
    def stop(signum, frame):
        """Expose graceful termination before the managed child exits."""
        gate("stop")
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    owner.write_text(str(os.getpid()))
    try:
        while True:
            signal.pause()
    finally:
        owner.unlink(missing_ok=True)
if args == ["ctl", "status"]:
    try:
        pid = int(owner.read_text())
        os.kill(pid, 0)
    except (FileNotFoundError, ProcessLookupError):
        sys.exit(1)
    gate("status")
    print(json.dumps({"pid": pid}))
    sys.exit(0)
gate(args[1] if args[0] == "--json" else args[0])
'''


class ChildEvent(BaseModel):
    """Parse child evidence before exposing it to the barrier controller."""

    model_config = ConfigDict(frozen=True)
    phase: str
    pid: int
    state: Path


@dataclass(frozen=True, slots=True)
class ChildGate:
    """Hold a child at its reported boundary until explicitly released."""

    event: ChildEvent
    release: anyio.Event = field(default_factory=anyio.Event)


@pytest.fixture
def resident_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_root: Path) -> Iterator[tuple[Path, Path]]:
    """Install a per-workspace managed child while keeping files in a private short root."""
    for name in tuple(os.environ):
        if name.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_", "CRYSTALLINE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("MARABRAIN_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("WORKSPACE_TEST_EVENTS", str(events))
    executable = tmp_path / "crystalline"
    executable.write_text(SCRIPT.replace('if args == ["ctl", "status"]:', MANAGED + '\nif args == ["ctl", "status"]:'))
    executable.chmod(0o755)
    yield workspace_root, events
    if events.exists():
        for event in records(events):
            assert_reaped(event.pid)


@asynccontextmanager
async def child_gates(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[MemoryObjectReceiveStream[ChildGate]]:
    """Receive child boundary events over owned loopback connections and release them on teardown."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    monkeypatch.setenv("RESIDENCY_TEST_PORT", str(listener.extra(SocketAttribute.local_port)))
    send, receive = anyio.create_memory_object_stream[ChildGate](100)
    gates: list[ChildGate] = []

    async def hold(connection: SocketStream) -> None:
        """Keep a real child blocked after recording its arrival and before its response."""
        async with connection:
            reader = BufferedByteReceiveStream(connection)
            gate = ChildGate(ChildEvent.model_validate_json(await reader.receive_until(b"\n", 4096)))
            gates.append(gate)
            await send.send(gate)
            await gate.release.wait()
            try:
                await connection.send(b"x")
            except anyio.BrokenResourceError:
                return

    async with listener, send, receive, anyio.create_task_group() as tasks:
        tasks.start_soon(listener.serve, hold)
        try:
            yield receive
        finally:
            for gate in gates:
                gate.release.set()
            tasks.cancel_scope.cancel()


def managed_settings(limit: int | None = 1) -> Settings:
    """Build the owned-daemon policy shared by residency scenarios."""
    return Settings(engine_mode=EngineMode.MANAGED, max_resident_workspaces=limit)


def context_for(workspace_id: str, root: Path) -> EngineContext:
    """Derive the same private child paths used by the application manager."""
    return EngineContext.capture().for_workspace(workspace_id, root)


def assert_reaped(pid: int) -> None:
    """Require the actual child to have left the process table."""
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
