"""Behavior tests for server configuration and MCP transport boundaries."""

import os
import stat
import sys
from pathlib import Path

import anyio
import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from fastmcp.exceptions import ToolError
from memory_fake import RECEIPT, calls, domain_list
from pydantic import SecretStr, ValidationError
from starlette.testclient import TestClient
from starlette.applications import Starlette
from transport_process_policy import CAPTURE_BYTES, PRIVATE_OUTPUT, assert_transport_process_policy, process_probe

from marabrain.config import Settings, Transport
from marabrain.domain_registry import DomainRegistry
from marabrain.server import create_server

PROBE_NOISE = "probe-noise"
ENGINE_SCRIPT = f"""#!/bin/sh
printf '%s\\n' "$*" >> "${{0%/*}}/calls"
if [ "$*" = "--json domain list" ]; then
    echo '{{"domains":[]}}'
    exit 0
fi
read -r mode < "${{0%/*}}/mode"
case "$mode" in
ok) echo {PROBE_NOISE}; echo {PROBE_NOISE} >&2; exit 0 ;;
fail) echo {PROBE_NOISE}; echo {PROBE_NOISE} >&2; exit 3 ;;
delay) /bin/sleep 0.5; exit 0 ;;
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
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
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
        assert result.structured_content == {"status": "ok", "version": "0.4.2"}
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
    transport = StdioTransport(command=sys.executable, args=["-m", "marabrain"], env=dict(os.environ))
    async with Client(transport, timeout=10) as client:
        result = await client.call_tool("health", {})
        assert result.structured_content == {"status": "ok", "version": "0.4.2"}
    assert probe_calls(engine) == ["--json domain list", "ctl status"]


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
        assert response.json()["result"]["structuredContent"] == {"status": "ok", "version": "0.4.2"}


@pytest.mark.asyncio
async def test_health_deadlines_are_independent_server_snapshots(engine: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify concurrent servers retain their health budgets after the environment changes."""
    (engine / "mode").write_text("delay")
    short = create_server(Settings(health_timeout_seconds=0.2))
    long = create_server(Settings(health_timeout_seconds=2.0))
    monkeypatch.setenv("MARABRAIN_HEALTH_TIMEOUT_SECONDS", "0.001")
    async with Client(short) as short_client, Client(long) as long_client:

        async def times_out() -> None:
            """Verify the shorter captured budget rejects the delayed probe."""
            with pytest.raises(ToolError, match="Crystalline daemon is not responding"):
                await short_client.call_tool("health", {})

        async def succeeds() -> None:
            """Verify the longer captured budget allows the same delayed probe."""
            result = await long_client.call_tool("health", {})
            assert result.structured_content == {"status": "ok", "version": "0.4.2"}

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(times_out)
            tasks.start_soon(succeeds)
    assert probe_calls(engine) == ["ctl status", "ctl status"]


@pytest.mark.asyncio
async def test_domain_policies_are_independent_server_snapshots(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify servers retain exact domain admission after later environment changes."""
    domain_list(cli, ["Notes"])
    await DomainRegistry.from_environment(30).synchronize()
    (cli / "calls.jsonl").unlink()
    allowed = create_server(Settings(allowed_domains=("Notes",)))
    denied = create_server(Settings(allowed_domains=()))
    monkeypatch.setenv("MARABRAIN_ALLOWED_DOMAINS", '["other"]')
    async with Client(allowed) as allowed_client, Client(denied) as denied_client:
        result = await allowed_client.call_tool("read_memory", {"domain": "Notes", "permalink": "plan"})
        assert result.structured_content == RECEIPT
        for client, domain in ((allowed_client, "notes"), (denied_client, "Notes")):
            with pytest.raises(ToolError):
                await client.call_tool("read_memory", {"domain": domain, "permalink": "plan"})
    assert len(calls(cli)) == 1


def test_http_domain_policy_denies_before_command(cli: Path) -> None:
    """Verify authenticated HTTP enforces the captured domain policy before CLI execution."""
    settings = Settings(transport=Transport.HTTP, bearer_secret=SecretStr("s" * 32), allowed_domains=("notes",))
    app = create_server(settings).http_app(
        stateless_http=True, json_response=True, host_origin_protection=True, allowed_hosts=settings.allowed_hosts, allowed_origins=settings.allowed_origins
    )
    headers = {"host": "localhost", "accept": "application/json, text/event-stream", "authorization": "Bearer " + "s" * 32}
    arguments = {"domain": "other", "permalink": "plan"}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_memory", "arguments": arguments}}
    with TestClient(app) as client:
        denied = client.post("/mcp", headers=headers, json=payload)
        assert denied.status_code == 200 and denied.json()["result"]["isError"] is True
        assert calls(cli) == []
        arguments["domain"] = "notes"
        allowed = client.post("/mcp", headers=headers, json=payload)
        assert allowed.json()["result"]["structuredContent"] == RECEIPT
    assert len(calls(cli)) == 1


async def test_http_process_limits_are_application_owned_and_reusable(
    cli: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """Keep authenticated HTTP applications' limits independent after environment changes."""
    settings = Settings(
        transport=Transport.HTTP,
        bearer_secret=SecretStr("s" * 32),
        max_transient_processes=2,
        max_capture_bytes=CAPTURE_BYTES,
        command_timeout_seconds=60,
        health_timeout_seconds=60,
    )

    def transport(app: Starlette) -> StreamableHttpTransport:
        """Connect the MCP HTTP client through the application's authenticated ASGI stack."""

        def factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
            *,
            follow_redirects: bool = True,
        ) -> httpx2.AsyncClient:
            """Supply the four options required by the MCP HTTP transport factory contract."""
            return httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app),
                headers=headers,
                auth=auth,
                timeout=timeout or httpx2.Timeout(5),
                follow_redirects=follow_redirects,
            )

        return StreamableHttpTransport("http://localhost/mcp", auth="s" * 32, httpx_client_factory=factory)

    with anyio.fail_after(30):
        async with process_probe(cli) as probe:
            app = create_server(settings).http_app(stateless_http=True, json_response=True, host_origin_protection=True)
            other = create_server(settings.model_copy(update={"max_capture_bytes": CAPTURE_BYTES * 2})).http_app(
                stateless_http=True, json_response=True, host_origin_protection=True
            )
            monkeypatch.setenv("MARABRAIN_MAX_TRANSIENT_PROCESSES", "3")
            monkeypatch.setenv("MARABRAIN_MAX_CAPTURE_BYTES", "16")
            async with app.router.lifespan_context(app), other.router.lifespan_context(other):
                async with Client(transport(app)) as client, Client(transport(other)) as independent:
                    await assert_transport_process_policy(client, probe, independent)
    assert PRIVATE_OUTPUT not in "".join(capfd.readouterr()) + caplog.text
