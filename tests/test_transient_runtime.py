"""Prove immediate application-owned admission with barrier-controlled real children."""

import inspect
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import pytest
from anyio.abc import Process, SocketAttribute, SocketStream
from fastmcp import Client
from fastmcp.exceptions import ToolError
from domain_fake import domain_cli as domain_cli
from memory_fake import RECEIPT, WORKSPACE_ID, calls, respond
from test_domain_registry import argv
from test_domain_resolution import command_barrier

from marabrain import lifecycle
from marabrain.config import Settings
from marabrain.crystalline import run_json
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import run_startup_command
from marabrain.server import create_server
from marabrain.transient import InputBudgetError, InputSizeError, ProcessAdmissionError, TransientLimitError, TransientRuntime
from marabrain.workspace_storage import prepare_workspace


async def test_data_saturation_reserves_one_immediate_control_slot_and_independent_apps(cli: Path) -> None:
    """Keep C-1 data children and one control child live while excess calls launch nothing."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    (cli / "crystalline").write_text(
        f"#!{sys.executable}\nimport os, socket\n"
        f"s = socket.create_connection(('127.0.0.1', {port}))\n"
        "s.sendall(str(os.getpid()).encode())\ns.recv(1)\nprint('{}')\n"
    )
    context = EngineContext.capture(max_transient_processes=3, max_capture_bytes=10)
    other = EngineContext.capture(max_transient_processes=2, max_capture_bytes=10)
    arrived, receive = anyio.create_memory_object_stream[int](4)
    release = anyio.Event()
    pids: list[int] = []
    completed: list[bool] = []
    done = anyio.Event()

    async def barrier(connection: SocketStream) -> None:
        """Hold a live child at the pipe boundary until its owner releases the test barrier."""
        async with connection:
            await arrived.send(int(await connection.receive()))
            await release.wait()
            await connection.send(b"x")

    async def data_request(bound: EngineContext) -> None:
        """Record a native successful receipt after the child crosses its barrier."""
        assert await run_json(["domain", "list"], context=bound) == {}
        completed.append(True)
        if len(completed) == 4:
            done.set()

    async def control_request() -> None:
        """Exercise the control owner while data children occupy all data capacity."""
        with await run_startup_command([str(cli / "crystalline")], context) as output:
            assert output == b"{}\n"
        completed.append(True)
        if len(completed) == 4:
            done.set()

    with anyio.fail_after(10):
        async with listener, arrived, receive, anyio.create_task_group() as tasks:
            tasks.start_soon(listener.serve, barrier)
            for _ in range(2):
                tasks.start_soon(data_request, context)
                pids.append(await receive.receive())
            workers = {worker.ident for worker in threading.enumerate() if worker.name == "marabrain-cli"}
            with pytest.raises(ToolError, match="capacity is exhausted"):
                await run_json(["delete"], context=context)
            assert {worker.ident for worker in threading.enumerate() if worker.name == "marabrain-cli"} == workers
            tasks.start_soon(control_request)
            pids.append(await receive.receive())
            with pytest.raises(ProcessAdmissionError):
                await run_startup_command([str(cli / "crystalline")], context)
            tasks.start_soon(data_request, other)
            pids.append(await receive.receive())
            release.set()
            await done.wait()
            tasks.cancel_scope.cancel()
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not [worker for worker in threading.enumerate() if worker.name == "marabrain-cli"]
    (cli / "crystalline").write_text(f"#!{sys.executable}\nprint('{{}}')\n")
    assert await run_json(["read"], context=context) == {}
    with await run_startup_command([str(cli / "crystalline")], context) as output:
        assert output == b"{}\n"


async def test_control_slot_remains_owned_until_process_close_completes(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain control capacity after reap until async stream cleanup has also completed."""
    stopped, release = anyio.Event(), anyio.Event()
    stop = lifecycle.stop_process
    context = EngineContext.capture(max_transient_processes=2)
    (cli / "crystalline").write_text(f"#!{sys.executable}\nprint('{{}}')\n")

    async def finish_close(process: Process) -> None:
        """Expose a deterministic final cleanup barrier after reaping the real child."""
        await stop(process)
        stopped.set()
        with anyio.CancelScope(shield=True):
            await release.wait()

    async def request() -> None:
        """Own a control command through its final stream cleanup barrier."""
        with await run_startup_command([str(cli / "crystalline")], context) as output:
            assert output == b"{}\n"

    monkeypatch.setattr(lifecycle, "stop_process", finish_close)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(request)
            try:
                await stopped.wait()
                with pytest.raises(ProcessAdmissionError):
                    await run_startup_command([str(cli / "crystalline")], context)
            finally:
                release.set()
    with await run_startup_command([str(cli / "crystalline")], context) as output:
        assert output == b"{}\n"


