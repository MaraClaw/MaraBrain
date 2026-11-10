"""Keep lifecycle captures charged through their actual parser and free initialization before launch."""

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

import anyio
import pytest
from anyio.abc import Process
from pydantic import TypeAdapter, ValidationError
from test_engine_lifecycle import assert_reaped
from test_engine_lifecycle import runtime as runtime
from test_startup_limits import LIMIT_ENGINE

from marabrain import lifecycle
from marabrain.config import Settings
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import EngineStatus, engine_lifecycle, probe_engine, probe_owned_engine, run_startup_command, stop_process
from marabrain.transient import CaptureLimitError, StdoutCapture


async def test_successful_preexisting_probe_discards_its_raw_output(cli: Path) -> None:
    """Release status-only output immediately after reaping rather than retaining it across probes."""
    (cli / "crystalline").write_text(f"#!{sys.executable}\nprint('{{}}', end='')\n")
    context = EngineContext.capture(max_total_capture_bytes=2)
    assert await probe_engine(2, context=context)
    assert await probe_engine(2, context=context)
    assert await probe_engine(2, context=context)


@pytest.mark.parametrize("malformed", [False, True])
async def test_readiness_json_keeps_ownership_until_the_real_pid_parse(cli: Path, monkeypatch: pytest.MonkeyPatch, malformed: bool) -> None:
    """Charge readiness bytes during identity parsing and free them on either parse outcome."""
    context = EngineContext.capture(max_total_capture_bytes=64)
    aliases: list[bytearray] = []

    class ObservedStatus:
        """Observe raw ownership while delegating identity validation to the real model."""

        @staticmethod
        def model_validate_json(raw: bytearray) -> EngineStatus:
            """Refuse another byte at the exact readiness consumption boundary."""
            aliases.append(raw)
            assert len(raw) == 64
            with pytest.raises(CaptureLimitError):
                StdoutCapture(None, context.transients.capture_budget).append(b"x")
            return EngineStatus.model_validate_json(raw)

    monkeypatch.setattr(lifecycle, "EngineStatus", ObservedStatus)
    process = await anyio.open_process([sys.executable, "-c", "import signal; signal.pause()"], stdout=subprocess.DEVNULL)
    try:
        payload = (b"invalid" if malformed else json.dumps({"pid": process.pid}).encode()).ljust(64)
        (cli / "crystalline").write_text(f"#!{sys.executable}\nimport os\nos.write(1, {payload!r})\n")
        if malformed:
            with pytest.raises(ValidationError):
                await probe_owned_engine(process, 2, context=context)
        else:
            assert await probe_owned_engine(process, 2, context=context)
        assert aliases == [bytearray()]
        with await run_startup_command([sys.executable, "-c", "print(' ' * 63)"], context) as raw:
            assert len(raw) == 64
    finally:
        await stop_process(process)


@pytest.mark.parametrize("malformed", [False, True])
async def test_initializer_charge_spans_overlay_parse_but_ends_before_daemon_launch(runtime: Path, monkeypatch: pytest.MonkeyPatch, malformed: bool) -> None:
    """Retain initializer output through strict parsing and clear it before readiness or serving can compete."""
    (runtime / "crystalline").write_text(f"#!{sys.executable}\n" + LIMIT_ENGINE)
    context = EngineContext.capture(max_total_capture_bytes=64)
    settings = Settings()
    payload = (b"invalid" if malformed else b'{"EMBEDDING_API_KEY":"resolved"}').ljust(64)
    aliases: list[bytearray] = []
    original_launch = anyio.open_process
    original_parse = TypeAdapter[dict[str, str]].validate_json
    served = False

    def parse(adapter: TypeAdapter[dict[str, str]], raw: bytearray, *, strict: bool) -> dict[str, str]:
        """Attempt competing retention while the real overlay parser still owns its raw input."""
        aliases.append(raw)
        assert raw == payload
        with pytest.raises(CaptureLimitError):
            StdoutCapture(None, context.transients.capture_budget).append(b"x")
        return original_parse(adapter, raw, strict=strict)

    async def launch(
        command: Sequence[str],
        *,
        stdin: int | None = None,
        stdout: int | TextIO | None = subprocess.PIPE,
        stderr: int | TextIO | None = subprocess.PIPE,
        env: Mapping[str, str] | None = None,
    ) -> Process:
        """Run a real initializer and verify released storage immediately before real daemon launch."""
        if command[0] == sys.executable:
            command = [sys.executable, "-c", f"import os; os.write(1, {payload!r})"]
        elif list(command) == ["crystalline", "serve", "--http", "off"]:
            assert aliases == [bytearray()]
            assert env is not None and env["EMBEDDING_API_KEY"] == "resolved"
            raw_capture = StdoutCapture(None, context.transients.capture_budget)
            with raw_capture:
                raw_capture.append(b"x" * 64)
        return await original_launch(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)

    monkeypatch.setattr(TypeAdapter, "validate_json", parse)
    monkeypatch.setattr(anyio, "open_process", launch)
    if malformed:
        with pytest.raises(ValidationError):
            async with engine_lifecycle(settings, context=context):
                pytest.fail("Malformed initializer output reached serving.")
    else:
        async with engine_lifecycle(settings, context=context):
            served = True
            assert aliases == [bytearray()]
            capture = StdoutCapture(None, context.transients.capture_budget)
            with capture:
                capture.append(b"x" * 64)
    assert served == (not malformed)
    assert aliases == [bytearray()]
    assert_reaped(runtime)
