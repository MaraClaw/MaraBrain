"""Real application startup and shared-registry behavior across server processes."""

import os
import sys
from pathlib import Path

import anyio
import pytest
from domain_fake import domain_cli as domain_cli
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from test_domain_registry import argv, rows
from test_engine_lifecycle import application, assert_reaped
from test_engine_lifecycle import runtime as runtime


@pytest.mark.parametrize("mode", ["managed", "external"])
async def test_invalid_domain_snapshot_fails_before_transport(runtime: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Refuse to serve either engine mode when startup cannot validate its domain snapshot."""
    monkeypatch.setenv("MARABRAIN_ENGINE_MODE", mode)
    monkeypatch.setenv("DOMAIN_LIST_FAILURE", "1")
    async with application(runtime) as process:
        with anyio.fail_after(10):
            assert await process.wait() != 0
        assert process.stdout is not None
        assert b"".join([chunk async for chunk in process.stdout]) == b""
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
            result = await client.call_tool("create_memory", {"domain": name, "title": title, "content": title})
            assert result.structured_content == {"domain": name.lower(), "permalink": title}

    with anyio.fail_after(20):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(create, names[0], "one")
            tasks.start_soon(create, names[1], "two")
    assert rows() == sorted({"notes", *(name.lower() for name in names)})
    root = domain_cli.parent / "state" / "marabrain" / "domains"
    assert (root / names[0].lower() / "one.md").read_text() == "one"
    assert (root / names[1].lower() / "two.md").read_text() == "two"
    assert sum(arguments[:3] == ["--json", "domain", "add"] for arguments in argv(domain_cli)) == len(set(name.lower() for name in names))
