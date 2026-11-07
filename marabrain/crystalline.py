"""Asynchronous adapter that runs the Crystalline command line and returns its JSON output."""

import subprocess
from collections.abc import Sequence

import anyio
from fastmcp.exceptions import ToolError
from pydantic import JsonValue, TypeAdapter, ValidationError

COMMAND_TIMEOUT_SECONDS = 30
JsonObject = dict[str, JsonValue]
_OUTPUT = TypeAdapter(JsonObject)


async def run_json(arguments: Sequence[str], content: str | None = None) -> JsonObject:
    """Run `crystalline --json` once and return its stdout JSON object unchanged.

    Content is sent on stdin, stderr is discarded, and failures raise fixed messages.
    A timeout or crash during a mutation does not promise that nothing was applied.
    """
    data = (content or "").encode()
    try:
        with anyio.fail_after(COMMAND_TIMEOUT_SECONDS):
            result = await anyio.run_process(
                ["crystalline", "--json", *arguments],
                input=data or None,
                stdin=None if data else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except TimeoutError as error:
        raise ToolError(f"Crystalline did not answer within {COMMAND_TIMEOUT_SECONDS} seconds; a mutation may or may not have been applied.") from error
    except OSError as error:
        raise ToolError("Crystalline is unavailable.") from error
    if result.returncode != 0:
        raise ToolError(f"Crystalline command failed with exit code {result.returncode}.")
    try:
        return _OUTPUT.validate_json(result.stdout)
    except ValidationError:
        raise ToolError("Crystalline returned malformed output.") from None
