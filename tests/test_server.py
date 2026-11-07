"""Behavior tests for server configuration and MCP transport boundaries."""

import os
import stat
import sys
from pathlib import Path

import anyio
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from pydantic import ValidationError
from starlette.testclient import TestClient

from marabrain.config import Settings, Transport
from marabrain.server import create_server

PROBE_NOISE = "probe-noise"
ENGINE_SCRIPT = f"""#!/bin/sh
printf '%s\\n' "$*" >> "${{0%/*}}/calls"
read -r mode < "${{0%/*}}/mode"
case "$mode" in
ok) echo {PROBE_NOISE}; echo {PROBE_NOISE} >&2; exit 0 ;;
fail) echo {PROBE_NOISE}; echo {PROBE_NOISE} >&2; exit 3 ;;
hang) exec /bin/sleep 30 ;;
esac
"""


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a fake crystalline executable first on PATH and return its directory, which records calls and takes its mode from a file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "crystalline"
    executable.write_text(ENGINE_SCRIPT)
    executable.chmod(0o755)
    (bin_dir / "mode").write_text("ok")
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
    return bin_dir


def probe_calls(engine: Path) -> list[str]:
    """Return the argument lines recorded by the fake crystalline executable."""
    calls = engine / "calls"
    return calls.read_text().splitlines() if calls.exists() else []


@pytest.mark.parametrize("transport", list(Transport))
def test_transport_environment_parses_enum(monkeypatch: pytest.MonkeyPatch, transport: Transport) -> None:
    """Verify that each supported transport parses from the environment."""
    monkeypatch.setenv("MARABRAIN_TRANSPORT", transport.value)
    monkeypatch.setenv("MARABRAIN_BEARER_SECRET", "s" * 32)
    assert Settings().transport is transport


@pytest.mark.parametrize(
    "name,value", [("MARABRAIN_TRANSPORT", "sse"), ("MARABRAIN_PORT", "0"), ("MARABRAIN_PORT", "65536"), ("MARABRAIN_LOG_LEVEL", "verbose")]
)
def test_invalid_environment_is_rejected(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    """Verify that invalid environment settings raise validation errors."""
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_http_secret_is_required_and_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that HTTP requires a secret and redacts it from errors and representations."""
    monkeypatch.setenv("MARABRAIN_TRANSPORT", "http")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("MARABRAIN_BEARER_SECRET", "short-secret")
    with pytest.raises(ValidationError) as error:
        Settings()
    assert "short-secret" not in str(error.value)
    monkeypatch.setenv("MARABRAIN_BEARER_SECRET", "s" * 32)
    assert "s" * 32 not in repr(Settings())


@pytest.mark.asyncio
async def test_mcp_inventory_and_health(engine: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """Verify the MCP server identity, six-tool interface inventory, and health response backed by one status probe."""
    async with Client(create_server(Settings())) as client:
        assert client.server_info is not None
        assert client.server_info.name == "marabrain-mcp"
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["health", "create_memory", "read_memory", "edit_memory", "delete_memory", "search_memories"]
        assert tools[0].annotations is not None
        assert tools[0].annotations.read_only_hint is True
        assert tools[0].annotations.idempotent_hint is True
        assert await client.list_resources() == []
        assert await client.list_prompts() == []
        result = await client.call_tool("health", {})
        assert result.structured_content == {"status": "ok", "version": "0.1.0"}
    assert probe_calls(engine) == ["ctl status"]
    assert PROBE_NOISE not in "".join(capfd.readouterr())


@pytest.mark.parametrize("mode", ["missing", "unexecutable", "fail", "hang"])
@pytest.mark.asyncio
async def test_health_fails_without_a_responsive_engine(engine: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], mode: str) -> None:
    """Verify that a missing, unexecutable, failing, or hung engine yields a bounded generic tool error."""
    if mode == "missing":
        (engine / "crystalline").unlink()
        monkeypatch.setenv("PATH", str(engine))
    elif mode == "unexecutable":
        (engine / "crystalline").chmod(stat.S_IRUSR | stat.S_IWUSR)
    else:
        (engine / "mode").write_text(mode)
    async with Client(create_server(Settings())) as client:
        with anyio.fail_after(10), pytest.raises(ToolError) as error:
            await client.call_tool("health", {})
    assert PROBE_NOISE not in str(error.value) + "".join(capfd.readouterr())
    assert probe_calls(engine) == (["ctl status"] if mode in {"fail", "hang"} else [])


@pytest.mark.asyncio
async def test_module_stdio_entrypoint(engine: Path) -> None:
    """Verify that the module entry point serves health requests over stdio."""
    transport = StdioTransport(command=sys.executable, args=["-m", "marabrain"])
    async with Client(transport, timeout=10) as client:
        result = await client.call_tool("health", {})
        assert result.structured_content == {"status": "ok", "version": "0.1.0"}
    assert probe_calls(engine) == ["ctl status"]


def test_http_auth_and_host_origin_protection(engine: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify HTTP authentication, host and origin restrictions, and authorized health requests."""
    monkeypatch.setenv("MARABRAIN_TRANSPORT", "http")
    monkeypatch.setenv("MARABRAIN_BEARER_SECRET", "s" * 32)
    settings = Settings()
    server = create_server(settings)
    app = server.http_app(
        stateless_http=True, json_response=True, host_origin_protection=True, allowed_hosts=settings.allowed_hosts, allowed_origins=settings.allowed_origins
    )
    headers = {"host": "localhost", "accept": "application/json, text/event-stream", "content-type": "application/json"}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "health", "arguments": dict[str, str]()}}
    with TestClient(app) as client:
        assert client.post("/mcp", headers=headers, json=payload).status_code == 401
        assert client.post("/mcp", headers={**headers, "authorization": "Bearer wrong"}, json=payload).status_code == 401
        auth = {**headers, "authorization": "Bearer " + "s" * 32}
        assert client.post("/mcp", headers={**auth, "host": "evil.example"}, json=payload).status_code == 421
        assert client.post("/mcp", headers={**auth, "origin": "https://evil.example"}, json=payload).status_code == 403
        response = client.post("/mcp", headers=auth, json=payload)
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"] == {"status": "ok", "version": "0.1.0"}
