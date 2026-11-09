"""Wire-level tests for LLM configuration and compression providers."""

import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import httpx2
import pytest
from anyio.abc import TaskStatus
from fastmcp.exceptions import ToolError
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter, ValidationError

from marabrain.crystalline import JsonObject
from marabrain.compression import compress_memory
from marabrain.llm_config import LLMProvider, LLMSettings


def completion(message: JsonObject | None = None, finish: str = "stop", count: int = 1) -> bytes:
    """Encode a complete OpenAI chat completion envelope."""
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "model",
            "choices": [
                {"index": i, "finish_reason": finish, "message": {"role": "assistant", **(message or {"content": '{"context":"result"}'})}}
                for i in range(count)
            ],
        }
    ).encode()


@dataclass(slots=True)
class Provider:
    """Mutable loopback provider state for recording requests and choosing responses."""

    url: str = ""
    status: int = 200
    delay_seconds: float = 0
    body: bytes = field(default_factory=completion)
    requests: list[tuple[str, dict[str, str], JsonObject]] = field(default_factory=list[tuple[str, dict[str, str], JsonObject]])


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[Provider]:
    """Serve configurable provider responses over a real local HTTP connection."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    state = Provider()

    class Handler(BaseHTTPRequestHandler):
        """Record requests and return the selected response."""

        def do_POST(self) -> None:
            """Capture request data and send the selected reply."""
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append((self.path, dict(self.headers), payload))
            time.sleep(state.delay_seconds)
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            try:
                self.wfile.write(state.body)
            except BrokenPipeError, ConnectionResetError:
                return

        def log_message(self, format: str, *args: str) -> None:
            """Suppress local provider logging."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1/"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("key", ["", "secret"])
