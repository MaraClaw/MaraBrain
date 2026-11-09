"""Exercise compression progress and its lifetime through MCP requests."""

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anyio
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memory_fake import respond
from test_compression import Provider, completion, provider as provider
from test_read_compression import MEMORY, TARGET, clean_llm_environment as clean_llm_environment, configure
from pydantic import SecretStr
from starlette.testclient import TestClient

from marabrain.config import Settings, Transport
from marabrain.llm_config import LLMSettings
from marabrain.server import create_server


@pytest.mark.parametrize("outcome", ["success", "provider_timeout", "cancelled"])
async def test_compression_reports_progress_until_request_ends(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    """Keep long calls active while preserving provider deadlines and caller cancellation."""
    configure(monkeypatch, provider)
    monkeypatch.setattr("marabrain.memory_tools._COMPRESSION_PROGRESS_INTERVAL", 0.02)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0.05" if outcome == "provider_timeout" else "2")
    provider.delay_seconds = 0.2
    respond(cli, json.dumps(MEMORY))
    updates: list[tuple[float, float | None, str | None]] = []

    async def progress(value: float, total: float | None, message: str | None) -> None:
        """Capture protocol notifications without depending on their wording."""
        updates.append((value, total, message))
        if outcome == "cancelled":
            cancellation.cancel()

    async with Client(create_server(Settings()), progress_handler=progress, timeout=2) as client:
        arguments = {**TARGET, "query": "retry", "compress": True}
        if outcome == "cancelled":
            with anyio.fail_after(2), anyio.CancelScope() as cancellation:
                await client.call_tool("read_memory", arguments)
            assert cancellation.cancel_called
        elif outcome == "provider_timeout":
            with pytest.raises(ToolError, match="LLM compression failed"):
                await client.call_tool("read_memory", arguments)
        else:
            result = await client.call_tool("read_memory", arguments)
            assert result.structured_content == {**MEMORY, "content": "result", "compressed": True}
        assert updates
        if outcome == "success":
            assert len(updates) >= 2
        count = len(updates)
        await anyio.sleep(0.05)
        assert len(updates) == count
        assert all(total is None for _, total, _ in updates)
        assert all(right[0] > left[0] for left, right in zip(updates, updates[1:]))
        assert (await client.call_tool("read_memory", TARGET)).structured_content == MEMORY
        assert len(updates) == count
    assert len(provider.requests) <= 1 if outcome == "cancelled" else len(provider.requests) == 1


def test_http_compression_emits_progress_on_request_stream(cli: Path, provider: Provider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliver progress and the final result on stateless HTTP's authenticated SSE response."""
    configure(monkeypatch, provider)
    monkeypatch.setattr("marabrain.memory_tools._COMPRESSION_PROGRESS_INTERVAL", 0.02)
    provider.delay_seconds = 0.15
    respond(cli, json.dumps(MEMORY))
    server = create_server(Settings(transport=Transport.HTTP, bearer_secret=SecretStr("s" * 32)))
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "read_memory", "arguments": {**TARGET, "query": "retry", "compress": True}, "_meta": {"progressToken": "compression"}},
    }
    with TestClient(server.http_app(stateless_http=True)) as client:
        reply = client.post("/mcp", json=payload, headers={"authorization": "Bearer " + "s" * 32, "accept": "application/json, text/event-stream"})
    assert reply.status_code == 200
    frames = [json.loads(line[6:]) for line in reply.text.splitlines() if line.startswith("data: ")]
    notifications = [frame for frame in frames if frame.get("method") == "notifications/progress"]
    assert len(notifications) >= 2
    assert all(frame["params"]["progressToken"] == "compression" for frame in notifications)
    [result] = [frame["result"] for frame in frames if "result" in frame]
    assert result["structuredContent"] == {**MEMORY, "content": "result", "compressed": True}


@dataclass(slots=True)
class BarrierProvider:
    """Control real provider responses and record concurrent request arrivals."""

    url: str = ""
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    requests: list[bytes] = field(default_factory=list[bytes])


