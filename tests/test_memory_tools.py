"""Behavior tests for the Crystalline-backed memory tools at the subprocess and MCP boundaries."""

import json
import os
import stat
from pathlib import Path

import anyio
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memory_fake import NOISE, RECEIPT, WORKSPACE_ACTIVATION, WORKSPACE_ID, call, calls, respond
from starlette.testclient import TestClient

from marabrain import crystalline
from marabrain.config import Settings
from marabrain.server import create_server


def edit(operation: dict[str, object], **extra: object) -> dict[str, object]:
    """Build edit_memory arguments for a fixed memory and the given operation."""
    return {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "team/plan", "operation": operation, **extra}


NOTE: dict[str, object] = {"workspace_id": WORKSPACE_ID, "domain": "notes", "title": "A title", "content": "Body\n- [x] ü"}
TARGET: dict[str, object] = {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "team/plan"}
OPERATIONS = [
    pytest.param("create_memory", NOTE, ["write", "notes", "A title"], "Body\n- [x] ü", id="create"),
    pytest.param(
        "create_memory",
        {**NOTE, "tags": ["a b", "c"], "folder": "team/docs"},
        ["write", "notes", "A title", "--tags=a b,c", "--folder=team/docs"],
        "Body\n- [x] ü",
        id="create-options",
    ),
    pytest.param("create_memory", {**NOTE, "content": ""}, ["write", "notes", "A title"], "", id="create-empty-body"),
    pytest.param("read_memory", TARGET, ["read", "team/plan", "--domain=notes"], "", id="read"),
    pytest.param("delete_memory", TARGET, ["delete", "team/plan", "notes", "--force"], "", id="delete"),
    pytest.param("edit_memory", edit({"type": "append", "content": "more"}), ["edit", "team/plan", "notes", "append"], "more", id="append"),
    pytest.param(
        "edit_memory",
        edit({"type": "prepend", "content": "first"}, expected_checksum="abc123"),
        ["edit", "team/plan", "notes", "prepend", "--expected-checksum=abc123"],
        "first",
        id="prepend-checksum",
    ),
    pytest.param(
        "edit_memory",
        edit({"type": "find_replace", "content": "new", "find_text": "-old"}),
        ["edit", "team/plan", "notes", "find_replace", "--find-text=-old"],
        "new",
        id="find-replace",
    ),
    pytest.param(
        "edit_memory",
        edit({"type": "find_replace", "content": "", "find_text": "old", "expected_replacements": 2}),
        ["edit", "team/plan", "notes", "find_replace", "--find-text=old", "--expected-replacements=2"],
        "",
        id="find-replace-count",
    ),
    pytest.param(
        "edit_memory",
        edit({"type": "find_replace", "content": "", "find_text": "old", "expected_replacements": 0}),
        ["edit", "team/plan", "notes", "find_replace", "--find-text=old", "--expected-replacements=0"],
        "",
        id="find-replace-zero-count",
    ),
    pytest.param(
        "edit_memory",
        edit({"type": "replace_section", "content": "body", "section": "Plan"}),
        ["edit", "team/plan", "notes", "replace_section", "--section=Plan"],
        "body",
        id="replace-section",
    ),
    pytest.param(
        "edit_memory",
        edit({"type": "replace_section", "content": "body", "section": "Plan", "include_subsections": True}),
        ["edit", "team/plan", "notes", "replace_section", "--section=Plan", "--include-subsections"],
        "body",
        id="replace-section-subsections",
    ),
    pytest.param(
        "edit_memory",
        edit({"type": "insert_before_section", "content": "x", "section": "Plan"}),
        ["edit", "team/plan", "notes", "insert_before_section", "--section=Plan"],
        "x",
        id="insert-before",
    ),
    pytest.param(
        "edit_memory",
        edit({"type": "insert_after_section", "content": "x", "section": "Plan"}),
        ["edit", "team/plan", "notes", "insert_after_section", "--section=Plan"],
        "x",
        id="insert-after",
    ),
]


