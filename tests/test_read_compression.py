"""Exercise optional read compression through MCP, the CLI, and loopback LLM providers."""

import json
import os
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memory_fake import calls, respond
from pydantic import SecretStr, TypeAdapter
from starlette.testclient import TestClient
from test_compression import Provider, provider as provider

from marabrain.config import Settings, Transport
from marabrain.crystalline import JsonObject
from marabrain.llm_config import LLMProvider
from marabrain.server import create_server

MEMORY: JsonObject = {"domain": "notes", "permalink": "team/plan", "content": "# Retries\nRetry at most twice.\n# Lunch\nPizza.", "checksum": "abc"}
TARGET = {"domain": "notes", "permalink": "team/plan"}


@pytest.fixture(autouse=True)
def clean_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep provider configuration isolated from exported developer settings."""
    for name in tuple(os.environ):
        if name.startswith("LLM_"):
            monkeypatch.delenv(name)


def configure(monkeypatch: pytest.MonkeyPatch, provider: Provider) -> None:
    """Configure the server from the same environment variables used in deployment."""
    monkeypatch.setenv("LLM_MODEL", "generation-model")
    monkeypatch.setenv("LLM_PROVIDER_URL", provider.url)
    monkeypatch.setenv("LLM_PROVIDER", LLMProvider.OPENAI.value)
    monkeypatch.setenv("LLM_API_KEY", "test-api-key")


async def test_compressed_read_replaces_content_and_preserves_source_metadata(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Send the complete source and query once and return generated content with an explicit marker."""
    configure(monkeypatch, provider)
    respond(cli, json.dumps(MEMORY))
    async with Client(create_server(Settings())) as client:
        result = (await client.call_tool("read_memory", {**TARGET, "query": "retry cap", "compress": True})).structured_content
    assert result == {**MEMORY, "content": "result", "compressed": True}
    assert [record["argv"] for record in calls(cli)] == [["--json", "read", "team/plan", "--domain=notes"]]
    [(_, _, payload)] = provider.requests
    messages = TypeAdapter(list[JsonObject]).validate_python(payload["messages"])
    content = messages[-1]["content"]
    assert isinstance(content, str)
    assert json.loads(content) == {"query": "retry cap"}
    system = messages[0]["content"]
    assert isinstance(system, str) and system.endswith(json.dumps(MEMORY["content"]))


@pytest.mark.parametrize("options", [{}, {"compress": False}, {"compress": False, "query": "ignored"}])
async def test_plain_read_returns_native_json_without_provider(cli: Path, provider: Provider, options: dict[str, object]) -> None:
    """Keep ordinary reads independent of LLM configuration and leave content unchanged."""
    respond(cli, json.dumps(MEMORY))
    async with Client(create_server(Settings())) as client:
        result = (await client.call_tool("read_memory", {**TARGET, **options})).structured_content
    assert result == MEMORY
    assert provider.requests == []


@pytest.mark.parametrize("options", [{"compress": True}, {"compress": True, "query": ""}, {"compress": True, "query": "   "}, {"compress": "true"}])
async def test_invalid_compression_arguments_fail_before_read(cli: Path, options: dict[str, object]) -> None:
    """Require a real boolean and a nonempty query before executing a compressed read."""
    async with Client(create_server(Settings())) as client:
        with pytest.raises(ToolError):
            await client.call_tool("read_memory", {**TARGET, **options})
    assert calls(cli) == []


@pytest.mark.parametrize("memory", [{**MEMORY, "domain": "private"}, {**MEMORY, "permalink": "other"}, {**MEMORY, "content": None}, {}])
async def test_invalid_read_never_reaches_provider(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch, memory: JsonObject) -> None:
    """Reject malformed or mismatched source content before external disclosure."""
    configure(monkeypatch, provider)
    respond(cli, json.dumps(memory))
    async with Client(create_server(Settings())) as client:
        with pytest.raises(ToolError):
            await client.call_tool("read_memory", {**TARGET, "query": "retry", "compress": True})
    assert provider.requests == []


async def test_compressed_read_obeys_domain_policy(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deny unauthorized reads before invoking the CLI or LLM."""
    configure(monkeypatch, provider)
    async with Client(create_server(Settings(allowed_domains=()))) as client:
        with pytest.raises(ToolError, match="not permitted"):
            await client.call_tool("read_memory", {**TARGET, "query": "retry", "compress": True})
    assert calls(cli) == []
    assert provider.requests == []


async def test_missing_llm_configuration_is_a_tool_error(cli: Path) -> None:
    """Refuse requested compression without falling back to full content."""
    respond(cli, json.dumps(MEMORY))
    async with Client(create_server(Settings())) as client:
        with pytest.raises(ToolError, match="not configured"):
            await client.call_tool("read_memory", {**TARGET, "query": "retry", "compress": True})


@pytest.mark.parametrize("status", [200, 500])
def test_http_compressed_read_success_and_provider_failure(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """Deliver distilled content over authenticated MCP HTTP or return a masked provider error."""
    configure(monkeypatch, provider)
    provider.status = status
    respond(cli, json.dumps(MEMORY))
    server = create_server(Settings(transport=Transport.HTTP, bearer_secret=SecretStr("s" * 32)))
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "read_memory", "arguments": {**TARGET, "query": "retry cap", "compress": True}},
    }
    with TestClient(server.http_app(stateless_http=True, json_response=True)) as client:
        reply = client.post("/mcp", json=payload, headers={"authorization": "Bearer " + "s" * 32, "accept": "application/json, text/event-stream"})
    assert reply.status_code == 200
    result = reply.json()["result"]
    if status == 200:
        assert result["structuredContent"] == {**MEMORY, "content": "result", "compressed": True}
    else:
        assert result["isError"] is True
        assert str(MEMORY["content"]) not in reply.text and "test-api-key" not in reply.text
