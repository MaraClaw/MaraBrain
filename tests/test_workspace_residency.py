"""Exercise resident reservations, idle selection, and supervisor cleanup with managed children."""

import os
from pathlib import Path

import anyio
import pytest
from anyio.abc import TaskStatus
from fastmcp.exceptions import ToolError
from residency_support import assert_reaped, child_gates, context_for, managed_settings, resident_cli as resident_cli
from test_workspaces import records

from marabrain.engine_context import EngineContext
from marabrain.workspaces import WorkspaceManager
from marabrain.workspace_storage import prepare_workspace


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_starting_reservations_coalesce_and_caller_cancellation_is_independent(
    resident_cli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, cancel_first: bool
) -> None:
    """Reserve both callers before readiness so unrelated work cannot take their startup slot."""
    root, events = resident_cli
    prepare_workspace("alpha", root)
    (context_for("alpha", root).state_home / "gate-domain").touch()
    settings = managed_settings()
    results: list[EngineContext] = []
    release = anyio.Event()
    handoff, resume = anyio.Event(), anyio.Event()

    async def request(*, task_status: TaskStatus[anyio.CancelScope]) -> None:
        """Hold an independently cancellable reservation through its eventual engine use."""
        with anyio.CancelScope() as scope:
            task_status.started(scope)
            async with manager.lease("alpha") as runtime:
                results.append(runtime.context)
                await release.wait()

    with anyio.fail_after(15):
        async with child_gates(monkeypatch) as gates:
            manager = WorkspaceManager(settings, EngineContext.for_application(settings))
            async with manager.lifespan(), anyio.create_task_group() as tasks:
                first = await tasks.start(request)
                gate = await gates.receive()
                activation = manager._activations["alpha"]
                original_wait = type(activation.ready).wait

                async def ready_wait(event: anyio.Event) -> None:
                    """Pause the second waiter after readiness but before it receives its leased runtime."""
                    await original_wait(event)
                    if event is activation.ready:
                        handoff.set()
                        await resume.wait()

                monkeypatch.setattr(type(activation.ready), "wait", ready_wait)
                await tasks.start(request)
                await anyio.wait_all_tasks_blocked()
                try:
                    if cancel_first:
                        first.cancel()
                        await anyio.wait_all_tasks_blocked()
                    with pytest.raises(ToolError, match="^Resident workspace capacity is busy\\.$"):
                        async with manager.lease("beta"):
                            pytest.fail("A starting runtime lost its reserved slot.")
                    gate.release.set()
                    await handoff.wait()
                    with pytest.raises(ToolError, match="capacity is busy"):
                        async with manager.lease("beta"):
                            pytest.fail("A ready waiter lost its reservation before receiving its runtime.")
                finally:
                    gate.release.set()
                    resume.set()
                    release.set()
        assert len(results) == (1 if cancel_first else 2)
        assert all(context is results[0] for context in results)
    assert sum(row.argv == ["serve", "--http", "off"] for row in records(events)) == 1
    for row in records(events):
        assert_reaped(row.pid)


async def test_retirement_counts_cleanup_and_coalesces_replacement_without_second_victim(
    resident_cli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reap the selected idle daemon before one coalesced replacement can launch."""
    _, events = resident_cli
    settings = managed_settings()
    results: list[EngineContext] = []

    async def replace() -> None:
        """Join the same replacement while its predecessor is still retiring."""
        async with manager.lease("beta") as runtime:
            results.append(runtime.context)

    with anyio.fail_after(15):
        async with child_gates(monkeypatch) as gates:
            manager = WorkspaceManager(settings, EngineContext.for_application(settings))
            async with manager.lifespan():
                async with manager.lease("alpha") as runtime:
                    state = runtime.context.state_home
                    pid = int((state / "owner").read_text())
                (state / "gate-stop").touch()
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(replace)
                    gate = await gates.receive()
                    try:
                        assert gate.event.phase == "stop" and gate.event.pid == pid
                        tasks.start_soon(replace)
                        await anyio.wait_all_tasks_blocked()
                        for workspace_id in ("alpha", "gamma"):
                            with pytest.raises(ToolError, match="capacity is busy"):
                                async with manager.lease(workspace_id):
                                    pytest.fail("Retirement reused an occupied slot.")
                        assert results == []
                        assert len(manager._activations) == 1
                        assert sum(row.argv == ["serve", "--http", "off"] for row in records(events)) == 1
                        os.kill(pid, 0)
                    finally:
                        gate.release.set()
                assert_reaped(pid)
                assert len(results) == 2 and results[0] is results[1]
                async with manager.lease("beta") as runtime:
                    assert runtime.context is results[0]
        assert sum(row.argv == ["serve", "--http", "off"] for row in records(events)) == 2


async def test_health_pins_probes_without_refreshing_memory_lru(resident_cli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a probed generation alive and evict the least recently used memory workspace afterward."""
    _, events = resident_cli
    settings = managed_settings(2)
    health: list[bool] = []

    async def probe() -> None:
        """Exercise the manager's real shared-deadline health probes."""
        health.append(await manager.health())

    with anyio.fail_after(15):
        async with child_gates(monkeypatch) as gates:
            manager = WorkspaceManager(settings, EngineContext.for_application(settings))
            async with manager.lifespan():
                async with manager.lease("alpha") as alpha:
                    alpha_pid = int((alpha.context.state_home / "owner").read_text())
                async with manager.lease("beta") as beta:
                    beta_pid = int((beta.context.state_home / "owner").read_text())
                async with manager.lease("alpha"):
                    pass
                marker = alpha.context.state_home / "gate-status"
                marker.touch()
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(probe)
                    gate = await gates.receive()
                    try:
                        with pytest.raises(ToolError, match="capacity is busy"):
                            async with manager.lease("gamma"):
                                pytest.fail("Health lost a pinned engine.")
                        os.kill(alpha_pid, 0)
                        os.kill(beta_pid, 0)
                    finally:
                        gate.release.set()
                marker.unlink()
                assert health == [True]
                async with manager.lease("gamma"):
                    assert_reaped(beta_pid)
                    os.kill(alpha_pid, 0)
                assert await manager.health()
        for row in records(events):
            assert_reaped(row.pid)


async def test_failed_activation_releases_its_slot_after_cleanup(resident_cli: tuple[Path, Path]) -> None:
    """Recover a failed startup and keep all slots reserved through real child cleanup."""
    root, _ = resident_cli
    settings = managed_settings()
    context = EngineContext.for_application(settings)
    manager = WorkspaceManager(settings, context)
    original = Path(context.environment["PATH"].split(os.pathsep)[0]) / "crystalline"
    script = original.read_text()
    original.write_text(script.replace('print(json.dumps({"domains": domains}))', 'print("{}")'))
    async with manager.lifespan():
        with pytest.raises(ToolError, match="activation failed"):
            async with manager.lease("alpha"):
                pytest.fail("Invalid complete synchronization activated a runtime.")
        pid = int(next(row.pid for row in records(resident_cli[1]) if row.argv == ["serve", "--http", "off"]))
        assert_reaped(pid)
        original.write_text(script)
        async with manager.lease("alpha") as runtime:
            assert runtime.context.state_home == context_for("alpha", root).state_home
        assert await manager.health()
