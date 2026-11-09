"""Exercise optional read compression through MCP, the CLI, and loopback LLM providers."""

import json
import os
from pathlib import Path

import pytest
import anyio
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from memory_fake import calls, respond
from pydantic import SecretStr, TypeAdapter
from starlette.testclient import TestClient
from test_compression import Provider, provider as provider

from marabrain.config import Settings, Transport
from marabrain.crystalline import JsonObject
from marabrain.llm_config import LLMProvider
from marabrain.server import create_server
from marabrain.llm_config import LLMSettings
from marabrain.memory_tools import register_memory_tools
from marabrain.compression import compress_memory
from marabrain.domain_registry import DomainRegistry

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


async def test_compression_keeps_its_own_budget_after_engine_deadline(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Complete real provider compression after the memory operation's engine deadline has elapsed."""
    configure(monkeypatch, provider)
    respond(cli, json.dumps(MEMORY))

    async def delayed_provider(settings: LLMSettings, query: str, content: str) -> str:
        """Cross the engine budget boundary before invoking the real loopback provider."""
        await anyio.sleep(1.1)
        return await compress_memory(settings, query, content)

    monkeypatch.setattr("marabrain.memory_tools.compress_memory", delayed_provider)
    server = FastMCP("compressed")
    register_memory_tools(server, llm_settings=LLMSettings(), operation_timeout_seconds=1)
    async with Client(server) as client:
        response = await client.call_tool("read_memory", {**TARGET, "query": "retry", "compress": True})
    assert response.structured_content == {**MEMORY, "content": "result", "compressed": True}
    assert len(provider.requests) == 1


async def test_cache_hit_reads_and_validates_fresh_source_metadata(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reuse only compressed text while preserving each read's fresh native envelope."""
    configure(monkeypatch, provider)
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "4")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "1024")
    respond(cli, json.dumps(MEMORY))
    async with Client(create_server(Settings())) as client:
        arguments = {**TARGET, "query": "retry", "compress": True}
        await client.call_tool("read_memory", arguments)
        fresh = {**MEMORY, "checksum": "new-checksum", "links": ["new-link"], "title": "New title"}
        respond(cli, json.dumps(fresh))
        result = (await client.call_tool("read_memory", arguments)).structured_content
        assert result == {**fresh, "content": "result", "compressed": True}
        respond(cli, json.dumps({**MEMORY, "domain": "private"}))
        with pytest.raises(ToolError, match="different memory"):
            await client.call_tool("read_memory", arguments)
    assert len(provider.requests) == 1
    assert [record["argv"] for record in calls(cli)] == [["--json", "read", "team/plan", "--domain=notes"]] * 3


@pytest.mark.parametrize("change", ["query", "permalink", "content", "domain", "application"])
async def test_cache_reuse_requires_complete_identity(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    """Dispatch a distinct provider request when any reuse identity component changes."""
    configure(monkeypatch, provider)
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "4")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "1024")
    (cli / "domains.json").write_text(json.dumps([{"name": "notes"}, {"name": "other"}]))
    await DomainRegistry.from_environment(5).synchronize()
    respond(cli, json.dumps(MEMORY))
    arguments = {**TARGET, "query": "retry", "compress": True}
    server = create_server(Settings())
    async with Client(server) as client:
        await client.call_tool("read_memory", arguments)
        next_arguments = dict(arguments)
        source = dict(MEMORY)
        if change in {"domain", "permalink", "query"}:
            next_arguments[change] = "other"
            if change != "query":
                source[change] = "other"
        if change == "content":
            source["content"] = "Changed source with unchanged native checksum."
        respond(cli, json.dumps(source))
        if change == "application":
            async with Client(create_server(Settings())) as other:
                await other.call_tool("read_memory", next_arguments)
        else:
            await client.call_tool("read_memory", next_arguments)
    assert len(provider.requests) == 2


async def test_cache_aliases_share_only_the_resolved_domain(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Map aliases and case variants to the exact domain's cache after fresh admission."""
    configure(monkeypatch, provider)
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "4")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "1024")
    (cli / "domains.json").write_text(json.dumps([{"name": "Legacy", "canonical_name": "canonical", "aliases": ["alias"]}]))
    await DomainRegistry.from_environment(5).synchronize()
    source = {**MEMORY, "domain": "Legacy"}
    respond(cli, json.dumps(source))
    async with Client(create_server(Settings())) as client:
        for domain in ("Legacy", "LEGACY", "canonical", "ALIAS"):
            result = await client.call_tool("read_memory", {**TARGET, "domain": domain, "query": "retry", "compress": True})
            assert result.structured_content == {**source, "content": "result", "compressed": True}
    assert len(provider.requests) == 1


@pytest.mark.parametrize("limit", [None, ""])
async def test_disabled_cache_does_not_reuse_compression(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch, limit: str | None) -> None:
    """Preserve separate provider requests when both cache limits are unset or empty."""
    configure(monkeypatch, provider)
    if limit is not None:
        monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", limit)
        monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", limit)
    respond(cli, json.dumps(MEMORY))
    async with Client(create_server(Settings())) as client:
        for _ in range(2):
            await client.call_tool("read_memory", {**TARGET, "query": "retry", "compress": True})
    assert len(provider.requests) == 2


@pytest.mark.parametrize("entries,byte_limit,queries,expected", [(2, 100, ["a", "b", "a", "c", "b"], 4), (4, 11, ["a", "b", "a"], 3), (4, 5, ["a", "a"], 2)])
async def test_cache_lru_enforces_entry_and_payload_byte_limits(
    cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch, entries: int, byte_limit: int, queries: list[str], expected: int
) -> None:
    """Evict least recent payloads under either limit and bypass oversized results."""
    configure(monkeypatch, provider)
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", str(entries))
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", str(byte_limit))
    respond(cli, json.dumps(MEMORY))
    async with Client(create_server(Settings())) as client:
        for query in queries:
            await client.call_tool("read_memory", {**TARGET, "query": query, "compress": True})
    assert len(provider.requests) == expected


async def test_cache_provider_failure_recovers_without_fallback(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never cache a provider error and allow a later successful request to populate reuse."""
    configure(monkeypatch, provider)
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "4")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "1024")
    respond(cli, json.dumps(MEMORY))
    async with Client(create_server(Settings())) as client:
        arguments = {**TARGET, "query": "retry", "compress": True}
        provider.status = 500
        with pytest.raises(ToolError, match="LLM compression failed"):
            await client.call_tool("read_memory", arguments)
        provider.status = 200
        assert (await client.call_tool("read_memory", arguments)).structured_content == {**MEMORY, "content": "result", "compressed": True}
        await client.call_tool("read_memory", arguments)
    assert len(provider.requests) == 2
