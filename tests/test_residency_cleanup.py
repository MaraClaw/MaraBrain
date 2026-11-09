"""Keep residency slots through command cleanup, supervisor shutdown, and fatal daemon loss."""

import os
import signal
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

import anyio
import pytest
from anyio.abc import TaskStatus
from fastmcp import Client
from fastmcp.exceptions import ToolError
from residency_support import assert_reaped, child_gates, context_for, managed_settings, resident_cli as resident_cli
from test_workspaces import records

from marabrain import crystalline
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import EngineLifecycleError
from marabrain.server import create_server
from marabrain.transient import StdoutCapture
from marabrain.workspaces import WorkspaceManager


@pytest.mark.parametrize("outcome", ["cancel", "failure"])
async def test_command_cancellation_and_failure_keep_slot_until_worker_cleanup(
    resident_cli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Refuse replacement while a real command launch or failed command worker still owns cleanup."""
    root, events = resident_cli
    entered, release = threading.Event(), threading.Event()
    original_launch = subprocess.Popen
    original_communicate = crystalline.communicate
    pids: list[int] = []
    scope = anyio.CancelScope()

    class GatedLaunch(original_launch[bytes]):
        """Delay handing a real command handle back to its owning worker."""

        def __init__(self, args: Sequence[str], *, stdin: int, stdout: int, stderr: int, env: Mapping[str, str]) -> None:
            """Expose OS creation and keep the worker alive until its cancellation is pending."""
            super().__init__(args, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
            pids.append(self.pid)
            entered.set()
            release.wait()

    def finishing(child: subprocess.Popen[bytes], data: bytes, capture: StdoutCapture) -> bytearray:
        """Hold the worker after a native failure and before it returns its cleanup receipt."""
        try:
            return original_communicate(child, data, capture)
        finally:
            pids.append(child.pid)
            entered.set()
            release.wait()

    async with Client(create_server(managed_settings())) as client:
        await client.call_tool("create_memory", {"workspace_id": "alpha", "domain": "notes", "title": "note", "content": "body"})
        daemon = int((context_for("alpha", root).state_home / "owner").read_text())
        if outcome == "cancel":
            monkeypatch.setattr("marabrain.crystalline.subprocess.Popen", GatedLaunch)
        else:
            monkeypatch.setattr("marabrain.crystalline.communicate", finishing)

        async def request() -> None:
            """Observe native failure or independently cancel the tool's request scope."""
            with scope:
                if outcome == "failure":
                    with pytest.raises(ToolError, match="command failed"):
                        await client.call_tool("read_memory", {"workspace_id": "alpha", "domain": "notes", "permalink": "missing"})
                else:
                    await client.call_tool("read_memory", {"workspace_id": "alpha", "domain": "notes", "permalink": "note"})

        with anyio.fail_after(15):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(request)
                try:
                    assert await anyio.to_thread.run_sync(entered.wait, 5)
                    if outcome == "cancel":
                        scope.cancel()
                        await anyio.wait_all_tasks_blocked()
                    with pytest.raises(ToolError, match="^Resident workspace capacity is busy\\.$"):
                        await client.call_tool("search_memories", {"workspace_id": "beta", "domains": ["notes"], "query": "body"})
                    os.kill(daemon, 0)
                    assert len(pids) == 1
                finally:
                    release.set()
        monkeypatch.setattr("marabrain.crystalline.subprocess.Popen", original_launch)
        monkeypatch.setattr("marabrain.crystalline.communicate", original_communicate)
        result = await client.call_tool("search_memories", {"workspace_id": "beta", "domains": ["notes"], "query": "body"})
        assert result.data == {"results": []}
        assert_reaped(daemon)
    for row in records(events):
        assert_reaped(row.pid)
    assert not [worker for worker in threading.enumerate() if worker.name == "marabrain-cli"]


async def test_shutdown_drains_retiring_daemons_and_discards_waiting_replacements(resident_cli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Wait for both old daemons' cleanup without launching their canceled successors during shutdown."""
    _, events = resident_cli
    settings = managed_settings(2)
    started, stopped = anyio.Event(), anyio.Event()
    with anyio.fail_after(15):
        async with child_gates(monkeypatch) as gates:
            manager = WorkspaceManager(settings, EngineContext.for_application(settings))

            async def request(workspace_id: str) -> None:
                """Reserve a successor whose selected victim is still cleaning up."""
                async with manager.lease(workspace_id):
                    pytest.fail("A replacement started before its victim was reaped.")

            async def supervise(*, task_status: TaskStatus[anyio.CancelScope]) -> None:
                """Own the application lifetime and expose its externally requested shutdown."""
                with anyio.CancelScope() as scope:
                    task_status.started(scope)
                    async with manager.lifespan():
                        for workspace_id in ("alpha", "beta"):
                            async with manager.lease(workspace_id) as runtime:
                                (runtime.context.state_home / "gate-stop").touch()
                        async with anyio.create_task_group() as requests:
                            requests.start_soon(request, "gamma")
                            requests.start_soon(request, "delta")
                            started.set()
                            await anyio.sleep_forever()
                stopped.set()

            async with anyio.create_task_group() as tasks:
                scope = await tasks.start(supervise)
                await started.wait()
                first, second = await gates.receive(), await gates.receive()
                try:
                    assert first.event.pid != second.event.pid
                    scope.cancel()
                    await anyio.wait_all_tasks_blocked()
                    assert not stopped.is_set()
                    assert len([row for row in records(events) if row.argv == ["serve", "--http", "off"]]) == 2
                finally:
                    first.release.set()
                    second.release.set()
                await stopped.wait()
                assert_reaped(first.event.pid)
                assert_reaped(second.event.pid)
            assert not await manager.health()
            with pytest.raises(ToolError, match="not running"):
                async with manager.lease("gamma"):
                    pytest.fail("A stopped manager retained an activation.")
    assert len([row for row in records(events) if row.argv == ["serve", "--http", "off"]]) == 2


async def test_unexpected_resident_daemon_death_is_fatal(resident_cli: tuple[Path, Path]) -> None:
    """Preserve fatal supervision when an idle resident dies without planned retirement."""
    settings = managed_settings()
    manager = WorkspaceManager(settings, EngineContext.for_application(settings))
    pids: list[int] = []
    with anyio.fail_after(15):
        with pytest.raises(ExceptionGroup) as failure:
            async with manager.lifespan():
                async with manager.lease("alpha") as runtime:
                    pid = int((runtime.context.state_home / "owner").read_text())
                    pids.append(pid)
                os.kill(pid, signal.SIGKILL)
                await anyio.sleep_forever()
        assert failure.value.subgroup(EngineLifecycleError) is not None
    assert_reaped(pids[0])


async def test_unset_residency_keeps_existing_unbounded_get_compatibility(resident_cli: tuple[Path, Path]) -> None:
    """Retain uncapped managed runtimes and reject an unleased capped acquisition."""
    settings = managed_settings(None)
    manager = WorkspaceManager(settings, EngineContext.for_application(settings))
    async with manager.lifespan():
        alpha = await manager.get("alpha")
        beta = await manager.get("beta")
        assert await manager.get("alpha") is alpha
        assert alpha is not beta
        assert await manager.health()
        for runtime in (alpha, beta):
            os.kill(int((runtime.context.state_home / "owner").read_text()), 0)
    limited = managed_settings()
    capped = WorkspaceManager(limited, EngineContext.for_application(limited))
    async with capped.lifespan():
        with pytest.raises(ToolError, match="requires a lease"):
            await capped.get("alpha")
