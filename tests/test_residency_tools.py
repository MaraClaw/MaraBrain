"""Verify memory-tool leases and compression survival at MCP and child-process boundaries."""

import json
import os
from pathlib import Path

import anyio
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import JsonValue
from residency_support import assert_reaped, child_gates, context_for, managed_settings, resident_cli as resident_cli
from test_workspaces import records

from marabrain.domain_registry import DomainRegistry
from marabrain.crystalline import JsonObject
from marabrain.llm_config import LLMSettings
from marabrain.server import create_server


@pytest.mark.parametrize("phase", ["registry", "command"])
@pytest.mark.parametrize(
    "tool,arguments,command",
    [
        ("create_memory", {"domain": "notes", "title": "other", "content": "body"}, "write"),
        ("read_memory", {"domain": "notes", "permalink": "note"}, "read"),
        ("edit_memory", {"domain": "notes", "permalink": "note", "operation": {"type": "append", "content": " edited"}}, "edit"),
        ("delete_memory", {"domain": "notes", "permalink": "note"}, "delete"),
        ("search_memories", {"domains": ["notes"], "query": "body"}, "search"),
    ],
)
async def test_each_memory_tool_pins_registry_and_final_command(
    resident_cli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, phase: str, tool: str, arguments: dict[str, JsonValue], command: str
) -> None:
    """Refuse capacity pressure throughout each tool's registry and native command phases."""
    root, _ = resident_cli
    entered, release = anyio.Event(), anyio.Event()
    original = DomainRegistry.resolve

    async def resolve(registry: DomainRegistry, names: list[str], *, create: bool = False, deadline: float | None = None) -> list[str]:
        """Hold the real registry boundary before resolving and closing its SQLite worker."""
        entered.set()
        await release.wait()
        return await original(registry, names, create=create, deadline=deadline)

    async with child_gates(monkeypatch) as gates, Client(create_server(managed_settings())) as client:
        await client.call_tool("create_memory", {"workspace_id": "alpha", "domain": "notes", "title": "note", "content": "body"})
        state = context_for("alpha", root).state_home
        pid = int((state / "owner").read_text())
        if phase == "registry":
            monkeypatch.setattr(DomainRegistry, "resolve", resolve)
        else:
            (state / f"gate-{command}").touch()

        async def request() -> None:
            """Run the actual tool after its workspace has already become idle once."""
            result = await client.call_tool(tool, {"workspace_id": "alpha", **arguments})
            assert not result.is_error

        with anyio.fail_after(15):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(request)
                gate = None
                if phase == "registry":
                    await entered.wait()
                else:
                    gate = await gates.receive()
                    assert gate.event.phase == command
                try:
                    with pytest.raises(ToolError, match="^Resident workspace capacity is busy\\.$"):
                        await client.call_tool("search_memories", {"workspace_id": "beta", "domains": ["notes"], "query": "body"})
                    os.kill(pid, 0)
                finally:
                    release.set()
                    if gate is not None:
                        gate.release.set()
        result = await client.call_tool("search_memories", {"workspace_id": "beta", "domains": ["notes"], "query": "body"})
        assert result.data == {"results": []}
        assert_reaped(pid)


async def test_retirement_preserves_storage_compression_flights_and_cached_entries(resident_cli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Retire a compressed reader, resync aliases on reactivation, and reuse its application-owned work."""
    root, events = resident_cli
    entered, release, follower = anyio.Event(), anyio.Event(), anyio.Event()
    calls: list[str] = []
    results: list[JsonObject] = []

    async def compress(settings: LLMSettings, query: str, content: str) -> str:
        """Hold provider work independently of any resident daemon's lifetime."""
        calls.append(content)
        entered.set()
        await release.wait()
        return "compressed body"

    async def progress(progress: float, total: float | None, message: str | None) -> None:
        """Expose arrival of the reactivated reader at its provider phase."""
        follower.set()

    monkeypatch.setattr("marabrain.memory_tools.compress_memory", compress)
    llm = LLMSettings.model_validate(
        {"model": "model", "provider_url": "https://provider.test/v1", "compression_cache_max_entries": 2, "compression_cache_max_bytes": 1024}
    )
    settings = managed_settings().model_copy(update={"llm": llm})
    async with Client(create_server(settings)) as client:
        await client.call_tool("create_memory", {"workspace_id": "alpha", "domain": "notes", "title": "note", "content": "body"})
        context = context_for("alpha", root)
        old_pid = int((context.state_home / "owner").read_text())
        persistent = [Path(context.environment[name]) / "keep" for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")]
        for path in persistent:
            path.write_text("persistent")
        arguments: dict[str, JsonValue] = {"workspace_id": "alpha", "domain": "notes", "permalink": "note", "compress": True, "query": "topic"}

        async def read(alias: bool = False) -> None:
            """Collect the native source envelope after shared compression finishes."""
            result = await client.call_tool(
                "read_memory", {**arguments, "domain": "shortcut" if alias else "notes"}, progress_handler=progress if alias else None
            )
            results.append(result.data)

        with anyio.fail_after(20):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(read)
                await entered.wait()
                try:
                    await client.call_tool("search_memories", {"workspace_id": "beta", "domains": ["notes"], "query": "body"})
                    assert_reaped(old_pid)
                    domains_path = context.state_home / "fake-domains.json"
                    domains = json.loads(domains_path.read_text())
                    domains[0]["aliases"] = ["shortcut"]
                    domains_path.write_text(json.dumps(domains))
                    tasks.start_soon(read, True)
                    await follower.wait()
                    await anyio.wait_all_tasks_blocked()
                    assert calls == ["body"]
                    assert results == []
                finally:
                    release.set()
        assert len(results) == 2
        assert all(result["content"] == "compressed body" and result["compressed"] is True for result in results)
        await client.call_tool("search_memories", {"workspace_id": "beta", "domains": ["notes"], "query": "body"})
        await read(True)
        assert calls == ["body"]
        assert all(path.read_text() == "persistent" for path in persistent)
        native = (await client.call_tool("read_memory", {**arguments, "compress": False})).data
        assert native["content"] == "body" and "compressed" not in native
        assert native["checksum"] == results[0]["checksum"]
    assert len({row.pid for row in records(events) if row.argv == ["serve", "--http", "off"]}) == 5
    for row in records(events):
        assert_reaped(row.pid)
