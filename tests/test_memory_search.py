"""Behavior tests for the search_memories tool contract at the MCP and subprocess boundaries."""

from pathlib import Path

import anyio
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from memory_fake import RECEIPT, call, calls
from starlette.testclient import TestClient

from marabrain.config import Settings
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
