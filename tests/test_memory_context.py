"""Verify captured engine environments through independent MCP memory servers."""

import sys
import pwd
import os
import sqlite3
from contextlib import closing
from collections.abc import MutableMapping
from dataclasses import FrozenInstanceError
from pathlib import Path

import anyio
import pytest
from fastmcp import Client, FastMCP

from marabrain.memory_tools import register_memory_tools
from marabrain.engine_context import EngineContext
from marabrain.domain_registry import DomainRegistry
from fastmcp.exceptions import ToolError


@pytest.mark.parametrize("root_mode", ["defaults", "explicit", "relative", "tilde"])
@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("create_memory", {"domain": "notes", "title": "note", "content": "body"}),
        ("read_memory", {"domain": "notes", "permalink": "note"}),
        ("edit_memory", {"domain": "notes", "permalink": "note", "operation": {"type": "append", "content": "body"}}),
        ("delete_memory", {"domain": "notes", "permalink": "note"}),
        ("search_memories", {"domains": ["notes"], "query": "body"}),
    ],
)
async def test_memory_servers_retain_home_defaults_after_environment_changes(
    cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, object], root_mode: str
) -> None:
    """Keep every tool on its captured HOME and default XDG roots when servers overlap."""
    (cli / "crystalline").write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "if sys.argv[2:] == ['domain', 'list']:\n"
        "    print(json.dumps({'domains': [{'name': 'notes'}]}))\n"
        "else:\n"
        "    home = pathlib.Path(os.environ['HOME'])\n"
        "    config = pathlib.Path(os.environ.get('CRYSTALLINE_CONFIG') or pathlib.Path(os.environ['XDG_CONFIG_HOME']) / 'crystalline/config.yaml')\n"
        "    print(json.dumps({'home': str(home), 'state': os.environ.get('XDG_STATE_HOME', str(home / '.local/state')), 'selected': config.read_text()}))\n"
    )
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CRYSTALLINE_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    servers: list[tuple[FastMCP, Path, Path, str]] = []
    for name in ("first", "second"):
        home = tmp_path / name
        monkeypatch.setenv("HOME", str(home))
        state = tmp_path / f"{name}-state" if root_mode == "explicit" else home / ".local/state"
        config = tmp_path / f"{name}.yaml" if root_mode == "explicit" else home / ".config/crystalline/config.yaml"
        if root_mode == "tilde":
            config = home / "selected.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(name)
        if root_mode == "explicit":
            monkeypatch.setenv("XDG_STATE_HOME", str(state))
            monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / f"{name}-config"))
            monkeypatch.setenv("CRYSTALLINE_CONFIG", str(config))
        if root_mode == "relative":
            for variable in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
                monkeypatch.setenv(variable, "relative-root")
        if root_mode == "tilde":
            monkeypatch.setenv("CRYSTALLINE_CONFIG", "~/selected.yaml")
        server = FastMCP(name)
        register_memory_tools(server)
        servers.append((server, home, state, name))
    monkeypatch.setenv("HOME", str(tmp_path / "ambient"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ambient-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ambient-state"))
    monkeypatch.setenv("CRYSTALLINE_CONFIG", str(tmp_path / "ambient.yaml"))

    async def request(server: FastMCP, home: Path, state: Path, selected: str) -> None:
        """Assert that the command resolves the bound engine state through MCP."""
        async with Client(server) as client:
            assert (await client.call_tool(tool, arguments)).structured_content == {"home": str(home), "state": str(state), "selected": selected}
        assert (state / "marabrain/domains.sqlite3").is_file()

    async with anyio.create_task_group() as tasks:
        for server, home, state, selected in servers:
            tasks.start_soon(request, server, home, state, selected)


@pytest.mark.parametrize("direct", [False, True])
def test_context_is_immutable_and_redacts_credentials(monkeypatch: pytest.MonkeyPatch, direct: bool) -> None:
    """Keep captured credentials immutable and absent from representations and config identity."""
    monkeypatch.setenv("CRYSTALLINE_EMBEDDINGS_API_KEY", "engine-secret")
    monkeypatch.setenv("EMBEDDING_API_KEY", "provider-secret")
    captured = EngineContext.capture()
    supplied = dict(captured.environment)
    context = EngineContext(supplied, captured.state_home, captured.config_identity) if direct else captured
    supplied["CRYSTALLINE_EMBEDDINGS_API_KEY"] = "caller-changed-secret"
    monkeypatch.setenv("CRYSTALLINE_EMBEDDINGS_API_KEY", "different-secret")
    assert context.environment["CRYSTALLINE_EMBEDDINGS_API_KEY"] == "engine-secret"
    assert not isinstance(context.environment, MutableMapping)
    assert "engine-secret" not in repr(context) + context.config_identity
    assert "provider-secret" not in repr(context) + context.config_identity
    with pytest.raises(FrozenInstanceError):
        setattr(context, "state_home", Path("/changed"))
    assert context.config_identity != EngineContext.capture().config_identity


@pytest.mark.parametrize("home", [None, ""])
def test_context_uses_account_home_when_home_is_absent(monkeypatch: pytest.MonkeyPatch, home: str | None) -> None:
    """Match the engine's account fallback for both missing and empty HOME values."""
    monkeypatch.delenv("HOME", raising=False)
    if home is not None:
        monkeypatch.setenv("HOME", home)
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    context = EngineContext.capture()
    expected = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert context.environment["HOME"] == str(expected)
    assert context.state_home == expected / ".local/state"


@pytest.mark.parametrize("override", ["CRYSTALLINE_CONFIG", "CRYSTALLINE_EMBEDDINGS_MODEL", "CRYSTALLINE_EMBEDDINGS_API_KEY"])
async def test_registry_refuses_other_captured_configuration(cli: Path, monkeypatch: pytest.MonkeyPatch, override: str) -> None:
    """Prevent reuse of durable routes for another config selection or engine override."""
    original = DomainRegistry.from_environment(5)
    monkeypatch.setenv(override, "other-configuration")
    changed = DomainRegistry.from_environment(5)
    with pytest.raises(ToolError, match="another Crystalline configuration"):
        await changed.resolve(["notes"])
    assert await original.resolve(["notes"]) == ["notes"]


@pytest.mark.parametrize("selection", ["same", "other-directory", "override"])
async def test_legacy_registry_reconciles_only_its_original_configuration(cli: Path, monkeypatch: pytest.MonkeyPatch, selection: str) -> None:
    """Upgrade matching legacy metadata through reconciliation without admitting other engine configurations."""
    original = DomainRegistry.from_environment(5)
    legacy = Path(original.context.environment["XDG_CONFIG_HOME"]) / "crystalline"
    with closing(sqlite3.connect(original.storage.root / "domains.sqlite3")) as database:
        database.execute("UPDATE metadata SET value=? WHERE key='config'", (str(legacy),))
        database.execute("UPDATE domains SET name='stale', normalized='stale'")
        database.commit()
    if selection == "other-directory":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(cli / "different-config"))
    if selection == "override":
        monkeypatch.setenv("CRYSTALLINE_CONFIG", str(legacy / "other.yaml"))
    registry = DomainRegistry.from_environment(5)
    if selection == "same":
        assert await registry.resolve(["NOTES"]) == ["notes"]
        with closing(sqlite3.connect(registry.storage.root / "domains.sqlite3")) as database:
            assert database.execute("SELECT name FROM domains").fetchall() == [("notes",)]
            assert database.execute("SELECT value FROM metadata WHERE key='config'").fetchone() == (registry.context.config_identity,)
    else:
        with pytest.raises(ToolError, match="another Crystalline configuration"):
            await registry.resolve(["notes"])
