"""Prove application-entry capture reaches every managed process and MCP handler."""

import json
import os
import sys
from pathlib import Path

import anyio
import pytest
import yaml
from anyio.streams.buffered import BufferedByteReceiveStream
from test_embeddings import Provider, configure
from test_embeddings import provider as provider
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
    """Record each capture and poison later ambient engine and embedding settings."""
    context = capture()
    with (root / "captures").open("a") as stream:
        stream.write("captured\\n")
    os.environ["ENGINE_TEST_ROOT"] = str(root / "wrong-engine")
    for name in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        os.environ[name] = str(root / ("wrong-" + name))
    os.environ["EMBEDDING_MODEL"] = "wrong-model"
    os.environ["EMBEDDING_API_KEY"] = "wrong-secret"
    return context
EngineContext.capture = staticmethod(capture_then_change)
main()
'''


@pytest.mark.asyncio
async def test_application_uses_entry_context_and_limits_credential_overlay_to_daemon(
    runtime: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep initialization, daemon, synchronization, health and memory on one captured context."""
    (runtime / "crystalline").write_text(f"#!{sys.executable}\n" + BUDGET_ENGINE)
    key = runtime / "provider-key"
    key.write_text("bound-secret\n")
    configure(monkeypatch, provider, EMBEDDING_API_KEY="", EMBEDDING_API_KEY_FILE=str(key))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    ambient = dict(os.environ)
    async with application(runtime, command=[sys.executable, "-c", CAPTURE_THEN_CHANGE]) as process:
        await health(process)
        assert process.stdin is not None and process.stdout is not None
        reader = BufferedByteReceiveStream(process.stdout)
        await process.stdin.send(
            b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"read_memory","arguments":{"domain":"notes","permalink":"plan"}}}\n'
        )
        with anyio.fail_after(5):
            result = json.loads(await reader.receive_until(b"\n", 100000))
        assert result["result"]["structuredContent"] == {"store": str(runtime / "xdg_state_home"), "key": ""}
        await process.stdin.aclose()
        with anyio.fail_after(5):
            assert await process.wait() == 0
    config = runtime / "xdg_config_home/crystalline/config.yaml"
    assert yaml.safe_load(config.read_text())["embeddings"]["model"] == "test-model"
    assert "bound-secret" not in config.read_text()
    assert [request.authorization for request in provider.requests] == ["Bearer bound-secret"]
    events = [json.loads(line) for line in (runtime / "events").read_text().splitlines()]
    assert [event["key"] for event in events if event["args"] == ["serve", "--http", "off"]] == ["bound-secret"]
    assert all(event["key"] == "" for event in events if event["args"] != ["serve", "--http", "off"])
    assert all(event["state"] == str(runtime / "xdg_state_home") and event["config"] == str(runtime / "xdg_config_home") for event in events)
    assert (runtime / "captures").read_text().splitlines() == ["captured"]
    assert dict(os.environ) == ambient
    assert b"bound-secret" not in (runtime / "application-stderr").read_bytes()
    assert_reaped(runtime)
