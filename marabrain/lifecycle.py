"""Own a foreground Crystalline daemon for one application invocation."""

import os
import signal
import subprocess
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import assert_never

import anyio
from anyio.abc import Process
from pydantic import BaseModel, Field, TypeAdapter

from marabrain.config import EngineMode, Settings


class EngineLifecycleError(RuntimeError):
    """Failure to start or retain the exclusively owned engine."""


class EngineStatus(BaseModel):
    """Process identity returned by the pinned Crystalline control client."""

    pid: int = Field(strict=True, gt=0)


async def probe_engine(timeout: float) -> bool:
    """Return whether the status command succeeds within its allotted duration."""
    try:
        with anyio.fail_after(timeout):
            await anyio.run_process(["crystalline", "ctl", "status"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError, subprocess.CalledProcessError, TimeoutError:
        return False
    return True


async def stop_process(process: Process) -> None:
    """Terminate and reap an owned child, escalating after three seconds."""
    with anyio.CancelScope(shield=True):
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                await process.wait()
            with anyio.move_on_after(3):
                await process.wait()
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    await process.wait()
        await process.wait()
        await process.aclose()


async def watch_engine(process: Process) -> None:
    """Fail the application when its daemon exits unexpectedly."""
    await process.wait()
    raise EngineLifecycleError("Owned Crystalline daemon exited unexpectedly.")


async def probe_owned_engine(process: Process, timeout: float) -> bool:
    """Accept readiness only from the live foreground child this application launched."""
    try:
        with anyio.fail_after(timeout):
            status = await anyio.run_process(["crystalline", "ctl", "status"], stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError, subprocess.CalledProcessError, TimeoutError:
        return False
    identity = EngineStatus.model_validate_json(status.stdout)
    if identity.pid != process.pid:
        raise EngineLifecycleError("Managed mode refuses a competing Crystalline daemon.")
    await anyio.lowlevel.checkpoint()
    if process.returncode is not None:
        raise EngineLifecycleError("Owned Crystalline daemon exited during startup.")
    return True


async def cancel_on_signal(scope: anyio.CancelScope, signals: AsyncIterator[int]) -> None:
    """Cancel application work on the first termination or interrupt signal."""
    async for _ in signals:
        scope.cancel()
        return


@asynccontextmanager
async def engine_lifecycle(settings: Settings) -> AsyncGenerator[None]:
    """Start and supervise an engine only when managed mode is explicitly selected."""
    mode = settings.engine_mode
    match mode:
        case EngineMode.EXTERNAL:
            yield
            return
        case EngineMode.MANAGED:
            async with managed_engine(settings):
                yield
            return
    assert_never(mode)


@asynccontextmanager
async def managed_engine(settings: Settings) -> AsyncGenerator[None]:
    """Initialize and retain one daemon under the exclusive HOME/XDG precondition."""

    if await probe_engine(settings.health_timeout_seconds):
        raise EngineLifecycleError("Managed mode refuses a responsive preexisting Crystalline daemon.")

    # A subprocess makes blocking provider initialization cancellable without an
    # abandoned thread that could later write configuration after shutdown.
    initialized = await anyio.run_process(
        [
            sys.executable,
            "-c",
            "import json; from marabrain.embeddings import initialize_embeddings; print(json.dumps(initialize_embeddings()))",
        ],
        stdin=subprocess.DEVNULL,
        stderr=sys.stderr,
    )
    overlay = TypeAdapter(dict[str, str]).validate_json(initialized.stdout, strict=True)
    deadline = anyio.current_time() + settings.engine_startup_timeout_seconds
    with anyio.CancelScope(shield=True) as launch:
        process = await anyio.open_process(
            ["crystalline", "serve", "--http", "off"],
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            env={**os.environ, **overlay},
        )
        try:
            launch.shield = False
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(watch_engine, process)
                with anyio.fail_after(max(0, deadline - anyio.current_time())):
                    while True:
                        remaining = deadline - anyio.current_time()
                        if await probe_owned_engine(process, min(settings.health_timeout_seconds, max(0, remaining))):
                            break
                        await anyio.sleep(min(0.05, max(0, deadline - anyio.current_time())))
                try:
                    yield
                finally:
                    tasks.cancel_scope.cancel()
        finally:
            await stop_process(process)


@asynccontextmanager
async def application_signals() -> AsyncGenerator[None]:
    """Handle TERM and INT throughout initialization, serving, and cleanup."""
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(cancel_on_signal, tasks.cancel_scope, signals)
            try:
                yield
            finally:
                tasks.cancel_scope.cancel()
