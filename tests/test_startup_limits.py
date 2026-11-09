"""Prevent captured startup resource failures from permitting engine initialization or serving."""

import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TextIO

import anyio
import pytest
from anyio.abc import Process
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memory_fake import WORKSPACE_ID
from pydantic import SecretStr
from test_engine_lifecycle import assert_reaped, await_file
from test_engine_lifecycle import runtime as runtime

from marabrain import server, workspaces
from marabrain.config import EngineMode, Settings, Transport
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import engine_lifecycle, run_startup_command
from marabrain.transient import CaptureLimitError, ProcessAdmissionError

LIMIT_ENGINE = """
import json, os, signal, sys
from pathlib import Path
root = Path(os.environ['ENGINE_TEST_ROOT'])
pid = os.getpid()
(root / f'child-{pid}').touch()
args = sys.argv[1:]
if args == ['ctl', 'status']:
    if os.environ.get('OVERFLOW_PHASE') == ('readiness' if (root / 'owner').exists() else 'probe'):
        print('secret-payload' * 1024)
    elif (root / 'owner').exists():
        print(json.dumps({'pid': int((root / 'owner').read_text())}))
    else:
        sys.exit(1)
elif args == ['serve', '--http', 'off']:
    (root / 'owner').write_text(str(pid))
    while True: signal.pause()
elif args == ['--json', 'domain', 'list']:
    print('{"domains":[]}')
else:
    print('{"private":"secret-payload"}')
"""


@pytest.mark.parametrize("phase", ["probe", "initialization", "readiness"])
@pytest.mark.parametrize("aggregate", [None, 64])
async def test_startup_overflow_fails_before_serving_and_reaps_owned_children(
    runtime: Path, monkeypatch: pytest.MonkeyPatch, phase: str, aggregate: int | None
) -> None:
    """Propagate overflow at every captured startup owner without treating it as daemon absence."""
    (runtime / "crystalline").write_text(f"#!{sys.executable}\n" + LIMIT_ENGINE)
    monkeypatch.setenv("OVERFLOW_PHASE", phase)
    context = EngineContext.capture(max_transient_processes=2, max_capture_bytes=None if aggregate else 64, max_total_capture_bytes=aggregate)
    original = anyio.open_process
    initialized = False

    async def initialize(
        command: Sequence[str],
        *,
        stdin: int | None = None,
        stdout: int | TextIO | None = subprocess.PIPE,
        stderr: int | TextIO | None = subprocess.PIPE,
        env: Mapping[str, str] | None = None,
    ) -> Process:
        """Use a real initializer child with controlled raw output and a recorded PID."""
        nonlocal initialized
        if command[0] == sys.executable:
            initialized = True
            output = "secret-payload" * 1024 if phase == "initialization" else "{}"
            command = [
                sys.executable,
                "-c",
                f"import os; from pathlib import Path; Path({str(runtime)!r}, 'child-' + str(os.getpid())).touch(); print({output!r})",
            ]
        return await original(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)

    monkeypatch.setattr(anyio, "open_process", initialize)
    failure = pytest.RaisesGroup(CaptureLimitError, flatten_subgroups=True) if phase == "readiness" else pytest.raises(CaptureLimitError)
    with anyio.fail_after(10), failure as error:
        async with engine_lifecycle(Settings(), context=context):
            pytest.fail("Overflow reached serving.")
    assert initialized == (phase != "probe")
    assert (runtime / "owner").exists() == (phase == "readiness")
    assert "secret-payload" not in str(error.value)
    assert_reaped(runtime)
    monkeypatch.setattr(anyio, "open_process", original)
    with await run_startup_command([sys.executable, "-c", "print(' ' * 63)"], context) as raw:
        assert len(raw) == 64


async def test_control_admission_failure_prevents_preexisting_probe_and_initialization(runtime: Path) -> None:
    """Fail managed preparation immediately when its control slot is already owned."""
    context = EngineContext.capture(max_transient_processes=2)
    with context.transients.admit(control=True), pytest.raises(ProcessAdmissionError):
        async with engine_lifecycle(Settings(), context=context):
            pytest.fail("Admission failure reached serving.")
    assert list(runtime.glob("child-*")) == []
    with await run_startup_command([sys.executable, "-c", "print('{}')"], context) as output:
        assert output == b"{}\n"


async def test_oversized_preexisting_probe_leaves_external_daemon_running(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse managed startup on overflow while preserving a separately owned daemon."""
    (runtime / "crystalline").write_text(f"#!{sys.executable}\n" + LIMIT_ENGINE)
    async with await anyio.open_process(["crystalline", "serve", "--http", "off"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as external:
        try:
            await await_file(runtime / "owner")
            monkeypatch.setenv("OVERFLOW_PHASE", "readiness")
            context = EngineContext.capture(max_total_capture_bytes=64)
            with pytest.raises(CaptureLimitError):
                async with engine_lifecycle(Settings(), context=context):
                    pytest.fail("Overflow adopted an external daemon.")
            assert external.returncode is None
            os.kill(external.pid, 0)
        finally:
            external.terminate()
            await external.wait()
    assert_reaped(runtime)


@pytest.mark.parametrize("aggregate", [None, 16])
async def test_programmatic_application_limits_share_context_through_preparation_and_serving(
    runtime: Path, monkeypatch: pytest.MonkeyPatch, aggregate: int | None
) -> None:
    """Retain entry-time limits and environment while binding the same owners to the MCP tools."""
    (runtime / "crystalline").write_text(f"#!{sys.executable}\n" + LIMIT_ENGINE)
    settings = Settings(
        transport=Transport.HTTP,
        bearer_secret=SecretStr("s" * 32),
        engine_mode=EngineMode.EXTERNAL,
        max_transient_processes=2,
        max_capture_bytes=16 if aggregate is None else 4096,
        max_total_capture_bytes=aggregate,
    )
    prepared: EngineContext | None = None
    served = False

    @asynccontextmanager
    async def prepare(manager: workspaces.WorkspaceManager, context: EngineContext, deadline: float | None) -> AsyncGenerator[None]:
        """Capture the application owner before ambient configuration changes."""
        nonlocal prepared
        assert context is not None
        prepared = context
        with await run_startup_command([sys.executable, "-c", "print('{}')"], context, deadline=deadline) as output:
            assert output == b"{}\n"
        monkeypatch.setenv("MARABRAIN_MAX_CAPTURE_BYTES", "4096")
        monkeypatch.setenv("MARABRAIN_MAX_TOTAL_CAPTURE_BYTES", "4096")
        monkeypatch.setenv("MARABRAIN_MAX_TRANSIENT_PROCESSES", "10")
        monkeypatch.setenv("PATH", str(runtime / "missing"))
        yield

    async def serve(settings: Settings, stdin: anyio.AsyncFile[str] | None = None, *, context: EngineContext | None = None) -> None:
        """Exercise the serving adapter with the startup owner's limits and snapshot."""
        nonlocal served
        assert context is not None
        async with Client(server.create_server(settings, context=context)) as client:
            with pytest.raises(ToolError, match="capture limit"):
                await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "plan"})
            assert prepared is not None
            assert prepared.transients is context.transients
            assert prepared.state_home != context.state_home
            with context.transients.admit(control=True):
                with pytest.raises(ToolError, match="daemon is not responding"):
                    await client.call_tool("health", {})
        served = True

    monkeypatch.setattr(workspaces.WorkspaceManager, "_engine", prepare)
    monkeypatch.setattr(server, "run_transport", serve)
    await server.run_application(settings)
    assert served
    assert_reaped(runtime)
