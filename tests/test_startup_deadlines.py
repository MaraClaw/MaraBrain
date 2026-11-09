"""Prove total preparation budgets and serving lifetime through application processes."""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memory_fake import WORKSPACE_ID, workspace_context
from pydantic import SecretStr
from test_engine_lifecycle import activation_failed, application, assert_reaped, await_file, await_http, begin_workspace
from test_engine_lifecycle import runtime as runtime

from marabrain import workspaces
from marabrain.config import EngineMode, Settings, Transport
from marabrain.domain_registry import DomainRegistry
from marabrain.engine_context import EngineContext
from marabrain.workspaces import WorkspaceManager

BUDGET_ENGINE = '''
"""Expose deterministic startup phase events and retain a foreground daemon."""
import json
import os
import signal
import sys
import time
from pathlib import Path

root = Path(os.environ["ENGINE_TEST_ROOT"])
args = sys.argv[1:]
pid = os.getpid()
(root / f"child-{pid}").touch()
with (root / "events").open("a") as stream:
    stream.write(json.dumps({"args": args, "pid": pid, "time": time.monotonic(), "key": os.environ.get("EMBEDDING_API_KEY", ""),
                            "state": os.environ["XDG_STATE_HOME"], "config": os.environ["XDG_CONFIG_HOME"]}) + "\\n")
phase = os.environ.get("BLOCK_PHASE")
if args == ["ctl", "status"]:
    if not (root / "owner").exists():
        (root / "preexisting-started").touch()
        if phase == "probe":
            signal.pause()
        sys.exit(1)
    (root / "readiness-started").touch()
    if phase == "readiness":
        signal.pause()
    print(json.dumps({"pid": int((root / "owner").read_text().split()[0])}))
elif args == ["--json", "domain", "list"]:
    (root / "sync-started").touch()
    if phase == "sync":
        signal.pause()
    print('{"domains":[]}')
elif args == ["serve", "--http", "off"]:
    (root / f"launched-{pid}").touch()
    (root / "owner").write_text(f"{pid} {time.monotonic()}")
    while True:
        signal.pause()
elif args[:2] == ["--json", "read"]:
    print(json.dumps({"store": os.environ["XDG_STATE_HOME"], "key": os.environ.get("EMBEDDING_API_KEY", "")}))
else:
    sys.exit(9)
'''


@pytest.fixture
def budget_runtime(runtime: Path) -> Path:
    """Use phase events to distinguish preparation timeout from MCP transport lifetime."""
    (runtime / "crystalline").write_text(f"#!{sys.executable}\n" + BUDGET_ENGINE)
    return runtime