@pytest.mark.parametrize("tool,arguments,argv,stdin", OPERATIONS)
async def test_tool_runs_one_cli_command_and_returns_native_json(cli: Path, tool: str, arguments: dict[str, object], argv: list[str], stdin: str) -> None:
    """Verify first activation precedes one exact final command and the unchanged native response."""
    assert await call(tool, arguments) == RECEIPT
    assert [(row["argv"], row["stdin"]) for row in calls(cli)] == [*((command, "") for command in WORKSPACE_ACTIVATION), (["--json", *argv], stdin)]


INVALID = [
    pytest.param("create_memory", {**NOTE, "domain": "-x"}, id="domain-flag"),
    pytest.param("create_memory", {**NOTE, "title": "--force"}, id="title-flag"),
    pytest.param("create_memory", {**NOTE, "title": "a\x00b"}, id="title-nul"),
    pytest.param("create_memory", {**NOTE, "title": "a\nb"}, id="title-newline"),
    pytest.param("create_memory", {**NOTE, "content": "a\x00b"}, id="content-nul"),
    pytest.param("create_memory", {**NOTE, "tags": ["a,b"]}, id="tag-comma"),
    pytest.param("create_memory", {**NOTE, "tags": [""]}, id="tag-empty"),
    pytest.param("create_memory", {**NOTE, "tags": [" a"]}, id="tag-padded"),
    pytest.param("create_memory", {**NOTE, "folder": "../x"}, id="folder-traversal"),
    pytest.param("create_memory", {**NOTE, "folder": "/abs"}, id="folder-absolute"),
    pytest.param("create_memory", {**NOTE, "folder": "a//b"}, id="folder-empty-segment"),
    pytest.param("create_memory", {**NOTE, "folder": "a/./b"}, id="folder-dot-segment"),
    pytest.param("create_memory", {**NOTE, "folder": "-x"}, id="folder-flag"),
    pytest.param("read_memory", {**TARGET, "permalink": "../etc/passwd"}, id="permalink-traversal"),
    pytest.param("read_memory", {**TARGET, "permalink": "crystalline://notes/plan"}, id="permalink-url"),
    pytest.param("read_memory", {**TARGET, "permalink": "a\\b"}, id="permalink-backslash"),
    pytest.param("read_memory", {**TARGET, "permalink": "-r"}, id="permalink-flag"),
    pytest.param("read_memory", {**TARGET, "domain": "a\x00"}, id="domain-nul"),
    pytest.param("edit_memory", edit({"type": "overwrite", "content": "x"}), id="edit-unknown-type"),
    pytest.param("edit_memory", edit({"content": "x"}), id="edit-missing-type"),
    pytest.param("edit_memory", edit({"type": "find_replace", "content": "x"}), id="find-replace-without-text"),
    pytest.param("edit_memory", edit({"type": "find_replace", "content": "x", "find_text": "y", "expected_replacements": -1}), id="replacements-negative"),
    pytest.param("edit_memory", edit({"type": "replace_section", "content": "x"}), id="replace-section-without-section"),
    pytest.param("edit_memory", edit({"type": "insert_after_section", "content": "x"}), id="insert-after-without-section"),
    pytest.param("edit_memory", edit({"type": "append", "content": "x", "section": "Plan"}), id="append-extra-field"),
    pytest.param("edit_memory", edit({"type": "append", "content": "a\x00b"}), id="edit-content-nul"),
    pytest.param("edit_memory", edit({"type": "append", "content": "x"}, expected_checksum="bad checksum"), id="checksum-space"),
]


@pytest.mark.parametrize("tool,arguments", INVALID)
async def test_invalid_arguments_fail_before_execution(cli: Path, tool: str, arguments: dict[str, object]) -> None:
    """Verify that unsafe or malformed arguments are rejected without starting crystalline."""
    with pytest.raises(ToolError):
        await call(tool, arguments)
    assert calls(cli) == []


