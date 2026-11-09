"""Prove immediate application-owned admission with barrier-controlled real children."""

import os
import sys
import threading
from pathlib import Path

import anyio
import pytest
from anyio.abc import Process, SocketAttribute, SocketStream
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memory_fake import RECEIPT, WORKSPACE_ID, calls
from test_domain_resolution import command_barrier

from marabrain import lifecycle
from marabrain.config import Settings
from marabrain.crystalline import run_json
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import run_startup_command
from marabrain.server import create_server
from marabrain.transient import ProcessAdmissionError


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
                    assert (await client.call_tool("health")).structured_content == {"status": "ok", "version": "0.4.5"}
                    assert (
                        await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "other", "permalink": "one"})
                    ).structured_content == RECEIPT
                    assert len(calls(cli)) == 3
                finally:
                    release.set()
