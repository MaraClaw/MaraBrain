"""Exercise bound engine environments through independent real MCP servers."""

import json
import os
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from marabrain.config import Settings
from marabrain.server import create_server

CONTEXT_CLI = '''
"""Resolve the engine environment and return receipts from the selected store."""
import json
import os
import sys
from pathlib import Path

home = Path(os.environ["HOME"])
state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state")
config = os.environ.get("CRYSTALLINE_CONFIG") or str(Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "crystalline/config.yaml")
state.mkdir(parents=True, exist_ok=True)
with (state / "engine-calls").open("a") as stream:
    stream.write(json.dumps({"args": sys.argv[1:], "config": config}) + "\\n")
if sys.argv[1:] == ["ctl", "status"]:
    sys.exit(0)
if sys.argv[1:] == ["--json", "domain", "list"]:
    print('{"domains":[{"name":"notes"}]}')
else:
    sys.stdin.read()
    print(json.dumps({"store": str(state), "config": config}))
'''


@pytest.mark.asyncio
async def test_interleaved_health_and_all_memory_tools_keep_their_engine_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain HOME fallbacks and explicit roots across ambient environment changes."""
    executable = tmp_path / "crystalline"
    executable.write_text(f"#!{sys.executable}\n" + CONTEXT_CLI)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "CRYSTALLINE_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    first_home = tmp_path / "first"
    monkeypatch.setenv("HOME", str(first_home))
    first = create_server(Settings())
    second_state = tmp_path / "second-state"
    selected = str(tmp_path / "second-config.yaml")
    monkeypatch.setenv("HOME", str(tmp_path / "second-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(second_state))
    monkeypatch.setenv("CRYSTALLINE_CONFIG", selected)
    second = create_server(Settings())
    monkeypatch.setenv("HOME", str(tmp_path / "ambient"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ambient-state"))
    monkeypatch.setenv("CRYSTALLINE_CONFIG", str(tmp_path / "ambient-config"))
    monkeypatch.setenv("PATH", str(tmp_path / "absent"))
    ambient = dict(os.environ)
    first_state = first_home / ".local/state"
    expected = ((first_state, str(first_home / ".config/crystalline/config.yaml")), (second_state, selected))
    async with Client(first) as first_client, Client(second) as second_client:
        for tool, arguments in (
            ("health", {}),
            ("create_memory", {"domain": "notes", "title": "Title", "content": "Body"}),
            ("read_memory", {"domain": "notes", "permalink": "plan"}),
            ("edit_memory", {"domain": "notes", "permalink": "plan", "operation": {"type": "append", "content": "More"}}),
            ("delete_memory", {"domain": "notes", "permalink": "plan"}),
            ("search_memories", {"query": "plan", "domains": ["notes"]}),
        ):
            for client, (state, config) in zip((first_client, second_client), expected, strict=True):
                result = await client.call_tool(tool, arguments)
                assert result.structured_content == ({"status": "ok", "version": "0.3.0"} if tool == "health" else {"store": str(state), "config": config})
    for state, config in expected:
        commands = [json.loads(line) for line in (state / "engine-calls").read_text().splitlines()]
        assert len(commands) == 7
        assert all(command["config"] == config for command in commands)
        assert (state / "marabrain/domains.sqlite3").is_file()
    assert dict(os.environ) == ambient
    assert not (tmp_path / "ambient-state").exists()