async def test_openai_wire_contract(provider: Provider, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    """Send separated system and JSON user data with supported OpenAI options."""
    for name in ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"):
        monkeypatch.setenv(name, "ambient-secret")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer ambient-secret")
    settings = LLMSettings(model="model", provider_url=AnyHttpUrl(provider.url), api_key=SecretStr(key), reasoning_effort="low", temperature=0.5)
    assert await compress_memory(settings, 'query "data"', "memory\ncontent") == "result"
    [(path, headers, payload)] = provider.requests
    assert path == "/v1/chat/completions"
    assert {name.lower(): value for name, value in headers.items()}.get("authorization") == (f"Bearer {key}" if key else None)
    assert "ambient-secret" not in str(headers)
    assert payload["model"] == "model" and payload["reasoning_effort"] == "low" and payload["temperature"] == 0.5
    assert payload["max_completion_tokens"] == 32768 and "tools" not in payload
    assert "max_input_tokens" not in payload and "max_context_tokens" not in payload
    response_format = TypeAdapter(JsonObject).validate_python(payload["response_format"])
    assert response_format["type"] == "json_schema"
    schema = TypeAdapter(JsonObject).validate_python(response_format["json_schema"])
    assert schema["strict"] is True
    messages = TypeAdapter(list[JsonObject]).validate_python(payload["messages"])
    assert [message["role"] for message in messages] == ["system", "user"]
    content = messages[1]["content"]
    assert isinstance(content, str)
    assert json.loads(content) == {"query": 'query "data"'}
    system = messages[0]["content"]
    assert isinstance(system, str) and system.endswith(json.dumps("memory\ncontent"))
    assert await compress_memory(settings, "another query", "memory\ncontent") == "result"
    subsequent = TypeAdapter(list[JsonObject]).validate_python(provider.requests[-1][2]["messages"])
    assert subsequent[0] == messages[0] and subsequent[1] != messages[1]


@pytest.mark.parametrize(
    "body",
    [
        b"secret-response",
        b'{"choices":[]}',
        completion(finish="length"),
        completion(finish="content_filter"),
        completion(count=2),
        completion({"content": "text", "refusal": "no"}),
        completion({"content": "plain text"}),
        *[completion({"content": json.dumps(value)}) for value in ({}, {"context": ""}, {"context": "  "}, {"context": 2}, {"context": None})],
    ],
)
async def test_invalid_openai_response_is_fixed_error(provider: Provider, body: bytes) -> None:
    """Reject malformed, incomplete, refused, empty, or multiple-choice responses."""
    provider.body = body
    settings = LLMSettings(model="model", provider_url=AnyHttpUrl(provider.url), api_key=SecretStr("secret"))
    with pytest.raises(ToolError, match="^LLM compression failed\\.$"):
        await compress_memory(settings, "query", "content")
    assert len(provider.requests) == 1


@pytest.mark.parametrize("status", [302, 401, 500])
async def test_http_errors_do_not_leak_or_retry(provider: Provider, status: int) -> None:
    """Return a fixed error for HTTP failures and do not retry or follow redirects."""
    provider.status, provider.body = status, b"secret-response"
    settings = LLMSettings(model="model", provider_url=AnyHttpUrl(provider.url), api_key=SecretStr("secret"))
    with pytest.raises(ToolError, match="^LLM compression failed\\.$"):
        await compress_memory(settings, "query", "content")
    assert len(provider.requests) == 1


def test_environment_defaults_and_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load empty and configured environment values without exposing secrets."""
    for name in ("MODEL", "PROVIDER_URL", "API_KEY", "PROVIDER", "REASONING_EFFORT", "TEMPERATURE", "MAX_TOKENS", "MAX_INPUT_TOKENS", "TIMEOUT_SECONDS"):
        monkeypatch.setenv(f"LLM_{name}", "")
    settings = LLMSettings()
    assert settings.model is None and settings.provider_url is None and settings.timeout_seconds == 60
    assert settings.max_input_tokens == 256000 and settings.max_tokens == 32768
    assert settings.provider is LLMProvider.OPENAI
    with pytest.raises(ToolError):
        settings.require_configured()
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_PROVIDER_URL", "https://example.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("LLM_MAX_INPUT_TOKENS", "128000")
    monkeypatch.setenv("LLM_PROVIDER", LLMProvider.ANTHROPIC.value)
    assert LLMSettings().max_input_tokens == 128000
    assert LLMSettings().provider is LLMProvider.ANTHROPIC
    assert "secret" not in repr(LLMSettings())


@pytest.mark.parametrize(
    "values",
    [
        {"model": "m"},
        {"provider_url": "https://example.com/v1"},
        {"model": " "},
        {"provider_url": "http://user:secret@example.com"},
        {"provider_url": "http://example.com?secret=x"},
        {"provider_url": "http://example.com#secret"},
        {"temperature": "nan"},
        {"temperature": "2.1"},
        {"max_tokens": "0"},
        {"max_input_tokens": "0"},
        {"provider": "unknown"},
        {"timeout_seconds": "inf"},
        {"provider": "anthropic", "temperature": "1.1"},
        {"provider": "anthropic", "temperature": "0"},
        {"provider": "anthropic", "temperature": "0.5"},
        {"provider": "anthropic", "temperature": "0.5", "reasoning_effort": "low"},
    ],
)
def test_invalid_configuration_is_rejected_without_input_values(values: dict[str, str]) -> None:
    """Reject invalid provider configuration without echoing supplied values."""
    with pytest.raises(ValidationError) as error:
        LLMSettings.model_validate(values)
    assert "secret" not in str(error.value)


async def test_anthropic_requires_key_before_request(provider: Provider) -> None:
    """Reject an Anthropic call without its mandatory API key."""
    settings = LLMSettings(provider=LLMProvider.ANTHROPIC, model="model", provider_url=AnyHttpUrl(provider.url))
    with pytest.raises(ToolError, match="API key"):
        await compress_memory(settings, "q", "c")
    assert provider.requests == []


async def test_unreachable_provider_returns_fixed_error() -> None:
    """Return a fixed error when the provider refuses a connection."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        url = AnyHttpUrl(f"http://127.0.0.1:{probe.getsockname()[1]}/v1")
        settings = LLMSettings(model="m", provider_url=url)
        with pytest.raises(ToolError, match="^LLM compression failed\\.$"):
            await compress_memory(settings, "q", "c")


async def test_default_options_are_omitted(provider: Provider) -> None:
    """Omit optional generation parameters when they are not configured."""
    settings = LLMSettings(model="m", provider_url=AnyHttpUrl(provider.url))
    assert await compress_memory(settings, "q", "c") == "result"
    payload = provider.requests[0][2]
    assert "reasoning_effort" not in payload and "temperature" not in payload


async def test_openai_cancellation_drains_local_client_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Complete awaited local HTTP closure before a cancelled provider request returns."""
    requested, closing, release, closed, stopped = (anyio.Event() for _ in range(5))

    async def handle(request: httpx2.Request) -> httpx2.Response:
        """Hold a real SDK request at the HTTP transport boundary until cancellation."""
        requested.set()
        await anyio.sleep_forever()
        raise AssertionError("Provider request unexpectedly resumed")

    class Client(httpx2.AsyncClient):
        """Expose a cleanup barrier while retaining the real SDK HTTP client contract."""

        def __init__(self, *, follow_redirects: bool, timeout: float) -> None:
            """Install the controlled transport for this provider client."""
            super().__init__(transport=httpx2.MockTransport(handle), follow_redirects=follow_redirects, timeout=timeout)

        async def aclose(self) -> None:
            """Require an awaited checkpoint before the HTTP resource can close."""
            closing.set()
            await release.wait()
            await super().aclose()
            closed.set()

    async def read(*, task_status: TaskStatus[anyio.CancelScope]) -> None:
        """Expose cancellation without bypassing provider cleanup."""
        with anyio.CancelScope() as scope:
            task_status.started(scope)
            await compress_memory(LLMSettings(model="m", provider_url=AnyHttpUrl("http://localhost/v1")), "q", "c")
        stopped.set()

    monkeypatch.setattr("marabrain.compression.httpx2.AsyncClient", Client)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks:
            try:
                scope = await tasks.start(read)
                await requested.wait()
                scope.cancel()
                await closing.wait()
                await anyio.wait_all_tasks_blocked()
                assert not stopped.is_set() and not closed.is_set()
            finally:
                release.set()
    assert stopped.is_set() and closed.is_set()
