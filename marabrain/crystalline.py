"""Asynchronous adapter that runs the Crystalline command line and returns its JSON output."""

import logging
import subprocess
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future

import anyio
from fastmcp.exceptions import ToolError
from pydantic import JsonValue, TypeAdapter, ValidationError

from marabrain.command_io import communicate
from marabrain.deadlines import check_deadline
from marabrain.engine_context import EngineContext
from marabrain.transient import StdoutCapture, TransientLimitError

JsonObject = dict[str, JsonValue]
_OUTPUT = TypeAdapter(JsonObject)
_LOGGER = logging.getLogger(__name__)
_OPERATIONS = frozenset({"write", "read", "edit", "delete", "search"})


async def _run_command(arguments: Sequence[str], data: bytes, context: EngineContext, deadline: float) -> subprocess.CompletedProcess[bytearray]:
    """Retain ownership through launch cancellation and reap the CLI before returning."""
    result = Future[subprocess.CompletedProcess[bytearray]]()
    cancelled = threading.Event()
    finished = anyio.Event()
    token = anyio.lowlevel.current_token()
    process: subprocess.Popen[bytes] | None = None

    def execute() -> None:
        """Own process creation and communication until the child has been reaped."""
        nonlocal process
        try:
            anyio.from_thread.run_sync(check_deadline, deadline, token=token)
            if cancelled.is_set():
                result.cancel()
                return
            with subprocess.Popen(
                ["crystalline", "--json", *arguments],
                stdin=subprocess.PIPE if data else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=context.environment,
            ) as child:
                process = child
                try:
                    if cancelled.is_set():
                        child.kill()
                    output = communicate(child, data, StdoutCapture(context.transients.max_capture_bytes))
                    result.set_result(subprocess.CompletedProcess(child.args, child.returncode, output))
                finally:
                    if child.poll() is None:
                        try:
                            child.kill()
                        except ProcessLookupError:
                            child.poll()
                    child.wait()
        except BaseException as error:
            # Transfer worker failures unchanged to the async boundary.
            result.set_exception(error)
        finally:
            anyio.from_thread.run_sync(finished.set, token=token)

    await anyio.lowlevel.checkpoint()
    check_deadline(deadline)
    with context.transients.admit():
        worker = threading.Thread(target=execute, name="marabrain-cli")
        worker.start()
        try:
            await finished.wait()
            return result.result()
        finally:
            cancelled.set()
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    process.poll()
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(worker.join)


async def run_json(
    arguments: Sequence[str], content: str | None = None, timeout_seconds: float = 30.0, *, context: EngineContext | None = None, deadline: float | None = None
) -> JsonObject:
    """Run `crystalline --json` once and return its stdout JSON object unchanged.

    Content is sent on stdin, stderr is discarded, and failures raise fixed messages.
    A timeout or crash during a mutation does not promise that nothing was applied.
    """
    operation = arguments[0] if arguments and arguments[0] in _OPERATIONS else "unknown"
    started = time.monotonic()
    _LOGGER.debug("crystalline.start operation=%s", operation)
    data = (content or "").encode()
    captured = context if context is not None else EngineContext.capture()
    try:
        check_deadline(deadline)
        cap = anyio.current_time() + timeout_seconds
        limit = cap if deadline is None else min(cap, deadline)
        with anyio.fail_after(limit - anyio.current_time()):
            check_deadline(limit)
            result = await _run_command(arguments, data, captured, limit)
            check_deadline(limit)
    except TimeoutError as error:
        _LOGGER.error("crystalline.failure operation=%s category=timeout elapsed_ms=%.1f", operation, (time.monotonic() - started) * 1000)
        raise ToolError(f"Crystalline did not answer within {timeout_seconds:g} seconds; a mutation may or may not have been applied.") from error
    except OSError as error:
        _LOGGER.error("crystalline.failure operation=%s category=unavailable elapsed_ms=%.1f", operation, (time.monotonic() - started) * 1000)
        raise ToolError("Crystalline is unavailable.") from error
    except TransientLimitError as error:
        _LOGGER.error("crystalline.failure operation=%s category=resource_limit elapsed_ms=%.1f", operation, (time.monotonic() - started) * 1000)
        raise ToolError(str(error)) from None
    if result.returncode != 0:
        _LOGGER.error(
            "crystalline.failure operation=%s category=exit elapsed_ms=%.1f exit_code=%d", operation, (time.monotonic() - started) * 1000, result.returncode
        )
        raise ToolError(f"Crystalline command failed with exit code {result.returncode}.")
    try:
        output = _OUTPUT.validate_json(result.stdout)
    except ValidationError:
        _LOGGER.error("crystalline.failure operation=%s category=malformed_output elapsed_ms=%.1f", operation, (time.monotonic() - started) * 1000)
        raise ToolError("Crystalline returned malformed output.") from None
    _LOGGER.info("crystalline.complete operation=%s elapsed_ms=%.1f", operation, (time.monotonic() - started) * 1000)
    return output