@pytest.mark.parametrize("completion", ["success", "cancel", "timeout"])
@pytest.mark.parametrize("limited_by", ["global", "domain"])
async def test_slot_remains_owned_until_cli_worker_join_completes(cli: Path, monkeypatch: pytest.MonkeyPatch, completion: str, limited_by: str) -> None:
    """Retain domain capacity after child exit through worker joining, including cancellation."""
    finished, release = threading.Event(), threading.Event()
    original = threading.Thread
    context = EngineContext.capture(max_transient_processes=2 if limited_by == "global" else 3)
    server = create_server(
        Settings(command_timeout_seconds=0.2 if completion == "timeout" else 30, domain_command_capacity=1 if limited_by == "domain" else None), context=context
    )
    scope = anyio.CancelScope()
    script = (cli / "crystalline").read_text()

    class GatedWorker(original):
        """Retain a finished CLI worker at a deterministic join barrier."""

        def run(self) -> None:
            """Delay worker termination only after the real child has completed."""
            super().run()
            if self.name == "marabrain-cli":
                finished.set()
                release.wait()

    async with Client(server) as client:
        await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "ready", "domains": ["notes"]})
        (cli / "calls.jsonl").unlink()
        if completion == "timeout":
            (cli / "crystalline").write_text(script.replace('if plan["hang"]:', 'import signal\nsignal.pause()\nif plan["hang"]:'))
        monkeypatch.setattr("marabrain.crystalline.threading.Thread", GatedWorker)

        async def request() -> None:
            """Own a real child through its artificial worker termination barrier."""
            with scope:
                if completion == "timeout":
                    with pytest.raises(ToolError, match="may or may not have been applied"):
                        await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "one"})
                else:
                    assert (
                        await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "one"})
                    ).structured_content == RECEIPT

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(request)
                try:
                    assert await anyio.to_thread.run_sync(finished.wait, 5)
                    [record] = calls(cli)
                    pid = record["pid"]
                    assert isinstance(pid, int)
                    with pytest.raises(ProcessLookupError):
                        os.kill(pid, 0)
                    if completion == "cancel":
                        scope.cancel()
                        await anyio.lowlevel.checkpoint()
                    error = "domain command capacity" if limited_by == "domain" else "transient process capacity"
                    with pytest.raises(ToolError, match=error):
                        await client.call_tool("delete_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "one"})
                    assert len(calls(cli)) == 1
                finally:
                    release.set()
        (cli / "crystalline").write_text(script)
        assert (await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "one"})).structured_content == RECEIPT
    assert not [worker for worker in threading.enumerate() if worker.name == "marabrain-cli"]


