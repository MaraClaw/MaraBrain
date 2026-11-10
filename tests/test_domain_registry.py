"""Behavioral contracts for domain snapshots, policy, registration, and persistence."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import anyio
import pytest
from domain_fake import domain_cli as domain_cli
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError
from memory_fake import WORKSPACE_ID, call, calls, domain_list, workspace_registry

from marabrain.config import Settings
from marabrain.domain_registry import DomainRegistry
from marabrain.server import create_server


def argv(cli: Path) -> list[list[str]]:
    """Read the subprocess protocol transcript with its concrete argument types."""
    from pydantic import TypeAdapter

    return TypeAdapter(list[list[str]]).validate_python([record["argv"] for record in calls(cli)])


def rows(*, workspace: bool = False) -> list[str]:
    """Read the durable public registry state after a request completes."""
    registry = workspace_registry() if workspace else DomainRegistry.from_environment(5)
    with closing(sqlite3.connect(registry.storage.root / "domains.sqlite3")) as database:
        return [name for (name,) in database.execute("SELECT name FROM domains ORDER BY name")]


async def test_startup_replaces_complete_snapshot_and_keeps_exact_names(domain_cli: Path) -> None:
    """Replace stale names with exact engine identifiers without registering domains."""
    domain_list(domain_cli, ["Legacy.Name", "archive"])
    await DomainRegistry.from_environment(5).synchronize()
    assert rows() == ["Legacy.Name", "archive"]
    assert argv(domain_cli) == [["--json", "domain", "list"]]


@pytest.mark.parametrize("mode", ["list-exit", "list-malformed", "collision"])
async def test_invalid_snapshot_preserves_previous_registry(domain_cli: Path, mode: str) -> None:
    """Fail synchronization without committing a partial or ambiguous snapshot."""
    if mode == "collision":
        domain_list(domain_cli, ["Notes", "notes"])
    else:
        (domain_cli / "failure").write_text(mode)
    with pytest.raises(ToolError) as error:
        await DomainRegistry.from_environment(5).synchronize()
    assert "secret engine payload" not in str(error.value)
    assert rows() == ["notes"]


async def test_create_registers_normalized_file_domain_and_restart_reads_it(domain_cli: Path) -> None:
    """Initialize a missing domain before writing and reuse its persistent identity after restart."""
    result = await call("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "Project_X", "title": "first", "content": "remember this"})
    assert result == {"domain": "project_x", "permalink": "first"}
    assert rows(workspace=True) == ["notes", "project_x"]
    await workspace_registry().synchronize()
    assert await call("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "PROJECT_X", "permalink": "first"}) == {
        "domain": "project_x",
        "content": "remember this",
    }
    assert sum(arguments[:3] == ["--json", "domain", "add"] for arguments in argv(domain_cli)) == 1


async def test_existing_domain_retains_exact_identifier_and_path(domain_cli: Path, tmp_path: Path) -> None:
    """Write through a case variant without replacing a preexisting domain or its contents."""
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.md").write_text("original")
    (domain_cli / "domains.json").write_text(json.dumps([{"name": "Legacy", "path": str(existing)}]))
    await workspace_registry().synchronize()
    assert await call("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "LEGACY", "title": "new", "content": "new body"}) == {
        "domain": "Legacy",
        "permalink": "new",
    }
    assert (existing / "keep.md").read_text() == "original"
    assert (existing / "new.md").read_text() == "new body"
    assert rows(workspace=True) == ["Legacy"]
    assert all(arguments[:3] != ["--json", "domain", "add"] for arguments in argv(domain_cli))


@pytest.mark.parametrize("name", ["../escape", "a/b", "a\\b", ".", "..", "a..b", "hello world", "café", "a" * 65])
async def test_unsafe_new_names_never_register(domain_cli: Path, name: str) -> None:
    """Refuse unsafe names without creating a domain directory or invoking a mutation."""
    with pytest.raises(ToolError):
        await call("create_memory", {"workspace_id": WORKSPACE_ID, "domain": name, "title": "note", "content": "body"})
    assert rows(workspace=True) == ["notes"]
    assert argv(domain_cli) == [["--json", "domain", "list"]]


@pytest.mark.parametrize("link", ["domains", "domains/new", "domains/new/MANIFEST.md", "domains.sqlite3"])
async def test_symlink_storage_never_mutates_external_paths(domain_cli: Path, tmp_path: Path, link: str) -> None:
    """Reject symlinked storage components before passing a path to the engine."""
    root = workspace_registry().storage.root
    outside = tmp_path / "outside"
    outside.mkdir()
    async with Client(create_server(Settings())) as client:
        await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "ready", "domains": ["notes"]})
        (domain_cli / "calls.jsonl").unlink()
        target = root / link
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        target.symlink_to(outside)
        with pytest.raises(ToolError, match="unsafe"):
            await client.call_tool("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "new", "title": "note", "content": "body"})
    assert list(outside.iterdir()) == []
    assert all(arguments[:3] != ["--json", "domain", "init"] for arguments in argv(domain_cli))


async def test_policy_rejects_before_registration_and_checks_resolved_identifier(domain_cli: Path) -> None:
    """Prevent normalization from widening the exact-name authorization boundary."""
    async with Client(create_server(Settings(allowed_domains=("Notes",)))) as client:
        with pytest.raises(ToolError, match="not permitted"):
            await client.call_tool("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "private", "title": "secret", "content": "secret"})
        assert calls(domain_cli) == []
        with pytest.raises(ToolError, match="not permitted"):
            await client.call_tool("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "Notes", "title": "secret", "content": "secret"})
    assert argv(domain_cli) == [["--json", "domain", "list"]]
    assert rows(workspace=True) == ["notes"]


@pytest.mark.parametrize("mode,expected", [("add-exit", ["notes"]), ("add-uncertain", ["new", "notes"])])
async def test_failed_add_never_writes_memory_and_reconciles_engine(domain_cli: Path, mode: str, expected: list[str]) -> None:
    """Record only authoritative registrations after a failed or uncertain engine mutation."""
    (domain_cli / "failure").write_text(mode)
    with pytest.raises(ToolError, match="exit code 8"):
        await call("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "new", "title": "note", "content": "body"})
    assert rows(workspace=True) == expected
    assert all(arguments[1] != "write" for arguments in argv(domain_cli))
    (domain_cli / "failure").unlink()
    assert await call("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "new", "title": "after", "content": "after"}) == {
        "domain": "new",
        "permalink": "after",
    }


async def test_concurrent_servers_register_once_and_preserve_each_memory(domain_cli: Path) -> None:
    """Serialize registrations across independent server objects without losing writes."""

    async def create(client: Client[FastMCPTransport], title: str) -> None:
        """Create a distinct memory through a separate registry instance."""
        result = await client.call_tool("create_memory", {"workspace_id": WORKSPACE_ID, "domain": "NEW", "title": title, "content": title})
        assert result.structured_content == {
            "domain": "new",
            "permalink": title,
        }

    async with Client(create_server(Settings())) as first, Client(create_server(Settings())) as second, Client(create_server(Settings())) as third:
        for client in (first, second, third):
            await client.call_tool("search_memories", {"workspace_id": WORKSPACE_ID, "query": "ready", "domains": ["notes"]})
        (domain_cli / "calls.jsonl").unlink()
        async with anyio.create_task_group() as tasks:
            for client, title in ((first, "first"), (second, "second"), (third, "third")):
                tasks.start_soon(create, client, title)
    assert rows(workspace=True) == ["new", "notes"]
    for title in ("first", "second", "third"):
        assert await call("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "new", "permalink": title}) == {"domain": "new", "content": title}
    assert sum(arguments[:3] == ["--json", "domain", "add"] for arguments in argv(domain_cli)) == 1
