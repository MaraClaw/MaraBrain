"""Prove application-entry capture reaches every managed process and MCP handler."""

import json
import os
import sys
from pathlib import Path

import anyio
import pytest
from anyio.streams.buffered import BufferedByteReceiveStream
from memory_fake import WORKSPACE_ID, workspace_context
from test_engine_lifecycle import application, assert_reaped, health
from test_engine_lifecycle import runtime as runtime
from test_startup_deadlines import BUDGET_ENGINE

CAPTURE_THEN_CHANGE = '''
"""Change host ambient state immediately after the application captures its context."""
import os
from pathlib import Path
from marabrain.engine_context import EngineContext
from marabrain.server import main

capture = EngineContext.capture
root = Path(os.environ["ENGINE_TEST_ROOT"])
def capture_then_change():
    """Record each capture and poison later ambient engine settings."""
    context = capture()
    with (root / "captures").open("a") as stream:
        stream.write("captured\\n")
    os.environ["ENGINE_TEST_ROOT"] = str(root / "wrong-engine")
    for name in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        os.environ[name] = str(root / ("wrong-" + name))
    return context
EngineContext.capture = staticmethod(capture_then_change)
main()
'''


@pytest.mark.asyncio
async def test_application_uses_entry_context_for_every_engine_process(runtime: Path) -> None:
    """Keep the daemon, synchronization, health and memory on one captured context."""
    (runtime / "crystalline").write_text(f"#!{sys.executable}\n" + BUDGET_ENGINE)
    ambient = dict(os.environ)
    context = workspace_context()
    async with application(runtime, command=[sys.executable, "-c", CAPTURE_THEN_CHANGE]) as process:
        await health(process)
        assert process.stdin is not None and process.stdout is not None
        reader = BufferedByteReceiveStream(process.stdout)
        await process.stdin.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "read_memory", "arguments": {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "plan"}},
                }
            ).encode()
            + b"\n"
        )
        with anyio.fail_after(5):
            result = json.loads(await reader.receive_until(b"\n", 100000))
        assert result["result"]["structuredContent"] == {"store": str(context.state_home)}
        await process.stdin.aclose()
        with anyio.fail_after(5):
            assert await process.wait() == 0
    assert not (Path(context.environment["XDG_CONFIG_HOME"]) / "crystalline/config.yaml").exists()
    events = [json.loads(line) for line in (runtime / "events").read_text().splitlines()]
    assert ["serve", "--http", "off"] in [event["args"] for event in events]
    assert all(event["state"] == str(context.state_home) and event["config"] == context.environment["XDG_CONFIG_HOME"] for event in events)
    assert (runtime / "captures").read_text().splitlines() == ["captured"]
    assert dict(os.environ) == ambient
    assert_reaped(runtime)
