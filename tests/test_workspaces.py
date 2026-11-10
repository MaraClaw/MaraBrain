"""Verify required workspace routing, independent stores, and concurrent activation at MCP boundaries."""

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio
import pytest
from anyio.abc import TaskStatus
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, JsonValue
from workspace_fake import SCRIPT

from marabrain.config import Settings
from marabrain.engine_context import EngineContext
from marabrain.server import create_server
from marabrain.workspaces import WorkspaceManager
from marabrain.workspace_storage import prepare_workspace


class CommandEvent(BaseModel):
    """Parse subprocess evidence at the JSON boundary."""

    model_config = ConfigDict(frozen=True)
    argv: list[str]
    state: str
    pid: int


@pytest.fixture
def workspace_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    """Use a short private workspace root and a stateful native-command fake."""
    for name in tuple(os.environ):
        if name.startswith(("MARABRAIN_", "EMBEDDING_", "CRYSTALLINE_")):
            monkeypatch.delenv(name)
    executable = tmp_path / "crystalline"
    executable.write_text(SCRIPT)
    executable.chmod(0o755)
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("WORKSPACE_TEST_EVENTS", str(events))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "legacy-state"))
    with TemporaryDirectory(prefix="mbw-", dir="/private/tmp" if sys.platform == "darwin" else "/tmp") as directory:
        root = Path(directory) / "workspaces"
        monkeypatch.setenv("MARABRAIN_WORKSPACE_ROOT", str(root))
        yield root, events


def records(events: Path) -> list[CommandEvent]:
    """Read the subprocess event stream for independently observable dispatch evidence."""
    return [CommandEvent.model_validate_json(line) for line in events.read_text().splitlines()]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "", "../other", "/absolute", "a/b", "a\\b", "a..b", " space", "tenant\n", "tenanté", "a" * 65, 1, True])
@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("create_memory", {"domain": "notes", "title": "shared", "content": "private"}),
        ("read_memory", {"domain": "notes", "permalink": "shared"}),
        ("edit_memory", {"domain": "notes", "permalink": "shared", "operation": {"type": "append", "content": "private"}}),
        ("delete_memory", {"domain": "notes", "permalink": "shared"}),
        ("search_memories", {"query": "private", "domains": ["notes"]}),
    ],
)
async def test_invalid_workspace_never_dispatches(workspace_cli: tuple[Path, Path], tool: str, arguments: dict[str, JsonValue], invalid: JsonValue) -> None:
    """Reject invalid and missing workspace identities before touching the filesystem or engine."""
    root, events = workspace_cli
    async with Client(create_server(Settings())) as client:
        for supplied in (arguments, {**arguments, "workspace_id": invalid}):
            with pytest.raises(ToolError):
                await client.call_tool(tool, supplied)
    assert not root.exists()
    assert not events.exists()


@pytest.mark.asyncio
async def test_workspace_crud_search_and_restart_are_isolated(workspace_cli: tuple[Path, Path]) -> None:
    """Keep identical domain names and permalinks independent across concurrent tenants and restart."""
    _, events = workspace_cli
    async with Client(create_server(Settings())) as client:

        async def create(workspace_id: str) -> None:
            """Create the same identity in a distinct workspace concurrently."""
            await client.call_tool("create_memory", {"workspace_id": workspace_id, "domain": "notes", "title": "shared", "content": workspace_id})

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(create, "Tenant-A")
            tasks.start_soon(create, "tenant-a")
        await client.call_tool(
            "edit_memory",
            {"workspace_id": "Tenant-A", "domain": "notes", "permalink": "shared", "operation": {"type": "append", "content": " edited"}},
        )
        for workspace_id, expected in (("Tenant-A", "Tenant-A edited"), ("tenant-a", "tenant-a")):
            read = await client.call_tool("read_memory", {"workspace_id": workspace_id, "domain": "notes", "permalink": "shared"})
            assert read.data["content"] == expected
            search = await client.call_tool("search_memories", {"workspace_id": workspace_id, "domains": ["notes"], "query": workspace_id})
            assert [hit["content"] for hit in search.data["results"]] == [expected]
        await client.call_tool("delete_memory", {"workspace_id": "Tenant-A", "domain": "notes", "permalink": "shared"})
    async with Client(create_server(Settings())) as restarted:
        read = await restarted.call_tool("read_memory", {"workspace_id": "tenant-a", "domain": "notes", "permalink": "shared"})
        assert read.data["content"] == "tenant-a"
        search = await restarted.call_tool("search_memories", {"workspace_id": "Tenant-A", "domains": ["notes"], "query": "Tenant-A"})
        assert search.data["results"] == []
    assert len({row.state for row in records(events)}) == 2


