"""Keep lifecycle captures charged through their actual parser."""

import json
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from marabrain import lifecycle
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import EngineStatus, probe_engine, probe_owned_engine, run_startup_command, stop_process
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
