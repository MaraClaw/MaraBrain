"""Exercise compression progress and its lifetime through MCP requests."""

import json
from pathlib import Path

import anyio
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memory_fake import respond
from test_compression import Provider, provider as provider
from test_read_compression import MEMORY, TARGET, clean_llm_environment as clean_llm_environment, configure
from pydantic import SecretStr
from starlette.testclient import TestClient

from marabrain.config import Settings, Transport
from marabrain.server import create_server


@pytest.mark.parametrize("outcome", ["success", "provider_timeout", "cancelled"])
async def test_compression_reports_progress_until_request_ends(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    """Keep long calls active while preserving provider deadlines and caller cancellation."""
    configure(monkeypatch, provider)
    monkeypatch.setattr("marabrain.memory_tools._COMPRESSION_PROGRESS_INTERVAL", 0.02)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0.05" if outcome == "provider_timeout" else "2")
    provider.delay_seconds = 0.2
    respond(cli, json.dumps(MEMORY))
    updates: list[tuple[float, float | None, str | None]] = []

    async def progress(value: float, total: float | None, message: str | None) -> None:
        """Capture protocol notifications without depending on their wording."""
        updates.append((value, total, message))
        if outcome == "cancelled":
            cancellation.cancel()

    async with Client(create_server(Settings()), progress_handler=progress, timeout=2) as client:
        arguments = {**TARGET, "query": "retry", "compress": True}
        if outcome == "cancelled":
            with anyio.fail_after(2), anyio.CancelScope() as cancellation:
                await client.call_tool("read_memory", arguments)
            assert cancellation.cancel_called
        elif outcome == "provider_timeout":
            with pytest.raises(ToolError, match="LLM compression failed"):
                await client.call_tool("read_memory", arguments)
        else:
            result = await client.call_tool("read_memory", arguments)
            assert result.structured_content == {**MEMORY, "content": "result", "compressed": True}
        assert updates
        if outcome == "success":
            assert len(updates) >= 2
        count = len(updates)
        await anyio.sleep(0.05)
        assert len(updates) == count
        assert all(total is None for _, total, _ in updates)
        assert all(right[0] > left[0] for left, right in zip(updates, updates[1:]))
        assert (await client.call_tool("read_memory", TARGET)).structured_content == MEMORY
        assert len(updates) == count
    assert len(provider.requests) <= 1 if outcome == "cancelled" else len(provider.requests) == 1


def test_http_compression_emits_progress_on_request_stream(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliver progress and the final result on stateless HTTP's authenticated SSE response."""
    configure(monkeypatch, provider)
    monkeypatch.setattr("marabrain.memory_tools._COMPRESSION_PROGRESS_INTERVAL", 0.02)
    provider.delay_seconds = 0.15
    respond(cli, json.dumps(MEMORY))
    server = create_server(Settings(transport=Transport.HTTP, bearer_secret=SecretStr("s" * 32)))
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "read_memory", "arguments": {**TARGET, "query": "retry", "compress": True}, "_meta": {"progressToken": "compression"}},
    }
    with TestClient(server.http_app(stateless_http=True)) as client:
        reply = client.post("/mcp", json=payload, headers={"authorization": "Bearer " + "s" * 32, "accept": "application/json, text/event-stream"})
    assert reply.status_code == 200
    frames = [json.loads(line[6:]) for line in reply.text.splitlines() if line.startswith("data: ")]
    notifications = [frame for frame in frames if frame.get("method") == "notifications/progress"]
    assert len(notifications) >= 2
    assert all(frame["params"]["progressToken"] == "compression" for frame in notifications)
    [result] = [frame["result"] for frame in frames if "result" in frame]
    assert result["structuredContent"] == {**MEMORY, "content": "result", "compressed": True}
