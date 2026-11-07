import sys

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from pydantic import ValidationError
from starlette.testclient import TestClient

from marabrain.config import Settings, Transport
from marabrain.server import create_server


@pytest.mark.parametrize("transport", list(Transport))
def test_transport_environment_parses_enum(monkeypatch: pytest.MonkeyPatch, transport: Transport) -> None:
    monkeypatch.setenv("MARABRAIN_TRANSPORT", transport.value)
    monkeypatch.setenv("MARABRAIN_BEARER_SECRET", "s" * 32)
    assert Settings().transport is transport


@pytest.mark.parametrize(
    "name,value", [("MARABRAIN_TRANSPORT", "sse"), ("MARABRAIN_PORT", "0"), ("MARABRAIN_PORT", "65536"), ("MARABRAIN_LOG_LEVEL", "verbose")]
)
def test_invalid_environment_is_rejected(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_http_secret_is_required_and_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
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
async def test_mcp_inventory_and_health() -> None:
    async with Client(create_server(Settings())) as client:
        assert client.server_info is not None
        assert client.server_info.name == "marabrain-mcp"
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["health"]
        assert tools[0].annotations is not None
        assert tools[0].annotations.read_only_hint is True
        assert tools[0].annotations.idempotent_hint is True
        assert await client.list_resources() == []
        assert await client.list_prompts() == []
        result = await client.call_tool("health", {})
        assert result.structured_content == {"status": "ok", "version": "0.1.0"}


@pytest.mark.asyncio
async def test_module_stdio_entrypoint() -> None:
    transport = StdioTransport(command=sys.executable, args=["-m", "marabrain"])
    async with Client(transport, timeout=10) as client:
        result = await client.call_tool("health", {})
        assert result.structured_content == {"status": "ok", "version": "0.1.0"}


def test_http_auth_and_host_origin_protection(monkeypatch: pytest.MonkeyPatch) -> None:
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
