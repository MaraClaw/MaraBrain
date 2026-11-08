"""Reap children when startup cancellation or expiry crosses subprocess launch."""

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import TextIO

import anyio
import pytest
from anyio.abc import Process
from test_engine_lifecycle import assert_reaped, await_file
from test_engine_lifecycle import runtime as runtime

from marabrain.config import Settings
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import engine_lifecycle, run_startup_command


@pytest.mark.parametrize("phase", ["client", "daemon"])
@pytest.mark.parametrize("interrupt", ["cancel", "expire"])
@pytest.mark.asyncio
async def test_interrupted_launch_handoff_reaps_child_before_next_command(runtime: Path, monkeypatch: pytest.MonkeyPatch, phase: str, interrupt: str) -> None:
    """Acquire the real launch handle under shielding and clean up before dispatching again."""
    open_process = anyio.open_process
    current_time = anyio.current_time
    context = EngineContext.capture()
    deadline = current_time() + 30
    owned: list[int] = []
    client = [
        sys.executable,
        "-c",
        "import os,signal; from pathlib import Path; Path(os.environ['ENGINE_TEST_ROOT'], 'client-started').touch(); signal.pause()",
    ]
    target = client if phase == "client" else ["crystalline", "serve", "--http", "off"]
    with anyio.CancelScope() as cancellation:

        async def interrupt_launch(
            command: Sequence[str],
            *,
            stdin: int | None = None,
            stdout: int | TextIO | None = subprocess.PIPE,
            stderr: int | TextIO | None = subprocess.PIPE,
            env: Mapping[str, str] | None = None,
        ) -> Process:
            """Interrupt after OS child creation and before its handle reaches the lifecycle owner."""
            process = await open_process(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
            if list(command) == target:
                owned.append(process.pid)
                await await_file(runtime / ("client-started" if phase == "client" else "owner"))
                if interrupt == "cancel":
                    cancellation.cancel()
                else:
                    monkeypatch.setattr(anyio, "current_time", lambda: current_time() + 60)
                await anyio.lowlevel.checkpoint()
            return process

        monkeypatch.setattr(anyio, "open_process", interrupt_launch)
        with pytest.raises(TimeoutError) if interrupt == "expire" else nullcontext():
            if phase == "client":
                await run_startup_command(client, context, deadline=deadline)
            else:
                async with engine_lifecycle(Settings(), context=context, deadline=deadline):
                    pytest.fail("Interrupted launch reached serving.")
    assert cancellation.cancelled_caught == (interrupt == "cancel")
    assert len(owned) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(owned[0], 0)
    assert not (runtime / "checked-app").exists() or phase == "daemon"
    if phase == "daemon":
        assert len(list(runtime.glob("child-*"))) == 2
    assert_reaped(runtime)


@pytest.mark.asyncio
async def test_expired_startup_dispatches_no_engine_command(runtime: Path) -> None:
    """Reject an already expired preparation budget before probing or launching any child."""
    with pytest.raises(TimeoutError):
        async with engine_lifecycle(Settings(), deadline=anyio.current_time() - 1):
            pytest.fail("Expired preparation reached serving.")
    assert list(runtime.glob("child-*")) == []