@pytest.mark.parametrize(
    "stdout,exit_code,message",
    [
        pytest.param(f'{{"leak": "{NOISE}"}}', 3, "exit code 3", id="nonzero-exit"),
        pytest.param("not json", 0, "malformed", id="not-json"),
        pytest.param("", 0, "malformed", id="empty-output"),
        pytest.param("[1, 2]", 0, "malformed", id="json-array"),
        pytest.param('{"a": 1}{"b": 2}', 0, "malformed", id="trailing-document"),
    ],
)
async def test_cli_failures_yield_fixed_errors_and_no_retry(cli: Path, capfd: pytest.CaptureFixture[str], stdout: str, exit_code: int, message: str) -> None:
    """Verify nonzero exits and malformed output give one bounded error that leaks neither stdout nor stderr and is not retried."""
    respond(cli, stdout=stdout, exit_code=exit_code)
    with pytest.raises(ToolError) as error:
        await call("delete_memory", TARGET)
    assert message in str(error.value)
    assert NOISE not in str(error.value) + "".join(capfd.readouterr())
    assert [record["argv"] for record in calls(cli)] == [*WORKSPACE_ACTIVATION, ["--json", "delete", "team/plan", "notes", "--force"]]


async def test_missing_executable_yields_unavailable_error(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that an absent or unexecutable crystalline yields a fixed error."""
    server = create_server(Settings())
    async with Client(server) as client:
        await client.call_tool("read_memory", TARGET)
        monkeypatch.setenv("PATH", str(cli))
        (cli / "crystalline").chmod(stat.S_IRUSR | stat.S_IWUSR)
        with pytest.raises(ToolError, match="unavailable"):
            await client.call_tool("read_memory", TARGET)
        (cli / "crystalline").unlink()
        monkeypatch.setenv("PATH", str(cli))
        with pytest.raises(ToolError, match="unavailable"):
            await client.call_tool("read_memory", TARGET)


async def test_hanging_cli_is_killed_at_the_deadline(cli: Path) -> None:
    """Verify that a hung command is terminated at the deadline with an error that does not promise rollback."""
    respond(cli, hang=True)
    with anyio.fail_after(15), pytest.raises(ToolError, match="may or may not have been applied"):
        await crystalline.run_json(["delete", "team/plan", "notes", "--force"], timeout_seconds=1.0)
    (record,) = calls(cli)
    pid = record["pid"]
    assert isinstance(pid, int)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_programmatic_server_capture_limits_are_independent_and_health_is_masked(
    cli: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Keep standalone servers on their programmatic limits despite later environment changes."""
    small = create_server(Settings(max_transient_processes=2, max_capture_bytes=128))
    large = create_server(Settings(max_transient_processes=3, max_capture_bytes=4096))
    monkeypatch.setenv("MARABRAIN_MAX_CAPTURE_BYTES", "1")
    monkeypatch.setenv("MARABRAIN_MAX_TRANSIENT_PROCESSES", "2")
    monkeypatch.setenv("PATH", str(cli / "absent"))
    payload = {"private": "secret-payload" * 20}
    respond(cli, stdout=json.dumps(payload))
    async with Client(small) as first, Client(large) as second:
        with pytest.raises(ToolError, match="capture limit"):
            await first.call_tool("delete_memory", TARGET)
        assert (await second.call_tool("read_memory", TARGET)).structured_content == payload
        script = (cli / "crystalline").read_text()
        probe = "private-health-probe"
        (cli / "crystalline").write_text(script.replace('if args == ["ctl", "status"]:', f'if args == ["ctl", "status"]:\n    print({probe!r} * 8)'))
        with pytest.raises(ToolError, match="daemon is not responding") as error:
            await first.call_tool("health", {})
        assert "capture limit" not in str(error.value) and "secret-payload" not in str(error.value)
        assert probe not in str(error.value) + "".join(capfd.readouterr())
        (cli / "crystalline").write_text(script)
        respond(cli, stdout="{}")
        assert (await first.call_tool("read_memory", TARGET)).structured_content == {}


@pytest.mark.parametrize(
    "tool,arguments",
    [("create_memory", NOTE), ("read_memory", TARGET), ("edit_memory", edit({"type": "append", "content": "x"})), ("delete_memory", TARGET)],
)
@pytest.mark.parametrize("policy", [frozenset[str](), frozenset({"Notes"})])
async def test_denied_domains_never_execute(cli: Path, tool: str, arguments: dict[str, object], policy: frozenset[str]) -> None:
    """Reject every single-domain operation before execution under an exact allowlist."""
    server = create_server(Settings(allowed_domains=tuple(policy)))
    async with Client(server) as client:
        with pytest.raises(ToolError, match="not permitted"):
            await client.call_tool(tool, arguments)
    assert calls(cli) == []


@pytest.mark.parametrize(
    "tool,arguments", [pytest.param("read_memory", TARGET, id="read"), pytest.param("create_memory", {**NOTE, "content": ""}, id="empty-body")]
)
async def test_commands_without_content_do_not_inherit_server_stdin(cli: Path, tool: str, arguments: dict[str, object]) -> None:
    """Verify that crystalline gets an empty stdin, not the server's own, when a command carries no content."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"protocol bytes")
    os.close(write_fd)
    saved_fd = os.dup(0)
    os.dup2(read_fd, 0)
    os.close(read_fd)
    try:
        await call(tool, arguments)
    finally:
        os.dup2(saved_fd, 0)
        os.close(saved_fd)
    assert [record["argv"] for record in calls(cli)[:2]] == WORKSPACE_ACTIVATION
    assert [record["stdin"] for record in calls(cli)] == ["", "", ""]


async def test_tool_annotations_separate_reads_from_mutations(cli: Path) -> None:
    """Verify read-only, destructive, and closed-world hints on the memory tools."""
    async with Client(create_server(Settings())) as client:
        hints = {tool.name: tool.annotations for tool in await client.list_tools()}
    assert {name for name, hint in hints.items() if hint and hint.read_only_hint} == {"health", "read_memory", "search_memories"}
    assert {name for name, hint in hints.items() if hint and hint.destructive_hint} == {"edit_memory", "delete_memory"}
    assert all(hint and hint.open_world_hint is False for name, hint in hints.items() if name not in {"health", "read_memory"})
    assert hints["read_memory"] and hints["read_memory"].open_world_hint is True


def test_http_requires_authentication_before_any_memory_command(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that unauthenticated or wrong-host HTTP tool calls never reach crystalline and authorized ones do."""
    monkeypatch.setenv("MARABRAIN_TRANSPORT", "http")
    monkeypatch.setenv("MARABRAIN_BEARER_SECRET", "s" * 32)
    settings = Settings()
    app = create_server(settings).http_app(
        stateless_http=True, json_response=True, host_origin_protection=True, allowed_hosts=settings.allowed_hosts, allowed_origins=settings.allowed_origins
    )
    headers = {"host": "localhost", "accept": "application/json, text/event-stream", "content-type": "application/json"}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "delete_memory", "arguments": TARGET}}
    with TestClient(app) as client:
        assert client.post("/mcp", headers=headers, json=payload).status_code == 401
        auth = {**headers, "authorization": "Bearer " + "s" * 32}
        assert client.post("/mcp", headers={**auth, "host": "evil.example"}, json=payload).status_code == 421
        assert calls(cli) == []
        response = client.post("/mcp", headers=auth, json=payload)
    assert response.json()["result"]["structuredContent"] == RECEIPT
    expected = [*WORKSPACE_ACTIVATION, ["--json", "delete", "team/plan", "notes", "--force"]]
    assert [(record["argv"], record["stdin"]) for record in calls(cli)] == [(command, "") for command in expected]
