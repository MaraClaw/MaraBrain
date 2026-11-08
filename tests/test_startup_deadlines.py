"""Prove total preparation budgets and serving lifetime through application processes."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest
from fastmcp import Client
from test_engine_lifecycle import application, assert_reaped, await_file, await_http, health
from test_engine_lifecycle import runtime as runtime

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
        time.sleep(float(os.environ.get("PREEXISTING_DELAY", "0")))
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
        await await_file(budget_runtime / f"{event}-started")
        with anyio.fail_after(5):
            assert await process.wait() != 0
        assert process.stdout is not None
        assert b"".join([chunk async for chunk in process.stdout]) == b""
    commands = [json.loads(line)["args"] for line in (budget_runtime / "events").read_text().splitlines()]
    assert (["--json", "domain", "list"] in commands) == (phase == "sync")
    assert len(list(budget_runtime.glob("launched-*"))) == (0 if phase == "probe" else 1)
    assert_reaped(budget_runtime)


@pytest.mark.asyncio
async def test_total_deadline_is_not_reset_after_a_slow_preexisting_probe(budget_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spend the same startup budget on an earlier probe and later synchronization."""
    monkeypatch.setenv("PREEXISTING_DELAY", "0.7")
    monkeypatch.setenv("MARABRAIN_HEALTH_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("MARABRAIN_STARTUP_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("BLOCK_PHASE", "sync")
    async with application(budget_runtime) as process:
        await await_file(budget_runtime / "sync-started")
        events = [json.loads(line) for line in (budget_runtime / "events").read_text().splitlines()]
        sync = next(event for event in events if event["args"] == ["--json", "domain", "list"])
        with anyio.fail_after(5):
            while True:
                try:
                    os.kill(sync["pid"], 0)
                except ProcessLookupError:
                    break
                await anyio.sleep(0.01)
            assert time.monotonic() - events[0]["time"] < 1.8
            assert await process.wait() != 0
    assert_reaped(budget_runtime)


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
            with anyio.fail_after(10):
                await received.wait()
            listing = await anyio.run_process(["ps", "-A", "-o", "pid=,ppid="])
            children = [int(pid) for pid, parent in (line.split() for line in listing.stdout.decode().splitlines()) if int(parent) == process.pid]
            assert len(children) == 1
            with anyio.fail_after(5):
                assert await process.wait() != 0
            with pytest.raises(ProcessLookupError):
                os.kill(children[0], 0)
        tasks.cancel_scope.cancel()
    assert not list(budget_runtime.glob("launched-*"))
    assert not (budget_runtime / "xdg_config_home/crystalline/config.yaml").exists()
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
        await await_file(budget_runtime / "sync-started")
        if transport == "http":
            await await_http(budget_runtime)
        first = json.loads((budget_runtime / "events").read_text().splitlines()[0])["time"]
        await anyio.sleep(max(0, first + 1.7 - time.monotonic()))
        if transport == "stdio":
            await health(process)
            assert process.stdin is not None
            await process.stdin.aclose()
        else:
            async with Client(f"http://127.0.0.1:{unused_tcp_port}/mcp", auth="s" * 32) as client:
                result = await client.call_tool("health", {})
                assert result.structured_content == {"status": "ok", "version": "0.4.1"}
            process.send_signal(signal.SIGTERM)
        with anyio.fail_after(9):
            assert await process.wait() == 0
    assert_reaped(budget_runtime)


@pytest.mark.asyncio
async def test_external_startup_expiry_preserves_preexisting_daemon(budget_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancel initial synchronization while leaving an externally owned daemon running."""
    async with await anyio.open_process(["crystalline", "serve", "--http", "off"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as external:
        try:
            await await_file(budget_runtime / "owner")
            monkeypatch.setenv("MARABRAIN_ENGINE_MODE", "external")
            monkeypatch.setenv("MARABRAIN_STARTUP_TIMEOUT_SECONDS", "0.5")
            monkeypatch.setenv("BLOCK_PHASE", "sync")
            async with application(budget_runtime) as process:
                await await_file(budget_runtime / "sync-started")
                with anyio.fail_after(5):
                    assert await process.wait() != 0
            assert external.returncode is None
            assert len(list(budget_runtime.glob("launched-*"))) == 1
        finally:
            external.terminate()
            await external.wait()
    assert_reaped(budget_runtime)