async def test_saturated_domain_keeps_health_and_other_domains_available(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Capture the scalar quota while keeping health and other domains available beside saturation."""
    monkeypatch.setenv("MARABRAIN_DOMAIN_COMMAND_CAPACITY", "1")
    settings = Settings(max_transient_processes=3)
    server = create_server(settings)
    monkeypatch.setenv("MARABRAIN_DOMAIN_COMMAND_CAPACITY", "9")
    async with command_barrier(cli) as (started, release), Client(server) as client:
        await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "ready", "domains": ["notes"]})
        (cli / "calls.jsonl").unlink()

        async def occupy() -> None:
            """Hold the limited domain without consuming all global data slots."""
            assert (await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "held"})).structured_content == RECEIPT

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(occupy)
                await started.receive()
                try:
                    with pytest.raises(ToolError, match="domain command capacity is exhausted"):
                        await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "r", "domains": ["notes"]})
                    assert len(calls(cli)) == 1
                    assert (await client.call_tool("health")).structured_content == {"status": "ok", "version": "0.4.7"}
                    assert (
                        await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "other", "permalink": "one"})
                    ).structured_content == RECEIPT
                    assert len(calls(cli)) == 3
                finally:
                    release.set()


def assert_input_idle(context: EngineContext, limit: int) -> None:
    """Require the input budget to hold nothing: exactly its limit fits again and no more."""
    budget = context.transients.input_budget
    assert budget is not None
    budget.reserve(limit)
    with pytest.raises(InputBudgetError):
        budget.reserve(1)
    budget.release(limit)


def cli_workers() -> set[int | None]:
    """Return the identities of the live CLI worker threads."""
    return {worker.ident for worker in threading.enumerate() if worker.name == "marabrain-cli"}


def test_input_budget_fits_exactly_and_refusal_reserves_nothing() -> None:
    """Admit payloads up to the limit, refuse excess immediately, and keep no part of a refused payload."""
    runtime = TransientRuntime(max_total_input_bytes=4)
    with runtime.reserve_input(3):
        with pytest.raises(InputBudgetError):
            with runtime.reserve_input(2):
                pytest.fail("A payload over the remaining budget was admitted.")
        with runtime.reserve_input(1):
            with pytest.raises(InputBudgetError):
                with runtime.reserve_input(1):
                    pytest.fail("A payload over the remaining budget was admitted.")
    with runtime.reserve_input(4):
        pass


def test_disabled_budget_and_empty_payload_reserve_nothing() -> None:
    """Skip accounting when the budget is unset or the command carries no stdin."""
    assert TransientRuntime().input_budget is None
    with TransientRuntime().reserve_input(1 << 40):
        pass
    runtime = TransientRuntime(max_total_input_bytes=1)
    with runtime.reserve_input(0), runtime.reserve_input(1):
        pass


def test_input_budget_is_a_keyword_only_addition_owned_by_each_runtime() -> None:
    """Keep positional construction working while every runtime builds its own input budget."""
    legacy = TransientRuntime(3, 10, 20)
    assert (legacy.capacity, legacy.max_capture_bytes, legacy.max_total_capture_bytes, legacy.input_budget) == (3, 10, 20, None)
    assert inspect.signature(TransientRuntime).parameters["max_total_input_bytes"].kind is inspect.Parameter.KEYWORD_ONLY
    assert TransientRuntime(max_total_input_bytes=5).input_budget is not TransientRuntime(max_total_input_bytes=5).input_budget


def test_concurrent_reservations_admit_exactly_one_owner() -> None:
    """Allow one thread to hold the whole budget while both race and stay live."""
    runtime = TransientRuntime(max_total_input_bytes=4)
    ready, attempted = threading.Barrier(2), threading.Barrier(2)

    def compete() -> bool:
        """Race for every byte and keep the winner's hold until both attempts have finished."""
        ready.wait(timeout=5)
        try:
            with runtime.reserve_input(4):
                attempted.wait(timeout=5)
                return True
        except InputBudgetError:
            attempted.wait(timeout=5)
            return False

    with ThreadPoolExecutor(2) as workers:
        results = [workers.submit(compete) for _ in range(2)]
        assert sorted(result.result(timeout=5) for result in results) == [False, True]
    with runtime.reserve_input(4):
        pass


def test_input_errors_are_fixed_size_free_and_do_not_claim_a_mutation() -> None:
    """Keep both refusals distinct from the uncertain-mutation capture error and free of any size."""
    sizes = {str(error) for error in (InputSizeError(), InputBudgetError())}
    assert len(sizes) == 2
    for message in sizes:
        assert "may or may not" not in message and "mutation" not in message
        assert not any(character.isdigit() for character in message)
    assert isinstance(InputSizeError(), TransientLimitError) and isinstance(InputBudgetError(), TransientLimitError)


def test_application_binds_one_input_budget_shared_by_workspaces(tmp_path: Path) -> None:
    """Bind one budget per application, share it with workspace derivatives only, and leave it absent when unset."""
    app = EngineContext.for_application(Settings(max_total_input_bytes=2))
    other = EngineContext.for_application(Settings(max_total_input_bytes=2))
    standalone = EngineContext.capture(max_total_input_bytes=2)
    assert app.transients.input_budget is not None
    assert app.for_workspace("one", tmp_path).transients is app.transients
    assert app.for_workspace("two", tmp_path).transients is app.transients
    assert other.transients.input_budget is not app.transients.input_budget
    assert standalone.transients.input_budget is not app.transients.input_budget
    assert EngineContext.for_application(Settings()).transients.input_budget is None
    assert EngineContext.capture().transients.input_budget is None


@pytest.mark.parametrize("completion", ["success", "nonzero", "timeout", "launch-failure", "cancel", "cancel-before-child"])
async def test_input_reservation_is_held_through_worker_join_on_every_exit(cli: Path, monkeypatch: pytest.MonkeyPatch, completion: str) -> None:
    """Keep stdin reserved after the child is reaped until its worker joins, then release it exactly once."""
    context = EngineContext.capture(max_total_input_bytes=4)
    started, finished, begin, release = (threading.Event() for _ in range(4))
    original = threading.Thread
    scope = anyio.CancelScope()
    outcomes: list[str] = []

    class GatedWorker(original):
        """Hold the CLI worker before it launches and again after it has finished."""

        def run(self) -> None:
            """Report arrival, wait to launch, then wait to terminate once the real child is gone."""
            if self.name == "marabrain-cli":
                started.set()
                begin.wait(10)
            super().run()
            if self.name == "marabrain-cli":
                finished.set()
                release.wait(10)

    async def request() -> None:
        """Run one real write whose ownership ends only through the gated worker."""
        with scope:
            try:
                await run_json(["write", "held"], "abcd", timeout_seconds=1.0 if completion == "timeout" else 30, context=context)
                outcomes.append("ok")
            except ToolError as error:
                outcomes.append(str(error))

    async def refused() -> None:
        """Prove the held bytes refuse a competing write without creating a child or worker."""
        workers, launched = cli_workers(), len(calls(cli))
        with pytest.raises(ToolError, match="input capacity is exhausted"):
            await run_json(["write", "other"], "x", context=context)
        assert (cli_workers(), len(calls(cli))) == (workers, launched)

    if completion != "cancel-before-child":
        begin.set()
    monkeypatch.setattr("marabrain.crystalline.threading.Thread", GatedWorker)
    async with command_barrier(cli) as (arrived, gate):
        if completion == "launch-failure":
            (cli / "crystalline").unlink()
        elif completion == "nonzero":
            respond(cli, stdout="{}", exit_code=3)
        with anyio.fail_after(30):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(request)
                try:
                    if completion in ("success", "nonzero", "cancel"):
                        await arrived.receive()
                    assert await anyio.to_thread.run_sync(started.wait, 10)
                    if completion == "cancel-before-child":
                        await refused()
                        scope.cancel()
                        await anyio.wait_all_tasks_blocked()
                        begin.set()
                    elif completion == "cancel":
                        scope.cancel()
                    elif completion in ("success", "nonzero"):
                        gate.set()
                    assert await anyio.to_thread.run_sync(finished.wait, 10)
                    for record in calls(cli):
                        pid = record["pid"]
                        assert isinstance(pid, int)
                        with pytest.raises(ProcessLookupError):
                            os.kill(pid, 0)
                    await refused()
                finally:
                    begin.set()
                    release.set()
    expected = {"success": "ok", "nonzero": "exit code 3", "timeout": "may or may not have been applied", "launch-failure": "unavailable"}
    assert scope.cancelled_caught == completion.startswith("cancel")
    assert len(outcomes) == (0 if completion.startswith("cancel") else 1)
    assert not outcomes or expected[completion] in outcomes[0]
    if completion in ("cancel-before-child", "launch-failure"):
        assert calls(cli) == []
    assert_input_idle(context, 4)


async def test_aggregate_input_budget_is_shared_by_workspaces_and_not_by_applications(cli: Path) -> None:
    """Refuse a sibling workspace's payload while another holds the budget, without launching anything."""
    app = EngineContext.for_application(Settings(max_total_input_bytes=4))
    first, second = app.for_workspace("first", cli), app.for_workspace("second", cli)
    independent = EngineContext.for_application(Settings(max_total_input_bytes=4)).for_workspace("first", cli)

    async def occupy() -> None:
        """Hold the whole budget through a real child."""
        assert await run_json(["write", "held"], "abcd", context=first) == RECEIPT

    async with command_barrier(cli) as (arrived, release):
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(occupy)
                await arrived.receive()
                try:
                    workers = cli_workers()
                    with pytest.raises(ToolError, match="input capacity is exhausted"):
                        await run_json(["write", "other"], "x", context=second)
                    assert cli_workers() == workers and len(calls(cli)) == 1
                    assert await run_json(["write", "other"], "abcd", context=independent) == RECEIPT
                    assert await run_json(["write", "empty"], "", context=second) == RECEIPT
                finally:
                    release.set()
    assert_input_idle(app, 4)
    assert await run_json(["write", "after"], "abcd", context=second) == RECEIPT


async def test_input_budget_over_mcp_refuses_only_writes_and_across_workspaces(cli: Path, workspace_root: Path) -> None:
    """Share one budget between workspaces for create and edit while reads, deletes, searches, and health reserve nothing."""
    prepare_workspace("second", workspace_root)
    settings = Settings(max_total_input_bytes=4)
    context = EngineContext.for_application(settings)
    async with command_barrier(cli) as (arrived, release), Client(create_server(settings, context=context)) as client:
        for workspace in (WORKSPACE_ID, "second"):
            await client.call_tool("search_memories", {"workspace_id": workspace, "query": "ready", "domains": ["notes"]})
        (cli / "calls.jsonl").unlink()

        async def occupy() -> None:
            """Hold the whole budget through a real create in the first workspace."""
            await client.call_tool("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "title": "held", "content": "abcd"})

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(occupy)
                await arrived.receive()
                try:
                    for tool, arguments in (
                        ("create_memory", {"domain": "notes", "title": "other", "content": "x"}),
                        ("edit_memory", {"domain": "notes", "permalink": "one", "operation": {"type": "append", "content": "x"}}),
                    ):
                        with pytest.raises(ToolError, match="input capacity is exhausted"):
                            await client.call_tool(tool, {"workspace_id": "second", **arguments})
                    assert len(calls(cli)) == 1
                    for tool, arguments in (
                        ("read_memory", {"domain": "notes", "permalink": "one"}),
                        ("delete_memory", {"domain": "notes", "permalink": "one"}),
                        ("search_memories", {"domains": ["notes"], "query": "body"}),
                        ("create_memory", {"domain": "notes", "title": "empty", "content": ""}),
                    ):
                        await client.call_tool(tool, {"workspace_id": "second", **arguments})
                    assert [command[1] for command in argv(cli)[1:]] == ["read", "delete", "search", "write"]
                    assert (await client.call_tool("health")).structured_content == {"status": "ok", "version": "0.4.7"}
                finally:
                    release.set()
        assert_input_idle(context, 4)
        await client.call_tool("create_memory", {"workspace_id": "second", "domain": "notes", "title": "after", "content": "abcd"})


async def test_registry_commands_reserve_nothing_beside_a_full_budget(domain_cli: Path) -> None:
    """Register a new domain beside a full budget, refuse only its final write, and keep the registration."""
    settings = Settings(max_total_input_bytes=1)
    context = EngineContext.for_application(settings)
    async with command_barrier(domain_cli) as (arrived, release), Client(create_server(settings, context=context)) as client:
        await client.call_tool("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "seed", "title": "seed", "content": ""})
        (domain_cli / "calls.jsonl").unlink()

        async def occupy() -> None:
            """Hold the whole budget through a real create in the registered domain."""
            await client.call_tool("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "seed", "title": "held", "content": "a"})

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(occupy)
                await arrived.receive()
                try:
                    with pytest.raises(ToolError, match="input capacity is exhausted"):
                        await client.call_tool("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "fresh", "title": "one", "content": "x"})
                    assert [command[1:3] for command in argv(domain_cli)] == [["write", "seed"], ["domain", "list"], ["domain", "init"], ["domain", "add"]]
                    assert [row["stdin"] for row in calls(domain_cli)] == ["a", "", "", ""]
                finally:
                    release.set()
        assert_input_idle(context, 1)
        created = await client.call_tool("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "fresh", "title": "one", "content": "x"})
        assert created.structured_content == {"domain": "fresh", "permalink": "one"}


async def test_exhausted_input_budget_leaves_stdinless_data_and_control_commands_running(cli: Path) -> None:
    """Refuse only the payload that does not fit while no-stdin data and control children still run."""
    context = EngineContext.capture(max_transient_processes=3, max_total_input_bytes=4)

    async def occupy() -> None:
        """Hold the whole budget through a real child."""
        assert await run_json(["write", "held"], "abcd", context=context) == RECEIPT

    async with command_barrier(cli) as (arrived, release):
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(occupy)
                await arrived.receive()
                try:
                    with pytest.raises(ToolError, match="input capacity is exhausted"):
                        await run_json(["write", "other"], "x", context=context)
                    assert await run_json(["read", "one"], context=context) == RECEIPT
                    with await run_startup_command([sys.executable, "-c", "print('{}')"], context) as output:
                        assert output == b"{}\n"
                finally:
                    release.set()
    assert_input_idle(context, 4)


async def test_process_saturation_refuses_before_any_input_is_reserved(cli: Path) -> None:
    """Report a full data lane without charging the refused payload to the input budget."""
    context = EngineContext.capture(max_transient_processes=2, max_total_input_bytes=8)

    async def occupy() -> None:
        """Hold the only data slot and half the budget through a real child."""
        assert await run_json(["write", "held"], "abcd", context=context) == RECEIPT

    async with command_barrier(cli) as (arrived, release):
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(occupy)
                await arrived.receive()
                try:
                    workers = cli_workers()
                    with pytest.raises(ToolError, match="transient process capacity is exhausted"):
                        await run_json(["write", "other"], "xy", context=context)
                    assert cli_workers() == workers and len(calls(cli)) == 1
                    budget = context.transients.input_budget
                    assert budget is not None
                    budget.reserve(4)
                    with pytest.raises(InputBudgetError):
                        budget.reserve(1)
                    budget.release(4)
                finally:
                    release.set()
    assert_input_idle(context, 8)


@pytest.mark.parametrize("capture", ["per-child", "aggregate"])
async def test_capture_limit_failure_releases_input_and_input_refusal_charges_no_capture(cli: Path, capture: str) -> None:
    """Release stdin after a capture-limit failure and keep a refused payload out of the capture budget."""
    context = EngineContext.capture(
        max_capture_bytes=2 if capture == "per-child" else None, max_total_capture_bytes=2 if capture == "aggregate" else None, max_total_input_bytes=4
    )
    with pytest.raises(ToolError, match="capture limit"):
        await run_json(["write"], "abcd", context=context)
    assert_input_idle(context, 4)
    with pytest.raises(ToolError, match="input capacity is exhausted"):
        await run_json(["write"], "abcde", context=context)
    assert len(calls(cli)) == 1
    budget = context.transients.capture_budget
    if budget is not None:
        budget.reserve(2)
        budget.release(2)


async def test_aggregate_refusal_logs_resource_limit_without_arguments_or_content(cli: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Record the refusal under the resource_limit category while launching nothing and leaking nothing."""
    context = EngineContext.capture(max_total_input_bytes=2)
    caplog.set_level(logging.DEBUG, logger="marabrain.crystalline")
    with pytest.raises(ToolError, match="input capacity is exhausted"):
        await run_json(["write", "private-argument"], "private-content", context=context)
    assert "category=resource_limit" in caplog.text and "operation=write" in caplog.text and "elapsed_ms=" in caplog.text
    assert "private-argument" not in caplog.text and "private-content" not in caplog.text
    assert calls(cli) == []