@pytest.mark.asyncio
async def test_workspace_contexts_clear_legacy_selectors_and_share_global_limits(workspace_cli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind private paths without inheriting selectors that can redirect engine data."""
    root, _ = workspace_cli
    monkeypatch.setenv("CRYSTALLINE_CONFIG", "/unrelated/config.yaml")
    monkeypatch.setenv("CRYSTALLINE_DB", "/unrelated/index.db")
    monkeypatch.setenv("CRYSTALLINE_DOMAIN", "legacy")
    base = EngineContext.for_application(Settings(max_transient_processes=3))
    left = base.for_workspace("left", root)
    right = base.for_workspace("right", root)
    assert left.transients is right.transients is base.transients
    assert left.state_home != right.state_home != base.state_home
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        assert left.environment[name] != right.environment[name]
        assert Path(left.environment[name]).is_relative_to(root)
    assert not any(name.startswith("CRYSTALLINE_") for name in left.environment)
    assert base.environment["CRYSTALLINE_DB"] == "/unrelated/index.db"


@pytest.mark.asyncio
async def test_concurrent_workspace_activation_coalesces_and_domain_capacity_is_private(workspace_cli: tuple[Path, Path]) -> None:
    """Join one activation per workspace while giving equal domain names separate permits."""
    _, events = workspace_cli
    settings = Settings(domain_command_capacity=1)
    manager = WorkspaceManager(settings, EngineContext.for_application(settings))
    async with manager.lifespan():
        contexts: list[EngineContext] = []

        async def activate() -> None:
            """Join concurrent first use of the same workspace."""
            contexts.append((await manager.get("alpha")).context)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(activate)
            tasks.start_soon(activate)
        assert contexts[0] is contexts[1]
        alpha, beta = await manager.get("alpha"), await manager.get("beta")
        with alpha.admission.admit(["notes"]), beta.admission.admit(["notes"]):
            with pytest.raises(ToolError):
                with alpha.admission.admit(["notes"]):
                    pytest.fail("The occupied workspace domain admitted another command.")
    assert sum(row.argv == ["--json", "domain", "list"] for row in records(events)) == 2


@pytest.mark.asyncio
async def test_health_checks_activated_workspaces_without_selecting_legacy_engine(workspace_cli: tuple[Path, Path]) -> None:
    """Report idle readiness and fail once an activated workspace's daemon becomes unavailable."""
    _, events = workspace_cli
    async with Client(create_server(Settings())) as client:
        assert (await client.call_tool("health")).structured_content == {"status": "ok", "version": "0.4.5"}
        assert not events.exists()
        await client.call_tool("create_memory", {"workspace_id": "alpha", "domain": "notes", "title": "shared", "content": "private"})
        assert (await client.call_tool("health")).structured_content == {"status": "ok", "version": "0.4.5"}
        (Path(records(events)[0].state) / "daemon-down").touch()
        with pytest.raises(ToolError):
            await client.call_tool("health")


@pytest.mark.asyncio
async def test_cancelled_activation_waiter_does_not_cancel_another_tenant_request(workspace_cli: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a shared initialization alive until the remaining caller receives its runtime."""
    _, events = workspace_cli
    monkeypatch.setenv("WORKSPACE_PROBE_DELAY", "0.2")
    settings = Settings()
    manager = WorkspaceManager(settings, EngineContext.for_application(settings))
    completed = anyio.Event()

    async def departing(*, task_status: TaskStatus[anyio.CancelScope]) -> None:
        """Join first use with a caller-owned cancellable scope."""
        with anyio.CancelScope() as scope:
            task_status.started(scope)
            await manager.get("alpha")

    async def remaining() -> None:
        """Retain independent ownership of the shared activation."""
        await manager.get("alpha")
        completed.set()

    async with manager.lifespan():
        async with anyio.create_task_group() as tasks:
            scope = await tasks.start(departing)
            tasks.start_soon(remaining)
            with anyio.fail_after(3):
                while not events.exists():
                    await anyio.sleep(0.005)
                scope.cancel()
                await completed.wait()
        assert await manager.health()
    assert sum(row.argv == ["--json", "domain", "list"] for row in records(events)) == 1
    for row in records(events):
        with pytest.raises(ProcessLookupError):
            os.kill(row.pid, 0)


@pytest.mark.asyncio
async def test_activation_resource_refusal_can_recover_on_a_later_request(workspace_cli: tuple[Path, Path]) -> None:
    """Release a failed activation so temporary shared-capacity saturation cannot poison a workspace."""
    settings = Settings(max_transient_processes=2)
    context = EngineContext.for_application(settings)
    manager = WorkspaceManager(settings, context)
    async with manager.lifespan():
        with context.transients.admit(control=True), pytest.raises(ToolError, match="transient process capacity"):
            await manager.get("alpha")
        runtime = await manager.get("alpha")
        assert runtime.context.state_home != context.state_home
        assert await manager.health()


def test_workspace_root_symlink_cannot_adopt_external_data(workspace_cli: tuple[Path, Path], tmp_path: Path) -> None:
    """Refuse redirected storage before writing any identity or domain into its target."""
    root, events = workspace_cli
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolError, match="unsafe"):
        prepare_workspace("alpha", root)
    assert list(outside.iterdir()) == []
    assert not events.exists()
