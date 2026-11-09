"""Preserve effective engine identities across every memory tool and exact policies."""

import json
import os
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream
from anyio.streams.memory import MemoryObjectReceiveStream
from domain_fake import domain_cli as domain_cli
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from memory_fake import FAKE_CLI, RECEIPT, call, calls
from test_domain_registry import argv, rows

from marabrain.config import Settings
from marabrain.domain_registry import DomainRegistry
from marabrain.memory_tools import register_memory_tools
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


@asynccontextmanager
async def command_barrier(cli: Path) -> AsyncGenerator[tuple[MemoryObjectReceiveStream[int], anyio.Event]]:
    """Hold real final commands containing held until explicitly released and check teardown."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    script = (cli / "crystalline").read_text()
    marker = 'if plan["hang"]:' if script == FAKE_CLI else 'elif args[0] == "read":'
    gate = (
        "if 'held' in sys.argv:\n"
        "    import socket\n"
        f"    with socket.create_connection(('127.0.0.1', {port})) as connection:\n"
        "        connection.sendall(str(os.getpid()).encode())\n"
        "        connection.recv(1)\n"
    )
    if script == FAKE_CLI:
        script = script.replace(marker, gate + marker)
    else:
        script = script.replace("domains = json.loads", gate + "domains = json.loads")
    (cli / "crystalline").write_text(script)
    send, receive = anyio.create_memory_object_stream[int](10)
    release = anyio.Event()
    pids: list[int] = []

    async def hold(connection: SocketStream) -> None:
        """Report child launch before retaining the live child at its socket barrier."""
        async with connection:
            pid = int(await connection.receive())
            pids.append(pid)
            await send.send(pid)
            await release.wait()
            await connection.send(b"x")

    async with listener, send, receive, anyio.create_task_group() as tasks:
        tasks.start_soon(listener.serve, hold)
        try:
            yield receive, release
        finally:
            release.set()
            tasks.cancel_scope.cancel()
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not [worker for worker in threading.enumerate() if worker.name == "marabrain-cli"]


@pytest.mark.parametrize("spelling", ["LEGACY", "CANONICAL", "ALIAS"])
@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("create_memory", {"title": "one", "content": "body"}),
        ("read_memory", {"permalink": "one"}),
        ("edit_memory", {"permalink": "one", "operation": {"type": "append", "content": "body"}}),
        ("delete_memory", {"permalink": "one"}),
        ("search_memories", {"query": "body"}),
    ],
)
async def test_final_commands_share_resolved_domain_quota(cli: Path, spelling: str, tool: str, arguments: dict[str, object]) -> None:
    """Refuse all final command variants sharing one resolved identity without launching them."""
    (cli / "domains.json").write_text(json.dumps([{"name": "Legacy", "canonical_name": "canonical", "aliases": ["alias"]}]))
    await DomainRegistry.from_environment(5).synchronize()
    (cli / "calls.jsonl").unlink()
    server = FastMCP("quotas")
    register_memory_tools(server, domain_command_capacity=1)
    async with command_barrier(cli) as (started, release), Client(server) as client:

        async def occupy() -> None:
            """Hold one resolved exact-name permit through a real child."""
            assert (await client.call_tool("read_memory", {"domain": "Legacy", "permalink": "held"})).structured_content == RECEIPT

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(occupy)
                await started.receive()
                try:
                    supplied = {"domains": [spelling]} if tool == "search_memories" else {"domain": spelling}
                    with pytest.raises(ToolError, match="domain command capacity is exhausted"):
                        await client.call_tool(tool, {**arguments, **supplied})
                    assert len(calls(cli)) == 1
                finally:
                    release.set()
        assert (await client.call_tool("read_memory", {"domain": spelling, "permalink": "one"})).structured_content == RECEIPT


async def test_rejected_final_create_preserves_completed_registration(domain_cli: Path) -> None:
    """Register a missing domain outside its occupied final-command quota without undoing it on refusal."""
    server = FastMCP("registration")
    register_memory_tools(server, domain_command_capacity=1)
    async with command_barrier(domain_cli) as (started, release), Client(server) as client:

        async def occupy() -> None:
            """Retain a new-name command permit while creation registers the domain."""
            result = await client.call_tool("read_memory", {"domain": "new", "permalink": "held"})
            assert result.structured_content == {"domain": "new", "content": "body"}

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(occupy)
                await started.receive()
                try:
                    with pytest.raises(ToolError, match="domain command capacity is exhausted"):
                        await client.call_tool("create_memory", {"domain": "new", "title": "one", "content": "body"})
                    assert rows() == ["new", "notes"]
                    assert [arguments[1:3] for arguments in argv(domain_cli)] == [["read", "held"], ["domain", "list"], ["domain", "init"], ["domain", "add"]]
                    registry = DomainRegistry.from_environment(5)
                    (registry.storage.domain_path("new") / "held.md").write_text("body")
                finally:
                    release.set()
        assert (await client.call_tool("create_memory", {"domain": "new", "title": "one", "content": "body"})).structured_content == {
            "domain": "new",
            "permalink": "one",
        }