@pytest.fixture
def barrier_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[BarrierProvider]:
    """Hold a loopback provider at a response barrier until the test releases it."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    state = BarrierProvider()

    class Handler(BaseHTTPRequestHandler):
        """Serve structured completions after a deterministic request barrier."""

        def do_POST(self) -> None:
            """Record the request before waiting for the response release."""
            state.requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            state.entered.set()
            if not state.release.wait(10):
                return
            body = completion()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError, ConnectionResetError:
                return

        def log_message(self, format: str, *args: str) -> None:
            """Keep provider diagnostics out of the protocol test output."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1/"
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("cancel", ["none", "initiator", "follower"])
async def test_shared_compression_preserves_waiter_progress_and_cancellation(
    cli: Path, barrier_provider: BarrierProvider, monkeypatch: pytest.MonkeyPatch, cancel: str
) -> None:
    """Coalesce MCP misses while keeping each caller's progress and cancellation independent."""
    monkeypatch.setenv("LLM_MODEL", "model")
    monkeypatch.setenv("LLM_PROVIDER_URL", barrier_provider.url)
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "4")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "1024")
    monkeypatch.setattr("marabrain.memory_tools._COMPRESSION_PROGRESS_INTERVAL", 0.01)
    respond(cli, json.dumps(MEMORY))
    events = {name: anyio.Event() for name in ("initiator", "follower")}
    scopes: dict[str, anyio.CancelScope] = {}
    updates: dict[str, int] = {"initiator": 0, "follower": 0}
    results: dict[str, bool] = {}

    async with Client(create_server(Settings(domain_command_capacity=1))) as client:

        async def request(name: str) -> None:
            """Track one MCP caller's own progress and completion."""

            async def report(progress: float, total: float | None, message: str | None) -> None:
                """Signal a notification on this caller's request stream."""
                updates[name] += 1
                if updates[name] >= 2:
                    events[name].set()

            with anyio.CancelScope() as scope:
                scopes[name] = scope
                response = await client.call_tool("read_memory", {**TARGET, "query": "retry", "compress": True}, progress_handler=report)
                results[name] = response.structured_content == {**MEMORY, "content": "result", "compressed": True}

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(request, "initiator")
                assert await anyio.to_thread.run_sync(barrier_provider.entered.wait, 5)
                tasks.start_soon(request, "follower")
                await events["initiator"].wait()
                await events["follower"].wait()
                assert (await client.call_tool("read_memory", TARGET)).structured_content == MEMORY
                if cancel != "none":
                    scopes[cancel].cancel()
                barrier_provider.release.set()
        assert results == {name: True for name in ("initiator", "follower") if name != cancel}
        counts = dict(updates)
        assert (await client.call_tool("read_memory", TARGET)).structured_content == MEMORY
        assert updates == counts
    assert len(barrier_provider.requests) == 1


@pytest.mark.parametrize("transport", list(Transport))
async def test_transport_lifespan_drains_shared_provider_cleanup(cli: Path, monkeypatch: pytest.MonkeyPatch, transport: Transport) -> None:
    """Join shielded producer retirement before either transport's lifespan finishes."""
    monkeypatch.setenv("LLM_MODEL", "model")
    monkeypatch.setenv("LLM_PROVIDER_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "4")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "1024")
    entered, cleaning, released, retired = (anyio.Event() for _ in range(4))
    shutdown_complete = False
    errors: list[str] = []

    async def compress(settings: LLMSettings, query: str, content: str) -> str:
        """Block production and require explicit permission to finish shielded cleanup."""
        try:
            entered.set()
            await anyio.sleep_forever()
            raise AssertionError("Producer unexpectedly finished without cancellation.")
        finally:
            with anyio.CancelScope(shield=True):
                cleaning.set()
                await released.wait()
                retired.set()

    monkeypatch.setattr("marabrain.memory_tools.compress_memory", compress)
    respond(cli, json.dumps(MEMORY))
    server = create_server(Settings(transport=transport, bearer_secret=SecretStr("s" * 32)))
    app = server.http_app(stateless_http=True)
    lifespan = server._lifespan_manager() if transport == Transport.STDIO else app.router.lifespan_context(app)

    async def request() -> None:
        """Keep a tool waiter active as its application shuts down."""
        try:
            await server.call_tool("read_memory", {**TARGET, "query": "retry", "compress": True})
        except ToolError as error:
            errors.append(str(error))

    async def release_cleanup() -> None:
        """Check that teardown is still waiting at the provider retirement barrier."""
        await cleaning.wait()
        assert not shutdown_complete
        assert not retired.is_set()
        released.set()

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(release_cleanup)
            async with lifespan:
                tasks.start_soon(request)
                await entered.wait()
            shutdown_complete = True
            assert retired.is_set()
    assert errors == ["LLM compression failed."]
