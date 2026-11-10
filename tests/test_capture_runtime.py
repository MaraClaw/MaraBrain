"""Exercise raw stdout ceilings and duplex pipes through real transient children."""

import os
import subprocess
import sys
import threading
from pathlib import Path

import anyio
import pytest
from fastmcp.exceptions import ToolError

from marabrain.crystalline import run_json
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import run_startup_command
from marabrain.transient import READ_BYTES, CaptureLimitError, StdoutCapture


@pytest.mark.parametrize("control", [False, True])
@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("aggregate", ["disabled", "alone", "combined"])
async def test_capture_counts_raw_multibyte_boundary_and_reuses_slot(cli: Path, control: bool, extra: int, aggregate: str) -> None:
    """Accept exactly S raw bytes and reject S+1 even when a UTF-8 character crosses reads."""
    payload = '{"content":"' + "x" * (READ_BYTES - 13) + '\u00fc"}'
    raw = payload.encode()
    (cli / "crystalline").write_text(f"#!{sys.executable}\nimport os\ndata = {raw!r} + b' ' * {extra}\nos.write(1, data[:65536])\nos.write(1, data[65536:])\n")
    context = EngineContext.capture(
        max_transient_processes=2,
        max_capture_bytes=None if aggregate == "alone" else len(raw),
        max_total_capture_bytes=None if aggregate == "disabled" else len(raw),
    )
    if extra:
        with pytest.raises(CaptureLimitError if control else ToolError, match="capture limit"):
            if control:
                await run_startup_command([str(cli / "crystalline")], context)
            else:
                await run_json(["read"], context=context)
    elif control:
        with await run_startup_command([str(cli / "crystalline")], context) as output:
            assert output == raw
    else:
        assert await run_json(["read"], context=context) == {"content": "x" * (READ_BYTES - 13) + "\u00fc"}
    (cli / "crystalline").write_text(f"#!{sys.executable}\nprint('{{}}', end='')\n")
    assert await run_json(["read"], context=context) == {}


@pytest.mark.parametrize("control", [False, True])
async def test_continuous_overflow_reaps_before_eof_without_payload_diagnostics(
    cli: Path, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, control: bool
) -> None:
    """Stop infinite output at the raw ceiling, release capacity and retain only bounded storage."""
    (cli / "crystalline").write_text(
        f"#!{sys.executable}\nimport os\nfrom pathlib import Path\n"
        f"Path({str(cli / 'pid')!r}).write_text(str(os.getpid()))\n"
        "os.write(1, b'{}')\nwhile True: os.write(1, b'secret-payload' * 8192)\n"
    )
    retained: list[tuple[int, int, int]] = []
    append = StdoutCapture.append

    def observe(capture: StdoutCapture, chunk: bytes) -> None:
        """Record byte counts and allocation only, including the rejected read."""
        try:
            append(capture, chunk)
        finally:
            retained.append((capture.retained_bytes, capture.output.__alloc__(), capture.peak_read_bytes))

    monkeypatch.setattr(StdoutCapture, "append", observe)
    context = EngineContext.capture(max_transient_processes=2, max_capture_bytes=1024)
    with anyio.fail_after(5), pytest.raises(CaptureLimitError if control else ToolError, match="capture limit") as error:
        if control:
            await run_startup_command([str(cli / "crystalline")], context)
        else:
            await run_json(["delete"], context=context)
    with pytest.raises(ProcessLookupError):
        os.kill(int((cli / "pid").read_text()), 0)
    assert retained and all(size <= 1024 and allocated <= 1024 + 64 and read <= READ_BYTES for size, allocated, read in retained)
    assert "secret-payload" not in str(error.value) + caplog.text + "".join(capfd.readouterr())
    assert not [worker for worker in threading.enumerate() if worker.name == "marabrain-cli"]
    (cli / "crystalline").write_text(f"#!{sys.executable}\nprint('{{}}')\n")
    assert await run_json(["read"], context=context) == {}


@pytest.mark.parametrize("close_stdin", [False, True])
@pytest.mark.parametrize("aggregate", [None, 300000])
async def test_bounded_capture_preserves_duplex_pressure_and_early_stdin_close(cli: Path, close_stdin: bool, aggregate: int | None) -> None:
    """Drain pipe-filling stdout while sending large stdin or handling its early closure."""
    (cli / "crystalline").write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"early = {close_stdin!r}\n"
        "if early: os.close(0)\n"
        "os.write(1, b' ' * 262144)\n"
        "content = '' if early else sys.stdin.read()\n"
        "print(json.dumps({'received': len(content)}))\n"
    )
    context = EngineContext.capture(max_transient_processes=2, max_capture_bytes=300000, max_total_capture_bytes=aggregate)
    with anyio.fail_after(10):
        assert await run_json(["write"], "x" * 1048576, context=context) == {"received": 0 if close_stdin else 1048576}


async def test_overflow_while_large_stdin_is_pending_finishes_worker(cli: Path) -> None:
    """Reject an overflowing child that never reads its input without stranding a writer."""
    (cli / "crystalline").write_text(f"#!{sys.executable}\nimport os\nwhile True: os.write(1, b'x' * 65536)\n")
    context = EngineContext.capture(max_transient_processes=2, max_capture_bytes=1024)
    with anyio.fail_after(5), pytest.raises(ToolError, match="capture limit"):
        await run_json(["write"], "x" * 1048576, context=context)
    assert not [worker for worker in threading.enumerate() if worker.name == "marabrain-cli"]


@pytest.mark.parametrize("control", [False, True])
@pytest.mark.parametrize("aggregate", [None, 2])
async def test_exact_capture_still_requires_eof_and_successful_exit(cli: Path, control: bool, aggregate: int | None) -> None:
    """Refuse a complete JSON prefix at S when its child exits unsuccessfully."""
    (cli / "crystalline").write_text(f"#!{sys.executable}\nimport os\nos.write(1, b'{{}}')\nraise SystemExit(7)\n")
    context = EngineContext.capture(max_capture_bytes=2, max_total_capture_bytes=aggregate)
    if control:
        with pytest.raises(subprocess.CalledProcessError):
            await run_startup_command([str(cli / "crystalline")], context)
    else:
        with pytest.raises(ToolError, match="exit code 7"):
            await run_json(["read"], context=context)
    (cli / "crystalline").write_text(f"#!{sys.executable}\nprint('{{}}', end='')\n")
    assert await run_json(["read"], context=context) == {}
