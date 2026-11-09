"""Keep real subprocess output charged through cleanup, cancelled handoff, and JSON consumption."""

import os
import sys
import threading
from pathlib import Path

import anyio
import pytest
from anyio.abc import Process
from fastmcp.exceptions import ToolError

from marabrain import crystalline, lifecycle
from marabrain.crystalline import JsonObject, run_json
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import run_startup_command
from marabrain.transient import CaptureLimitError, StdoutCapture


@pytest.mark.parametrize("control", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_capture_stays_charged_until_worker_or_stream_cleanup_and_consumption(
    cli: Path, monkeypatch: pytest.MonkeyPatch, control: bool, cancel: bool
) -> None:
    """Refuse competing output after child exit and discard a cancelled, undelivered worker result."""
    (cli / "crystalline").write_text(f"#!{sys.executable}\nimport os\nos.write(1, b'{{}}')\n")
    context = EngineContext.capture(max_transient_processes=3, max_total_capture_bytes=2)
    cleanup, release = threading.Event(), threading.Event()
    original_thread = threading.Thread
    original_stop = lifecycle.stop_process
    scope = anyio.CancelScope()
    captures: list[StdoutCapture] = []
    append = StdoutCapture.append
    pids: list[int] = []

    def observe(capture: StdoutCapture, chunk: bytes) -> None:
        """Retain an alias to prove that released bytes no longer remain in owned storage."""
        append(capture, chunk)
        if capture not in captures:
            captures.append(capture)

    class GatedWorker(original_thread):
        """Keep the actual CLI worker live after it has reaped its real child."""

        def run(self) -> None:
            """Pause worker exit after the subprocess runner has produced its result."""
            super().run()
            if self.name == "marabrain-cli":
                cleanup.set()
                release.wait(timeout=10)

    async def gated_stop(process: Process) -> None:
        """Hold final stream cleanup after the real child has been reaped."""
        pids.append(process.pid)
        await original_stop(process)
        cleanup.set()
        with anyio.CancelScope(shield=True):
            assert await anyio.to_thread.run_sync(release.wait, 10)

    async def request() -> None:
        """Run a capture whose completed output can be stranded by cancellation."""
        with scope:
            if control:
                with await run_startup_command([str(cli / "crystalline")], context) as raw:
                    assert raw == b"{}"
            else:
                assert await run_json(["read"], context=context) == {}

    monkeypatch.setattr(StdoutCapture, "append", observe)
    monkeypatch.setattr(lifecycle, "stop_process", gated_stop if control else original_stop)
    monkeypatch.setattr(crystalline.threading, "Thread", original_thread if control else GatedWorker)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(request)
            try:
                assert await anyio.to_thread.run_sync(cleanup.wait, 5)
                assert captures[0].output == b"{}"
                contender = StdoutCapture(None, context.transients.capture_budget)
                with pytest.raises(CaptureLimitError):
                    contender.append(b"x")
                if cancel:
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()
            finally:
                release.set()
    assert scope.cancelled_caught == cancel
    assert captures[0].output == b""
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not [worker for worker in threading.enumerate() if worker.name == "marabrain-cli"]
    monkeypatch.setattr(lifecycle, "stop_process", original_stop)
    monkeypatch.setattr(crystalline.threading, "Thread", original_thread)
    assert await run_json(["read"], context=context) == {}


@pytest.mark.parametrize("payload", [b"{}", b"no"])
async def test_data_json_consumption_retains_bytes_until_parse_or_failure(cli: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    """Keep the raw payload charged inside the real validator and release it after either parse outcome."""
    (cli / "crystalline").write_text(f"#!{sys.executable}\nimport os\nos.write(1, {payload!r})\n")
    context = EngineContext.capture(max_total_capture_bytes=2)
    original = crystalline._OUTPUT.validate_json
    aliases: list[bytearray] = []

    def validate(raw: bytearray) -> JsonObject:
        """Compete for bytes at the consumption boundary, before invoking the real parser."""
        aliases.append(raw)
        assert raw == payload
        with pytest.raises(CaptureLimitError):
            StdoutCapture(None, context.transients.capture_budget).append(b"x")
        return original(raw)

    monkeypatch.setattr(crystalline._OUTPUT, "validate_json", validate)
    if payload == b"{}":
        assert await run_json(["read"], context=context) == {}
    else:
        with pytest.raises(ToolError, match="malformed output"):
            await run_json(["read"], context=context)
    assert aliases == [bytearray()]
    monkeypatch.setattr(crystalline._OUTPUT, "validate_json", original)
    (cli / "crystalline").write_text(f"#!{sys.executable}\nprint('{{}}', end='')\n")
    assert await run_json(["read"], context=context) == {}


async def test_control_output_stays_owned_after_child_cleanup_until_discard() -> None:
    """Keep a returned startup buffer charged until its explicit consumer disposes it."""
    context = EngineContext.capture(max_total_capture_bytes=2)
    held = await run_startup_command([sys.executable, "-c", "print('{}', end='')"], context)
    with held as raw:
        with pytest.raises(CaptureLimitError):
            await run_startup_command([sys.executable, "-c", "print('x', end='')"], context)
        assert raw == b"{}"
    assert raw == b""
    with await run_startup_command([sys.executable, "-c", "print('{}', end='')"], context) as reused:
        assert reused == b"{}"
