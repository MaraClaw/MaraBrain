"""Behavior tests for the search_memories tool contract at the MCP and subprocess boundaries."""

from pathlib import Path

import anyio
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from memory_fake import RECEIPT, call, calls, respond
from starlette.testclient import TestClient
from test_domain_resolution import command_barrier

from marabrain.config import Settings
from marabrain.engine_context import EngineContext
from marabrain.memory_tools import register_memory_tools
from marabrain.server import create_server

BASE: dict[str, object] = {"query": "retries", "domains": ["notes"]}


@pytest.mark.parametrize(
    "arguments,argv",
    [
        pytest.param(BASE, ["--domain=notes", "--limit=10", "--page=1", "retries"], id="required-only"),
        pytest.param({**BASE, "query": "two words"}, ["--domain=notes", "--limit=10", "--page=1", "two words"], id="query-with-space"),
        pytest.param(
            {"query": "r", "domains": ["a", "b"], "tags": ["x", "y"], "limit": 5, "page": 2},
            ["--domain=a", "--domain=b", "--tag=x", "--tag=y", "--limit=5", "--page=2", "r"],
            id="filters-and-paging",
        ),
        pytest.param({**BASE, "page": 20000, "limit": 100}, ["--domain=notes", "--limit=100", "--page=20000", "retries"], id="deep-page"),
    ],
)
async def test_search_runs_one_cli_command_and_returns_native_json(cli: Path, arguments: dict[str, object], argv: list[str]) -> None:
    """Verify search maps its arguments to one exact argv and returns the upstream JSON object unchanged."""
    assert await call("search_memories", arguments) == RECEIPT
    assert [(record["argv"], record["stdin"]) for record in calls(cli)] == [(["--json", "search", *argv], "")]


def without(key: str) -> dict[str, object]:
    """Return the valid search arguments with one key removed."""
    return {name: value for name, value in BASE.items() if name != key}


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(without("query"), id="query-missing"),
        pytest.param({**BASE, "query": None}, id="query-null"),
        pytest.param({**BASE, "query": ""}, id="query-empty"),
        pytest.param({**BASE, "query": "   "}, id="query-whitespace"),
        pytest.param({**BASE, "query": " padded"}, id="query-padded"),
        pytest.param({**BASE, "query": "--limit"}, id="query-flag"),
        pytest.param(without("domains"), id="domains-missing"),
        pytest.param({**BASE, "domains": None}, id="domains-null"),
        pytest.param({**BASE, "domains": []}, id="domains-empty"),
        pytest.param({**BASE, "domains": [""]}, id="domain-empty"),
        pytest.param({**BASE, "domains": ["notes", "-x"]}, id="domain-flag"),
        pytest.param({**BASE, "tags": ["a,b"]}, id="tag-comma"),
        pytest.param({**BASE, "limit": 0}, id="limit-low"),
        pytest.param({**BASE, "limit": 101}, id="limit-high"),
        pytest.param({**BASE, "page": 0}, id="page-low"),
    ],
)
async def test_invalid_search_arguments_fail_before_execution(cli: Path, arguments: dict[str, object]) -> None:
    """Verify that missing, empty, or unsafe search arguments are rejected without starting crystalline."""
    with pytest.raises(ToolError):
        await call("search_memories", arguments)
    assert calls(cli) == []


async def test_search_schema_requires_query_and_domains_only() -> None:
    """Verify the advertised schema requires a nonempty query and domain list and leaves tags, limit, and page optional."""
    async with Client(create_server(Settings())) as client:
        (tool,) = [tool for tool in await client.list_tools() if tool.name == "search_memories"]
    schema = tool.input_schema
    assert set(schema["required"]) == {"query", "domains"}
    assert schema["properties"]["domains"]["minItems"] == 1
    assert schema["properties"]["query"]["minLength"] == 1
    assert {"tags", "limit", "page"}.isdisjoint(schema["required"])


@pytest.mark.parametrize("domains", [["notes", "private"], ["Notes"], ["private", "notes"]])
async def test_search_rejects_any_unapproved_domain(cli: Path, domains: list[str]) -> None:
    """Reject an entire search when any domain is not an exact policy match."""
    server = FastMCP("test")
    register_memory_tools(server, frozenset({"notes"}))
    async with Client(server) as client:
        with pytest.raises(ToolError, match="not permitted"):
            await client.call_tool("search_memories", {"query": "r", "domains": domains})
    assert calls(cli) == []


async def test_concurrent_servers_keep_independent_policies(cli: Path) -> None:
    """Keep different server policies independent while requests overlap."""
    allowed = create_server(Settings(allowed_domains=("notes",)))
    denied = create_server(Settings(allowed_domains=()))

    async def request(server: FastMCP, permitted: bool) -> None:
        """Check the result of one request against its server policy."""
        async with Client(server) as client:
            if permitted:
                assert (await client.call_tool("search_memories", BASE)).structured_content == RECEIPT
            else:
                with pytest.raises(ToolError, match="not permitted"):
                    await client.call_tool("search_memories", BASE)

    async with anyio.create_task_group() as group:
        group.start_soon(request, allowed, True)
        group.start_soon(request, denied, False)
    assert len(calls(cli)) == 1


