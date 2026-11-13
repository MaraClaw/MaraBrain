"""Prove per-workspace transient shares with barrier-held real fake CLI children."""

import os
import sys
import threading
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream, TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream
from fastmcp.exceptions import ToolError

from marabrain.config import Settings
from marabrain.crystalline import run_json
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import run_startup_command
from marabrain.transient import StdoutCapture


class HeldChildren:
    """Start requests whose fake CLI children block at a socket barrier until the test releases them."""

    def __init__(self, requests: TaskGroup, arrivals: MemoryObjectReceiveStream[int], retained: threading.Event, pids: list[int]) -> None:
        """Track the request group, child arrivals, the stdout retention signal, and held PIDs."""
        self.requests = requests
        self.arrivals = arrivals
        self.retained = retained
        self.pids = pids

    async def hold(self, request: Callable[[], Coroutine[None, None, None]]) -> None:
        """Return once the started request's child is live and its parent has retained the child's stdout."""
        self.retained.clear()
        self.requests.start_soon(request)
        self.pids.append(await self.arrivals.receive())
        assert await anyio.to_thread.run_sync(self.retained.wait, 5)


@asynccontextmanager
async def held_children(cli: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[HeldChildren]:
    """Serve the barrier, release every held child on exit, and prove each was reaped and its worker joined.

    The fake CLI prints `{}` and, when its final argument is `held`, waits for the barrier.
    """
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    (cli / "crystalline").write_text(
        f"#!{sys.executable}\nimport os, socket, sys\nos.write(1, b'{{}}')\n"
        f"s = socket.create_connection(('127.0.0.1', {port})) if sys.argv[-1] == 'held' else None\n"
        "if s: s.sendall(str(os.getpid()).encode())\nif s: s.recv(1)\n"
    )
    release = anyio.Event()
    retained = threading.Event()
    arrived, arrivals = anyio.create_memory_object_stream[int](4)
    pids: list[int] = []
    append = StdoutCapture.append

    def observe(capture: StdoutCapture, chunk: bytes) -> None:
        """Publish successful retention rather than relying on OS write timing."""
        append(capture, chunk)
        retained.set()

    async def barrier(connection: SocketStream) -> None:
        """Hold the real child live until the test releases every held child."""
        async with connection:
            await arrived.send(int(await connection.receive()))
            await release.wait()
            await connection.send(b"x")

    monkeypatch.setattr(StdoutCapture, "append", observe)
    with anyio.fail_after(10):
        async with listener, arrived, arrivals, anyio.create_task_group() as serving:
            serving.start_soon(listener.serve, barrier)
            async with anyio.create_task_group() as requests:
                try:
                    yield HeldChildren(requests, arrivals, retained, pids)
                finally:
                    release.set()
            serving.cancel_scope.cancel()
    assert pids
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not [worker for worker in threading.enumerate() if worker.name == "marabrain-cli"]


def cli_workers() -> set[int | None]:
    """Return the identities of live CLI worker threads."""
    return {worker.ident for worker in threading.enumerate() if worker.name == "marabrain-cli"}


@pytest.mark.parametrize("shared", [True, False])
async def test_saturated_workspace_slot_share_refuses_while_siblings_complete(cli: Path, monkeypatch: pytest.MonkeyPatch, shared: bool) -> None:
    """Refuse a re-derived saturated workspace without a worker while siblings, other apps, and control still run."""
    settings = Settings(max_transient_processes=3, max_workspace_transient_processes=1 if shared else None)
    app = EngineContext.for_application(settings)
    other = EngineContext.for_application(settings)
    first, second = app.for_workspace("first", cli), app.for_workspace("second", cli)
    assert first.transients is second.transients is app.transients
    assert (first.workspace_transients is not None and first.workspace_transients.application is app.transients) is shared
    ledger = app.transients.workspace_ledger

    async def held_read() -> None:
        """Hold one data slot for the first workspace until the barrier opens."""
        assert await run_json(["held"], context=first) == {}

    async with held_children(cli, monkeypatch) as children:
        await children.hold(held_read)
        again = app.for_workspace("first", cli)
        if shared:
            workers = cli_workers()
            with pytest.raises(ToolError, match="^Crystalline transient process capacity is exhausted.$"):
                await run_json(["read"], context=again)
            assert cli_workers() == workers
            assert ledger.held("first") == (1, 0)
        else:
            assert again.workspace_transients is None and again.data_transients is app.transients
            assert await run_json(["read"], context=again) == {}
        assert await run_json(["read"], context=second) == {}
        assert await run_json(["read"], context=other.for_workspace("first", cli)) == {}
        with await run_startup_command([str(cli / "crystalline"), "status"], first) as raw:
            assert raw == b"{}"
    assert ledger.held("first") == ledger.held("second") == (0, 0)
    assert await run_json(["read"], context=first) == {}


@pytest.mark.parametrize("total", [None, 4])
async def test_saturated_workspace_byte_share_refuses_while_siblings_complete(cli: Path, monkeypatch: pytest.MonkeyPatch, total: int | None) -> None:
    """Refuse excess workspace bytes without touching the shared budget while siblings, other apps, and control still capture."""
    settings = Settings(max_transient_processes=4, max_workspace_capture_bytes=2, max_total_capture_bytes=total)
    app = EngineContext.for_application(settings)
    other = EngineContext.for_application(settings)
    first, second = app.for_workspace("first", cli), app.for_workspace("second", cli)
    ledger = app.transients.workspace_ledger

    async def held_read() -> None:
        """Retain the first workspace's whole byte share until the barrier opens."""
        assert await run_json(["held"], context=first) == {}

    async with held_children(cli, monkeypatch) as children:
        await children.hold(held_read)
        assert ledger.held("first") == (0, 2)
        with pytest.raises(ToolError, match="^Crystalline output exceeded the capture limit; a mutation may or may not have been applied.$"):
            await run_json(["read"], context=app.for_workspace("first", cli))
        assert ledger.held("first") == (0, 2)
        assert await run_json(["read"], context=second) == {}
        assert await run_json(["read"], context=other.for_workspace("first", cli)) == {}
        with await run_startup_command([str(cli / "crystalline"), "status"], first) as raw:
            assert raw == b"{}"
            assert ledger.held("first") == (0, 2)
    assert ledger.held("first") == ledger.held("second") == (0, 0)
    assert await run_json(["read"], context=first) == {}


@pytest.mark.parametrize("resource", ["slots", "bytes"])
async def test_application_refusal_leaves_no_workspace_charge(cli: Path, monkeypatch: pytest.MonkeyPatch, resource: str) -> None:
    """Roll back the workspace share when the shared application level refuses the same command."""
    if resource == "slots":
        settings = Settings(max_transient_processes=2, max_workspace_transient_processes=2, max_workspace_capture_bytes=2)
        error = "capacity is exhausted"
    else:
        settings = Settings(max_total_capture_bytes=2, max_workspace_transient_processes=2, max_workspace_capture_bytes=2)
        error = "capture limit"
    app = EngineContext.for_application(settings)
    ledger = app.transients.workspace_ledger

    async def held_read() -> None:
        """Exhaust the application's data slot or byte budget from a sibling workspace."""
        assert await run_json(["held"], context=app.for_workspace("second", cli)) == {}

    async with held_children(cli, monkeypatch) as children:
        await children.hold(held_read)
        assert ledger.held("second") == (1, 2)
        with pytest.raises(ToolError, match=error):
            await run_json(["read"], context=app.for_workspace("first", cli))
        assert ledger.held("first") == (0, 0)
    assert ledger.held("second") == (0, 0)
    assert await run_json(["read"], context=app.for_workspace("first", cli)) == {}


async def test_control_lane_is_never_charged_to_a_workspace_share(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a workspace's whole data share available while its own control command holds a child and bytes."""
    app = EngineContext.for_application(Settings(max_transient_processes=2, max_workspace_transient_processes=1, max_workspace_capture_bytes=2))
    first = app.for_workspace("first", cli)

    async def held_status() -> None:
        """Hold the reserved control slot with retained stdout until the barrier opens."""
        with await run_startup_command([str(cli / "crystalline"), "held"], first) as raw:
            assert raw == b"{}"

    async with held_children(cli, monkeypatch) as children:
        await children.hold(held_status)
        assert app.transients.workspace_ledger.held("first") == (0, 0)
        assert await run_json(["read"], context=first) == {}