@pytest.mark.parametrize("phase", ["probe", "readiness", "sync"])
@pytest.mark.asyncio
async def test_total_startup_expiry_stops_each_engine_phase_before_serving(budget_runtime: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    """Bound every preparation stage and reap all owned engine clients and daemons."""
    monkeypatch.setenv("MARABRAIN_STARTUP_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("MARABRAIN_HEALTH_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("BLOCK_PHASE", phase)
    event = "preexisting" if phase == "probe" else phase
    async with application(budget_runtime) as process:
        reader = await begin_workspace(process)
        await await_file(budget_runtime / f"{event}-started")
        await activation_failed(process, reader)
    commands = [json.loads(line)["args"] for line in (budget_runtime / "events").read_text().splitlines()]
    assert (["--json", "domain", "list"] in commands) == (phase == "sync")
    assert len(list(budget_runtime.glob("launched-*"))) == (0 if phase == "probe" else 1)
    assert_reaped(budget_runtime)


@pytest.mark.asyncio
@pytest.mark.usefixtures("runtime")
async def test_total_deadline_is_not_reset_after_a_slow_preexisting_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spend the same startup budget on an earlier probe and later synchronization."""
    settings = Settings(transport=Transport.HTTP, bearer_secret=SecretStr("s" * 32), engine_mode=EngineMode.MANAGED, startup_timeout_seconds=1.5)
    started = anyio.current_time()
    elapsed = 0.0
    deadlines: list[float | None] = []
    workspace_ready = False

    def current_time() -> float:
        """Advance startup time only when a preparation phase consumes its budget."""
        return started + elapsed

    @asynccontextmanager
    async def prepare_engine(settings: Settings, *, context: EngineContext | None = None, deadline: float | None = None) -> AsyncGenerator[None]:
        """Consume part of the startup budget before yielding a ready engine."""
        nonlocal elapsed
        deadlines.append(deadline)
        elapsed = 0.7
        yield

    async def synchronize(registry: DomainRegistry, *, deadline: float | None = None) -> None:
        """Finish synchronization after the original budget but before a reset budget."""
        nonlocal elapsed
        deadlines.append(deadline)
        elapsed = 1.6

    monkeypatch.setattr(anyio, "current_time", current_time)
    monkeypatch.setattr(workspaces, "managed_engine", prepare_engine)
    monkeypatch.setattr(DomainRegistry, "synchronize", synchronize)
    manager = WorkspaceManager(settings, EngineContext.for_application(settings))
    async with manager.lifespan():
        with pytest.raises(ToolError, match="activation failed"):
            await manager.get(WORKSPACE_ID)
            workspace_ready = True

    assert deadlines == [started + 1.5, started + 1.5]
    assert not workspace_ready


@pytest.mark.asyncio
async def test_initializer_expiry_reaps_writer_without_launching_daemon(budget_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancel a blocking provider subprocess within the total budget before YAML writes."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(anyio.abc.SocketAttribute.local_address)[1]
    received = anyio.Event()

    async def hold_request(stream: anyio.abc.SocketStream) -> None:
        """Expose provider arrival without responding before the initializer expires."""
        async with stream:
            await stream.receive()
            received.set()
            await anyio.sleep_forever()

    monkeypatch.setenv("MARABRAIN_STARTUP_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("EMBEDDING_MODEL", "deadline-model")
    monkeypatch.setenv("EMBEDDING_DIMS", "3")
    monkeypatch.setenv("EMBEDDING_PROVIDER_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    async with listener, anyio.create_task_group() as tasks:
        tasks.start_soon(listener.serve, hold_request)
        async with application(budget_runtime) as process:
            reader = await begin_workspace(process)
            with anyio.fail_after(10):
                await received.wait()
            listing = await anyio.run_process(["ps", "-A", "-o", "pid=,ppid="])
            children = [int(pid) for pid, parent in (line.split() for line in listing.stdout.decode().splitlines()) if int(parent) == process.pid]
            assert len(children) == 1
            await activation_failed(process, reader)
            with pytest.raises(ProcessLookupError):
                os.kill(children[0], 0)
        tasks.cancel_scope.cancel()
    assert not list(budget_runtime.glob("launched-*"))
    assert not (Path(workspace_context().environment["XDG_CONFIG_HOME"]) / "crystalline/config.yaml").exists()
    assert_reaped(budget_runtime)


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.asyncio
async def test_completed_startup_serves_beyond_total_deadline(
    budget_runtime: Path, monkeypatch: pytest.MonkeyPatch, unused_tcp_port: int, transport: str
) -> None:
    """Serve health after the preparation deadline over both real MCP transports."""
    monkeypatch.setenv("MARABRAIN_STARTUP_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("MARABRAIN_TRANSPORT", transport)
    monkeypatch.setenv("MARABRAIN_BEARER_SECRET", "s" * 32)
    monkeypatch.setenv("MARABRAIN_PORT", str(unused_tcp_port))
    async with application(budget_runtime) as process:
        reader = await begin_workspace(process) if transport == "stdio" else None
        if transport == "http":
            await await_http(budget_runtime)
            async with Client(f"http://127.0.0.1:{unused_tcp_port}/mcp", auth="s" * 32) as client:
                await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "ready"})
        else:
            assert reader is not None
            with anyio.fail_after(10):
                activated = json.loads(await reader.receive_until(b"\n", 100000))
                assert activated["id"] == 2 and not activated["result"].get("isError", False)
        await await_file(budget_runtime / "sync-started")
        first = json.loads((budget_runtime / "events").read_text().splitlines()[0])["time"]
        await anyio.sleep(max(0, first + 1.7 - time.monotonic()))
        if transport == "stdio":
            assert process.stdin is not None and reader is not None
            await process.stdin.send(b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"health","arguments":{}}}\n')
            result = json.loads(await reader.receive_until(b"\n", 100000))
            assert result["result"]["structuredContent"] == {"status": "ok", "version": "0.4.2"}
            await process.stdin.aclose()
        else:
            async with Client(f"http://127.0.0.1:{unused_tcp_port}/mcp", auth="s" * 32) as client:
                result = await client.call_tool("health", {})
                assert result.structured_content == {"status": "ok", "version": "0.4.2"}
            process.send_signal(signal.SIGTERM)
        with anyio.fail_after(9):
            assert await process.wait() == 0
    assert_reaped(budget_runtime)


@pytest.mark.asyncio
async def test_external_startup_expiry_preserves_preexisting_daemon(budget_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancel initial synchronization while leaving an externally owned daemon running."""
    async with await anyio.open_process(
        ["crystalline", "serve", "--http", "off"], env=dict(workspace_context().environment), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ) as external:
        try:
            await await_file(budget_runtime / "owner")
            monkeypatch.setenv("MARABRAIN_ENGINE_MODE", "external")
            monkeypatch.setenv("MARABRAIN_STARTUP_TIMEOUT_SECONDS", "0.5")
            monkeypatch.setenv("BLOCK_PHASE", "sync")
            async with application(budget_runtime) as process:
                reader = await begin_workspace(process)
                await await_file(budget_runtime / "sync-started")
                await activation_failed(process, reader)
            assert external.returncode is None
            assert len(list(budget_runtime.glob("launched-*"))) == 1
        finally:
            external.terminate()
            await external.wait()
    assert_reaped(budget_runtime)