@pytest.mark.parametrize("domains", [["other", "NOTES"], ["notes", "other"], ["other", "notes", "OTHER"]])
async def test_search_domain_admission_is_atomic_and_deduplicated(cli: Path, domains: list[str]) -> None:
    """Reject a mixed saturated search without consuming another domain's available permit."""
    server = FastMCP("atomic")
    register_memory_tools(server, context=EngineContext.capture(max_transient_processes=4), domain_command_capacity=1)
    async with command_barrier(cli) as (started, release), Client(server) as client:

        async def occupy() -> None:
            """Hold notes while another domain remains dispatchable."""
            await client.call_tool("read_memory", {"domain": "notes", "permalink": "held"})

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(occupy)
                await started.receive()
                try:
                    with pytest.raises(ToolError, match="domain command capacity is exhausted"):
                        await client.call_tool("search_memories", {"query": "r", "domains": domains})
                    assert len(calls(cli)) == 1
                    result = await client.call_tool("search_memories", {"query": "r", "domains": ["other", "OTHER", "additional"]})
                    assert result.structured_content == RECEIPT
                    assert len(calls(cli)) == 2
                finally:
                    release.set()
        assert (await client.call_tool("search_memories", {"query": "r", "domains": ["notes", "NOTES", "other"]})).structured_content == RECEIPT


@pytest.mark.parametrize("capacity", [1, 2])
@pytest.mark.parametrize("domain", ["notes", "other"])
async def test_domain_command_counters_are_application_local(cli: Path, capacity: int, domain: str) -> None:
    """Apply the uniform capacity to every domain with independent application counters."""
    first, second = FastMCP("first"), FastMCP("second")
    for server in (first, second):
        register_memory_tools(server, domain_command_capacity=capacity)
    async with command_barrier(cli) as (started, release), Client(first) as client, Client(second) as other:

        async def occupy() -> None:
            """Hold only the first application's quota."""
            await client.call_tool("read_memory", {"domain": domain, "permalink": "held"})

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                for _ in range(capacity):
                    tasks.start_soon(occupy)
                for _ in range(capacity):
                    await started.receive()
                try:
                    arguments = {"query": "r", "domains": [domain]}
                    assert (await other.call_tool("search_memories", arguments)).structured_content == RECEIPT
                    with pytest.raises(ToolError, match="domain command capacity is exhausted"):
                        await client.call_tool("search_memories", arguments)
                    assert len(calls(cli)) == capacity + 1
                finally:
                    release.set()


@pytest.mark.parametrize("value", [None, ""])
async def test_disabled_domain_command_capacity_allows_same_domain_overlap(cli: Path, monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Allow overlapping same-domain commands when the scalar limit is absent or empty."""
    monkeypatch.delenv("MARABRAIN_DOMAIN_COMMAND_CAPACITY", raising=False)
    if value is not None:
        monkeypatch.setenv("MARABRAIN_DOMAIN_COMMAND_CAPACITY", value)
    server = create_server(Settings(max_transient_processes=4))
    async with command_barrier(cli) as (started, release), Client(server) as client:

        async def occupy() -> None:
            """Hold a same-domain command while another command is admitted."""
            assert (await client.call_tool("read_memory", {"domain": "notes", "permalink": "held"})).structured_content == RECEIPT

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                for _ in range(2):
                    tasks.start_soon(occupy)
                for _ in range(2):
                    await started.receive()
                try:
                    assert (await client.call_tool("search_memories", BASE)).structured_content == RECEIPT
                    assert len(calls(cli)) == 3
                finally:
                    release.set()


@pytest.mark.parametrize("failure", ["global", "unavailable", "exit", "malformed", "capture"])
async def test_failed_search_releases_every_domain_reservation(cli: Path, failure: str) -> None:
    """Release the entire domain set after global refusal or native command failures."""
    context = EngineContext.capture(max_transient_processes=2, max_capture_bytes=256)
    server = FastMCP("failures")
    register_memory_tools(server, context=context, domain_command_capacity=1)
    arguments = {"query": "r", "domains": ["notes", "other"]}
    executable = cli / "crystalline"
    original = executable.read_text()
    async with Client(server) as client:
        if failure == "global":
            with context.transients.admit(), pytest.raises(ToolError, match="transient process capacity"):
                await client.call_tool("search_memories", arguments)
            assert calls(cli) == []
        else:
            if failure == "unavailable":
                executable.unlink()
            elif failure == "exit":
                respond(cli, exit_code=8)
            elif failure == "malformed":
                respond(cli, stdout="secret non-json")
            else:
                respond(cli, stdout="x" * 257)
            with pytest.raises(ToolError):
                await client.call_tool("search_memories", arguments)
            executable.write_text(original)
            executable.chmod(0o755)
            respond(cli)
        assert (await client.call_tool("search_memories", arguments)).structured_content == RECEIPT


def test_http_servers_keep_independent_policies(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply each HTTP server's own policy to authenticated requests."""
    monkeypatch.setenv("MARABRAIN_TRANSPORT", "http")
    monkeypatch.setenv("MARABRAIN_BEARER_SECRET", "s" * 32)
    allowed = Settings(allowed_domains=("notes",))
    denied = Settings(allowed_domains=())
    headers = {"host": "localhost", "accept": "application/json, text/event-stream", "authorization": "Bearer " + "s" * 32}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_memories", "arguments": BASE}}
    with TestClient(create_server(allowed).http_app(stateless_http=True, json_response=True)) as first:
        with TestClient(create_server(denied).http_app(stateless_http=True, json_response=True)) as second:
            assert first.post("/mcp", headers=headers, json=payload).json()["result"]["structuredContent"] == RECEIPT
            assert second.post("/mcp", headers=headers, json=payload).json()["result"]["isError"] is True
    assert len(calls(cli)) == 1
