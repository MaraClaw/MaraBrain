"""Own a foreground Crystalline daemon for one application invocation."""

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
from marabrain.engine_context import EngineContext
from marabrain.transient import READ_BYTES, StdoutCapture


class EngineLifecycleError(RuntimeError):
    """Failure to start or retain the exclusively owned engine."""


class EngineStatus(BaseModel):
    """Process identity returned by the pinned Crystalline control client."""

    pid: int = Field(strict=True, gt=0)


def startup_remaining(deadline: float | None) -> float | None:
    """Return the remaining preparation budget, refusing further work after expiry."""
    if deadline is None:
        return None
    remaining = deadline - anyio.current_time()
    if remaining <= 0:
        raise TimeoutError("Application startup deadline expired.")
    return remaining


async def run_startup_command(command: list[str], context: EngineContext, *, deadline: float | None = None, initialization: bool = False) -> bytearray:
    """Capture one startup command and reap its child even when cancellation crosses launch."""
    await anyio.lowlevel.checkpoint()
    with anyio.fail_after(startup_remaining(deadline)), context.transients.admit(control=True):
        output = StdoutCapture(context.transients.max_capture_bytes)
        with anyio.CancelScope(shield=True) as launch:
            process = await anyio.open_process(
                command, stdin=subprocess.DEVNULL, stderr=sys.stderr if initialization else subprocess.DEVNULL, env=context.environment
            )
            try:
                launch.shield = False
                await anyio.lowlevel.checkpoint()
                startup_remaining(deadline)
                assert process.stdout is not None
                while True:
                    try:
                        output.append(await process.stdout.receive(READ_BYTES))
                    except anyio.EndOfStream:
                        break
                code = await process.wait()
                if code != 0:
                    raise subprocess.CalledProcessError(code, command)
            finally:
                await stop_process(process)
    return output.finish()


async def probe_engine(timeout: float, *, context: EngineContext | None = None, deadline: float | None = None) -> bool:
    """Return whether the status command succeeds within its allotted duration."""
    try:
        command_deadline = anyio.current_time() + timeout
        await run_startup_command(
            ["crystalline", "ctl", "status"],
            context if context is not None else EngineContext.capture(),
            deadline=command_deadline if deadline is None else min(deadline, command_deadline),
        )
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


async def probe_owned_engine(process: Process, timeout: float, *, context: EngineContext | None = None, deadline: float | None = None) -> bool:
    """Accept readiness only from the live foreground child this application launched."""
    try:
        command_deadline = anyio.current_time() + timeout
        status = await run_startup_command(
            ["crystalline", "ctl", "status"],
            context if context is not None else EngineContext.capture(),
            deadline=command_deadline if deadline is None else min(deadline, command_deadline),
        )
    except OSError, subprocess.CalledProcessError, TimeoutError:
        return False
    identity = EngineStatus.model_validate_json(status)
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
async def engine_lifecycle(settings: Settings, *, context: EngineContext | None = None, deadline: float | None = None) -> AsyncGenerator[None]:
    """Start and supervise an engine only when managed mode is explicitly selected."""
    bound_context = context if context is not None else EngineContext.for_application(settings)
    mode = settings.engine_mode
    match mode:
        case EngineMode.EXTERNAL:
            yield
            return
        case EngineMode.MANAGED:
            async with managed_engine(settings, context=bound_context, deadline=deadline):
                yield
            return
    assert_never(mode)


@asynccontextmanager
async def managed_engine(settings: Settings, *, context: EngineContext | None = None, deadline: float | None = None) -> AsyncGenerator[None]:
    """Initialize and retain one daemon under the exclusive HOME/XDG precondition."""

    bound_context = context if context is not None else EngineContext.for_application(settings)
    startup_remaining(deadline)
    if await probe_engine(settings.health_timeout_seconds, context=bound_context, deadline=deadline):
        raise EngineLifecycleError("Managed mode refuses a responsive preexisting Crystalline daemon.")

    # A subprocess makes blocking provider initialization cancellable without an
    # abandoned thread that could later write configuration after shutdown.
    initialized = await run_startup_command(
        [
            sys.executable,
            "-c",
            "import json; from marabrain.embeddings import initialize_embeddings; print(json.dumps(initialize_embeddings()))",
        ],
        bound_context,
        deadline=deadline,
        initialization=True,
    )
    overlay = TypeAdapter(dict[str, str]).validate_json(initialized, strict=True)
    await anyio.lowlevel.checkpoint()
    startup_remaining(deadline)
    readiness_deadline = anyio.current_time() + settings.engine_startup_timeout_seconds
    if deadline is not None:
        readiness_deadline = min(readiness_deadline, deadline)
    with anyio.CancelScope(shield=True) as launch:
        process = await anyio.open_process(
            ["crystalline", "serve", "--http", "off"],
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            env={**bound_context.environment, **overlay},
        )
        try:
            launch.shield = False
            await anyio.lowlevel.checkpoint()
            startup_remaining(readiness_deadline)
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(watch_engine, process)
                with anyio.fail_after(startup_remaining(readiness_deadline)):
                    while True:
                        if await probe_owned_engine(process, settings.health_timeout_seconds, context=bound_context, deadline=readiness_deadline):
                            break
                        await anyio.sleep(min(0.05, max(0, readiness_deadline - anyio.current_time())))
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
