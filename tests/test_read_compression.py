"""Exercise optional read compression through MCP, the CLI, and loopback LLM providers."""

import json
import os
from pathlib import Path

import pytest
import anyio
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError
from memory_fake import RECEIPT, WORKSPACE_ACTIVATION, WORKSPACE_ID, calls, respond, workspace_registry
from pydantic import SecretStr, TypeAdapter
from starlette.testclient import TestClient
from test_compression import Provider, provider as provider

from marabrain.config import Settings, Transport
from marabrain.crystalline import JsonObject
from marabrain.llm_config import LLMProvider
from marabrain.server import create_server
from marabrain.llm_config import LLMSettings
from marabrain.compression import compress_memory

MEMORY: JsonObject = {"domain": "notes", "permalink": "team/plan", "content": "# Retries\nRetry at most twice.\n# Lunch\nPizza.", "checksum": "abc"}
TARGET = {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "team/plan"}


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
    expected = [*WORKSPACE_ACTIVATION, ["--json", "read", "team/plan", "--domain=notes"]]
    assert [(record["argv"], record["stdin"]) for record in calls(cli)] == [(command, "") for command in expected]
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
    server = create_server(Settings(llm=LLMSettings(), operation_timeout_seconds=1))
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
    expected = [*WORKSPACE_ACTIVATION, *([["--json", "read", "team/plan", "--domain=notes"]] * 3)]
    assert [(record["argv"], record["stdin"]) for record in calls(cli)] == [(command, "") for command in expected]


@pytest.mark.parametrize("change", ["query", "permalink", "content", "domain", "application"])
async def test_cache_reuse_requires_complete_identity(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    """Dispatch a distinct provider request when any reuse identity component changes."""
    configure(monkeypatch, provider)
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "4")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "1024")
    (cli / "domains.json").write_text(json.dumps([{"name": "notes"}, {"name": "other"}]))
    await workspace_registry().synchronize()
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
    await workspace_registry().synchronize()
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


MUTATIONS: dict[str, dict[str, object]] = {"edit_memory": {"operation": {"type": "append", "content": "more"}}, "delete_memory": {}}
MUTATION_ARGV = {"edit_memory": ["--json", "edit", "team/plan", "notes", "append"], "delete_memory": ["--json", "delete", "team/plan", "notes", "--force"]}


def enable_cache(monkeypatch: pytest.MonkeyPatch, total: int | None = None) -> None:
    """Enable the compression cache with generous per-pair limits and an optional application-wide ceiling."""
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "4")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "1024")
    if total is not None:
        monkeypatch.setenv("LLM_COMPRESSION_CACHE_TOTAL_MAX_BYTES", str(total))


async def compressed_read(
    client: Client[FastMCPTransport], cli: Path, workspace_id: str = WORKSPACE_ID, domain: str = "notes", permalink: str = "team/plan", query: str = "retry"
) -> None:
    """Read one memory compressed after pointing the fake engine at its native document."""
    source = {**MEMORY, "domain": domain, "permalink": permalink}
    respond(cli, json.dumps(source))
    result = await client.call_tool("read_memory", {"workspace_id": workspace_id, "domain": domain, "permalink": permalink, "query": query, "compress": True})
    assert result.structured_content == {**source, "content": "result", "compressed": True}


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
@pytest.mark.parametrize("tool", list(MUTATIONS))
async def test_dispatched_mutation_purges_cached_compression(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch, tool: str, outcome: str) -> None:
    """Drop the memory's cached text once its edit or delete launches, whether the command succeeds, exits nonzero, or times out."""
    configure(monkeypatch, provider)
    enable_cache(monkeypatch)
    async with Client(create_server(Settings(command_timeout_seconds=1.0))) as client:
        await compressed_read(client, cli)
        await compressed_read(client, cli)
        assert len(provider.requests) == 1
        respond(cli, json.dumps(RECEIPT), exit_code=1 if outcome == "failure" else 0, hang=outcome == "timeout")
        if outcome == "success":
            await client.call_tool(tool, {**TARGET, **MUTATIONS[tool]})
        else:
            with pytest.raises(ToolError, match="exit code 1" if outcome == "failure" else "may or may not have been applied"):
                await client.call_tool(tool, {**TARGET, **MUTATIONS[tool]})
        assert calls(cli)[-1]["argv"] == MUTATION_ARGV[tool]
        await compressed_read(client, cli)
        assert len(provider.requests) == 2
        await compressed_read(client, cli)
        assert len(provider.requests) == 2


