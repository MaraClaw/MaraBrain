"""Behavior tests for the graph, review, and vocabulary tools at the MCP and subprocess boundaries."""

import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memory_fake import RECEIPT, WORKSPACE_ACTIVATION, WORKSPACE_ID, call, calls, domain_list, workspace_registry
from test_domain_registry import argv

from marabrain.config import Settings
from marabrain.server import create_server

ANCHOR: dict[str, object] = {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "team/plan"}
REVIEW: dict[str, object] = {"workspace_id": WORKSPACE_ID, "domains": ["notes"]}
VOCABULARY: dict[str, object] = {"workspace_id": WORKSPACE_ID, "domain": "notes"}


@pytest.mark.parametrize(
    "tool,arguments,command",
    [
        pytest.param("build_memory_context", ANCHOR, ["context", "crystalline://notes/team/plan", "--domain=notes"], id="context-defaults"),
        pytest.param(
            "build_memory_context",
            {**ANCHOR, "domains": ["other", "notes", "other"], "depth": 3, "max_related": 25, "timeframe": "2w"},
            ["context", "crystalline://notes/team/plan", "--domain=notes", "--domain=other", "--depth=3", "--max-related=25", "--timeframe=2w"],
            id="context-options-deduplicated",
        ),
        pytest.param("review_memories", REVIEW, ["evolve", "--domain=notes", "--limit=10", "--page=1"], id="review-defaults"),
        pytest.param(
            "review_memories",
            {**REVIEW, "domains": ["notes", "other"], "families": ["structure", "temporal"], "limit": 100, "page": 3},
            ["evolve", "--domain=notes", "--domain=other", "--family=structure", "--family=temporal", "--limit=100", "--page=3"],
            id="review-filters",
        ),
        pytest.param("get_memory_vocabulary", VOCABULARY, ["vocabulary", "--domain=notes"], id="vocabulary"),
    ],
)
async def test_graph_tool_runs_one_cli_command_and_returns_native_json(cli: Path, tool: str, arguments: dict[str, object], command: list[str]) -> None:
    """Verify first activation precedes one exact final command with empty stdin and the unchanged native response."""
    domain_list(cli, ["notes", "other"])
    await workspace_registry().synchronize()
    (cli / "calls.jsonl").unlink()
    assert await call(tool, arguments) == RECEIPT
    assert [(row["argv"], row["stdin"]) for row in calls(cli)] == [*((step, "") for step in WORKSPACE_ACTIVATION), (["--json", *command], "")]


@pytest.mark.parametrize(
    "tool,arguments,command",
    [
        pytest.param("build_memory_context", ANCHOR, ["context", "crystalline://Legacy/team/plan", "--domain=Legacy"], id="context"),
        pytest.param("review_memories", REVIEW, ["evolve", "--domain=Legacy", "--limit=10", "--page=1"], id="review"),
        pytest.param("get_memory_vocabulary", VOCABULARY, ["vocabulary", "--domain=Legacy"], id="vocabulary"),
    ],
)
@pytest.mark.parametrize("spelling", ["LEGACY", "alias"])
async def test_graph_tools_use_the_exact_registration_name(cli: Path, spelling: str, tool: str, arguments: dict[str, object], command: list[str]) -> None:
    """Build the anchor URL and filters from the resolved registration, never from the caller's spelling."""
    (cli / "domains.json").write_text(json.dumps([{"name": "Legacy", "aliases": ["alias"]}]))
    await workspace_registry().synchronize()
    (cli / "calls.jsonl").unlink()
    domain_arguments = {"domains": [spelling]} if tool == "review_memories" else {"domain": spelling}
    await call(tool, {**arguments, **domain_arguments})
    assert argv(cli) == [*WORKSPACE_ACTIVATION, ["--json", *command]]


@pytest.mark.parametrize(
    "tool,arguments",
    [
        pytest.param("build_memory_context", {**ANCHOR, "permalink": "crystalline://notes/plan"}, id="anchor-url"),
        pytest.param("build_memory_context", {**ANCHOR, "permalink": "team/*/../x"}, id="anchor-traversal"),
        pytest.param("build_memory_context", {**ANCHOR, "permalink": "-x"}, id="anchor-flag"),
        pytest.param("build_memory_context", {**ANCHOR, "domain": "-x"}, id="domain-flag"),
        pytest.param("build_memory_context", {**ANCHOR, "domains": []}, id="domains-empty"),
        pytest.param("build_memory_context", {**ANCHOR, "domains": ["--domain=secret"]}, id="extra-domain-flag"),
        pytest.param("build_memory_context", {**ANCHOR, "depth": 0}, id="depth-low"),
        pytest.param("build_memory_context", {**ANCHOR, "depth": 4}, id="depth-high"),
        pytest.param("build_memory_context", {**ANCHOR, "max_related": 0}, id="max-related-low"),
        pytest.param("build_memory_context", {**ANCHOR, "max_related": 101}, id="max-related-high"),
        pytest.param("build_memory_context", {**ANCHOR, "timeframe": "abc"}, id="timeframe-word"),
        pytest.param("build_memory_context", {**ANCHOR, "timeframe": "10"}, id="timeframe-unitless"),
        pytest.param("build_memory_context", {**ANCHOR, "timeframe": "30min"}, id="timeframe-minutes"),
        pytest.param("build_memory_context", {**ANCHOR, "timeframe": "0d"}, id="timeframe-zero"),
        pytest.param("review_memories", {**REVIEW, "domains": []}, id="review-domains-empty"),
        pytest.param("review_memories", {**REVIEW, "families": ["graph"]}, id="review-unknown-family"),
        pytest.param("review_memories", {**REVIEW, "families": []}, id="review-families-empty"),
        pytest.param("review_memories", {**REVIEW, "limit": 101}, id="review-limit-high"),
        pytest.param("review_memories", {**REVIEW, "page": 0}, id="review-page-low"),
        pytest.param("get_memory_vocabulary", {**VOCABULARY, "domain": "a\nb"}, id="vocabulary-newline"),
        pytest.param("get_memory_vocabulary", {"domain": "notes"}, id="vocabulary-missing-workspace"),
    ],
)
async def test_invalid_graph_arguments_fail_before_execution(cli: Path, tool: str, arguments: dict[str, object]) -> None:
    """Reject URLs, globs, out-of-range bounds, and timeframes the engine would silently ignore without starting crystalline."""
    with pytest.raises(ToolError):
        await call(tool, arguments)
    assert calls(cli) == []


@pytest.mark.parametrize(
    "tool,arguments",
    [
        pytest.param("build_memory_context", {**ANCHOR, "domain": "secret"}, id="denied-anchor"),
        pytest.param("build_memory_context", {**ANCHOR, "domains": ["secret"]}, id="denied-neighbourhood"),
        pytest.param("review_memories", {**REVIEW, "domains": ["notes", "secret"]}, id="denied-review"),
        pytest.param("get_memory_vocabulary", {**VOCABULARY, "domain": "secret"}, id="denied-vocabulary"),
    ],
)
async def test_denied_domains_never_execute_graph_tools(cli: Path, tool: str, arguments: dict[str, object]) -> None:
    """Reject the whole request when the anchor or any neighbourhood domain is outside the exact allowlist."""
    async with Client(create_server(Settings(allowed_domains=("notes",)))) as client:
        with pytest.raises(ToolError, match="not permitted"):
            await client.call_tool(tool, arguments)
    assert calls(cli) == []
