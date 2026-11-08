"""Asynchronous adapter that runs the Crystalline command line and returns its JSON output."""

import logging
import subprocess
import time
from collections.abc import Sequence

import anyio
from fastmcp.exceptions import ToolError
from pydantic import JsonValue, TypeAdapter, ValidationError

JsonObject = dict[str, JsonValue]
_OUTPUT = TypeAdapter(JsonObject)
_LOGGER = logging.getLogger(__name__)
_OPERATIONS = frozenset({"write", "read", "edit", "delete", "search"})


async def run_json(arguments: Sequence[str], content: str | None = None, timeout_seconds: float = 30.0) -> JsonObject:
    """Run `crystalline --json` once and return its stdout JSON object unchanged.

    Content is sent on stdin, stderr is discarded, and failures raise fixed messages.
    A timeout or crash during a mutation does not promise that nothing was applied.
    """
    operation = arguments[0] if arguments and arguments[0] in _OPERATIONS else "unknown"
    started = time.monotonic()
    _LOGGER.debug("crystalline.start operation=%s", operation)
    data = (content or "").encode()
    try:
        with anyio.fail_after(timeout_seconds):
            result = await anyio.run_process(
                ["crystalline", "--json", *arguments],
                input=data or None,
                stdin=None if data else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except TimeoutError as error:
        _LOGGER.error("crystalline.failure operation=%s category=timeout elapsed_ms=%.1f", operation, (time.monotonic() - started) * 1000)
        raise ToolError(f"Crystalline did not answer within {timeout_seconds:g} seconds; a mutation may or may not have been applied.") from error
    except OSError as error:
        _LOGGER.error("crystalline.failure operation=%s category=unavailable elapsed_ms=%.1f", operation, (time.monotonic() - started) * 1000)
        raise ToolError("Crystalline is unavailable.") from error
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