async def test_purge_spares_other_permalinks_domains_and_workspaces(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave cached text for every other permalink, resolved domain, and workspace after one memory is deleted."""
    configure(monkeypatch, provider)
    enable_cache(monkeypatch)
    (cli / "domains.json").write_text(json.dumps([{"name": "notes"}, {"name": "other"}]))
    await workspace_registry().synchronize()
    memories = [
        (WORKSPACE_ID, "notes", "team/plan"),
        (WORKSPACE_ID, "notes", "team/other"),
        (WORKSPACE_ID, "other", "team/plan"),
        ("second-workspace", "notes", "team/plan"),
    ]
    async with Client(create_server(Settings())) as client:
        for memory in memories:
            await compressed_read(client, cli, *memory)
        assert len(provider.requests) == 4
        respond(cli)
        await client.call_tool("delete_memory", TARGET)
        for memory in memories[1:]:
            await compressed_read(client, cli, *memory)
        assert len(provider.requests) == 4
        await compressed_read(client, cli, *memories[0])
        assert len(provider.requests) == 5


async def test_mutation_refused_before_dispatch_purges_nothing(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the cached text when a denied domain spelling that resolves to the same memory is refused before any command."""
    configure(monkeypatch, provider)
    enable_cache(monkeypatch)
    async with Client(create_server(Settings(allowed_domains=("notes",)))) as client:
        await compressed_read(client, cli)
        for tool, extra in MUTATIONS.items():
            with pytest.raises(ToolError, match="not permitted"):
                await client.call_tool(tool, {**TARGET, "domain": "NOTES", **extra})
        await compressed_read(client, cli)
    assert len(provider.requests) == 1
    read = ["--json", "read", "team/plan", "--domain=notes"]
    assert [record["argv"] for record in calls(cli)] == [*WORKSPACE_ACTIVATION, read, read]


async def test_mutation_during_a_running_compression_answers_the_caller_without_caching(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return an in-flight compression to its caller after an edit purges the memory, but cache nothing so the next read recomputes."""
    entered, release = anyio.Event(), anyio.Event()
    queries: list[str] = []

    async def compress(settings: LLMSettings, query: str, content: str) -> str:
        """Hold each provider call at an explicit barrier."""
        queries.append(query)
        entered.set()
        await release.wait()
        return "compressed body"

    monkeypatch.setenv("LLM_MODEL", "generation-model")
    monkeypatch.setenv("LLM_PROVIDER_URL", "https://provider.test/v1")
    enable_cache(monkeypatch)
    monkeypatch.setattr("marabrain.memory_tools.compress_memory", compress)
    respond(cli, json.dumps(MEMORY))
    arguments = {**TARGET, "query": "retry", "compress": True}
    results: list[JsonObject | None] = []
    async with Client(create_server(Settings())) as client:

        async def read() -> None:
            """Record the response of the compression that straddles the edit."""
            results.append((await client.call_tool("read_memory", arguments)).structured_content)

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(read)
                await entered.wait()
                await anyio.wait_all_tasks_blocked()
                respond(cli)
                await client.call_tool("edit_memory", {**TARGET, **MUTATIONS["edit_memory"]})
                release.set()
            assert results == [{**MEMORY, "content": "compressed body", "compressed": True}]
            respond(cli, json.dumps(MEMORY))
            await client.call_tool("read_memory", arguments)
            await client.call_tool("read_memory", arguments)
    assert queries == ["retry", "retry"]


async def test_total_ceiling_spans_workspaces_and_hits_refresh_recency(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the application to its byte ceiling across workspaces, evicting the least recently used entry first."""
    configure(monkeypatch, provider)
    enable_cache(monkeypatch, total=12)
    sequence = [
        (WORKSPACE_ID, "x"),
        ("second-workspace", "x"),
        (WORKSPACE_ID, "x"),
        (WORKSPACE_ID, "y"),
        ("second-workspace", "x"),
        (WORKSPACE_ID, "y"),
        (WORKSPACE_ID, "x"),
    ]
    counts: list[int] = []
    async with Client(create_server(Settings())) as client:
        for workspace, query in sequence:
            await compressed_read(client, cli, workspace, query=query)
            counts.append(len(provider.requests))
    assert counts == [1, 2, 2, 3, 4, 4, 5]
