"""Preserve effective engine identities across every memory tool and exact policies."""

import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memory_fake import call
from test_domain_registry import argv, rows

from marabrain.config import Settings
from marabrain.domain_registry import DomainRegistry
from marabrain.server import create_server


@pytest.mark.parametrize("spelling", ["LEGACY", "CANONICAL", "ALIAS"])
@pytest.mark.parametrize(
    "tool,arguments,expected",
    [
        ("create_memory", {"title": "one", "content": "body"}, ["write", "Legacy", "one"]),
        ("read_memory", {"permalink": "one"}, ["read", "one", "--domain=Legacy"]),
        ("edit_memory", {"permalink": "one", "operation": {"type": "append", "content": "body"}}, ["edit", "one", "Legacy", "append"]),
        ("delete_memory", {"permalink": "one"}, ["delete", "one", "Legacy", "--force"]),
        ("search_memories", {"query": "body"}, ["search", "--domain=Legacy", "--limit=10", "--page=1", "body"]),
    ],
)
async def test_tools_resolve_existing_names_without_registering(cli: Path, spelling: str, tool: str, arguments: dict[str, object], expected: list[str]) -> None:
    """Use the exact registration key for case variants and effective engine aliases."""
    (cli / "domains.json").write_text(json.dumps([{"name": "Legacy", "canonical_name": "canonical", "aliases": ["alias"]}]))
    await DomainRegistry.from_environment(5).synchronize()
    (cli / "calls.jsonl").unlink()
    domain_arguments = {"domains": [spelling]} if tool == "search_memories" else {"domain": spelling}
    await call(tool, {**arguments, **domain_arguments})
    assert argv(cli) == [["--json", *expected]]
    assert rows() == ["Legacy"]


@pytest.mark.parametrize(
    "snapshot",
    [
        [{"name": "first", "aliases": ["SHARED"]}, {"name": "second", "aliases": ["shared"]}],
        [{"name": "first", "canonical_name": "shared"}, {"name": "second", "canonical_name": "shared"}],
    ],
)
async def test_ambiguous_alias_normalization_preserves_snapshot(cli: Path, snapshot: list[dict[str, object]]) -> None:
    """Refuse to merge different registrations that normalize to one effective identifier."""
    (cli / "domains.json").write_text(json.dumps(snapshot))
    with pytest.raises(ToolError, match="ambiguous"):
        await DomainRegistry.from_environment(5).synchronize()
    assert rows() == ["notes"]


async def test_allowlist_requires_exact_resolved_registration_before_alias_write(cli: Path) -> None:
    """Deny an admitted alias when its resolved registration is outside the allowlist."""
    (cli / "domains.json").write_text(json.dumps([{"name": "private", "aliases": ["allowed"]}]))
    await DomainRegistry.from_environment(5).synchronize()
    (cli / "calls.jsonl").unlink()
    async with Client(create_server(Settings(allowed_domains=("allowed",)))) as client:
        with pytest.raises(ToolError, match="not permitted"):
            await client.call_tool("create_memory", {"domain": "allowed", "title": "one", "content": "body"})
    assert argv(cli) == []


async def test_shared_registry_refuses_another_engine_configuration(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to silently reuse one state directory for another configuration identity."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cli / "other-config"))
    with pytest.raises(ToolError, match="another Crystalline configuration"):
        await DomainRegistry.from_environment(5).synchronize()
    assert rows() == ["notes"]
    assert argv(cli) == []


async def test_shadowed_canonical_name_keeps_exact_registration_precedence(cli: Path) -> None:
    """Retain the pinned engine's local-name precedence when another manifest uses that name."""
    (cli / "domains.json").write_text(json.dumps([{"name": "first", "canonical_name": "second", "shadowed": True}, {"name": "second"}]))
    await DomainRegistry.from_environment(5).synchronize()
    (cli / "calls.jsonl").unlink()
    await call("read_memory", {"domain": "second", "permalink": "one"})
    assert argv(cli) == [["--json", "read", "one", "--domain=second"]]
    assert rows() == ["first", "second"]


@pytest.mark.parametrize("shadowed", ["true", "false", 1, 0, None])
async def test_malformed_shadowing_metadata_never_replaces_snapshot_or_registers(cli: Path, shadowed: object) -> None:
    """Reject coercible shadowing flags before losing an existing canonical route."""
    (cli / "domains.json").write_text(json.dumps([{"name": "local", "canonical_name": "canonical", "shadowed": shadowed}]))
    with pytest.raises(ToolError, match="invalid domain list"):
        await DomainRegistry.from_environment(5).synchronize()
    with pytest.raises(ToolError, match="invalid domain list"):
        await call("create_memory", {"domain": "canonical", "title": "one", "content": "body"})
    assert rows() == ["notes"]
    assert argv(cli) == [["--json", "domain", "list"], ["--json", "domain", "list"]]
