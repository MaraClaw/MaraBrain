"""Real application startup and shared-registry behavior across server processes."""

import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from domain_fake import domain_cli as domain_cli
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from test_domain_registry import argv, rows
from test_engine_lifecycle import activation_failed, application, assert_reaped, await_file, begin_workspace
from test_engine_lifecycle import runtime as runtime
from memory_fake import WORKSPACE_ID, workspace_context, workspace_registry


@pytest.mark.parametrize("mode", ["managed", "external"])
async def test_invalid_domain_snapshot_fails_workspace_activation(runtime: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Refuse an activated workspace in either engine mode when its domain snapshot is invalid."""
    monkeypatch.setenv("MARABRAIN_ENGINE_MODE", mode)
    monkeypatch.setenv("DOMAIN_LIST_FAILURE", "1")
    external = None
    if mode == "external":
        external = await anyio.open_process(
            [str(runtime / "crystalline"), "serve", "--http", "off"],
            env=dict(workspace_context().environment),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    try:
        if external is not None:
            await await_file(runtime / "owner")
        async with application(runtime) as process:
            reader = await begin_workspace(process)
            await activation_failed(process, reader)
        if external is not None:
            assert external.returncode is None
    finally:
        if external is not None:
            external.terminate()
            await external.wait()
            await external.aclose()
    assert_reaped(runtime)


@pytest.mark.parametrize("names", [("NEW", "new"), ("first", "second")])
async def test_processes_share_registration_lock_and_retain_all_memories(domain_cli: Path, names: tuple[str, str]) -> None:
    """Prevent duplicate registration and lost engine updates across independent processes."""

    async def create(name: str, title: str) -> None:
        """Create a memory through a real module-launched MCP process."""
        transport = StdioTransport(
            sys.executable,
            ["-m", "marabrain"],
            env={**os.environ, "MARABRAIN_TRANSPORT": "stdio", "MARABRAIN_ENGINE_MODE": "external"},
        )
        async with Client(transport) as client:
            result = await client.call_tool("create_memory", {"workspace_id": WORKSPACE_ID, "domain": name, "title": title, "content": title})
            assert result.structured_content == {"domain": name.lower(), "permalink": title}

    with anyio.fail_after(20):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(create, names[0], "one")
            tasks.start_soon(create, names[1], "two")
    assert rows(workspace=True) == sorted({"notes", *(name.lower() for name in names)})
    root = workspace_registry().storage.root / "domains"
    assert (root / names[0].lower() / "one.md").read_text() == "one"
    assert (root / names[1].lower() / "two.md").read_text() == "two"
    assert sum(arguments[:3] == ["--json", "domain", "add"] for arguments in argv(domain_cli)) == len(set(name.lower() for name in names))
