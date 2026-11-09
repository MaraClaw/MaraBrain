"""Exercise managed engine ownership and shutdown through real application processes."""

import json
import os
import signal
import subprocess
import sys
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from anyio.abc import Process
from anyio.streams.buffered import BufferedByteReceiveStream
from fastmcp import Client
from memory_fake import WORKSPACE_ID, workspace_context

FAKE_ENGINE = '''#!/usr/bin/env python3
"""Emulate a singleton daemon and its control client in an isolated state directory."""
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

root = Path(os.environ["ENGINE_TEST_ROOT"])
pid = os.getpid()
(root / f"child-{pid}").touch()
args = sys.argv[1:]
if args == ["--json", "domain", "list"]:
    if os.environ.get("DOMAIN_LIST_FAILURE"):
        print('{"domains":[{"invalid":true}]}')
        sys.exit(0)
    print('{"domains":[]}')
    sys.exit(0)
if args == ["ctl", "status"]:
    checked = root / ("checked-" + os.environ.get("INSTANCE", "app"))
    first = not checked.exists()
    checked.touch()
    if first and os.environ.get("FORCE_RACE"):
        sys.exit(1)
    if not (root / "owner").exists():
        sys.exit(1)
    owner, started = (root / "owner").read_text().split()
    if os.environ.get("HANG_PROBE"):
        (root / "probe-hanging").touch()
        time.sleep(60)
    if time.monotonic() - float(started) < float(os.environ.get("READY_DELAY", "0")):
        sys.exit(1)
    print(json.dumps({"pid": int(owner)}))
    sys.exit(0)
if args[:2] == ["--json", "read"]:
    print('{"content":"ready"}')
    sys.exit(0)
if args != ["serve", "--http", "off"]:
    sys.exit(9)
(root / f"launched-{pid}").touch()
lock = (root / "singleton").open("w")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    time.sleep(0.5)
    sys.exit(3)
if os.environ.get("DIE_STARTUP"):
    sys.exit(4)
(root / "owner").write_text(f"{pid} {time.monotonic()}")
(root / "environment-key").write_text(os.environ.get("EMBEDDING_API_KEY", ""))
if os.environ.get("IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("engine-noise", flush=True)
print("engine-noise", file=sys.stderr, flush=True)
while True:
    signal.pause()
'''


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_root: Path) -> Iterator[Path]:
    """Isolate state and fake children, retaining persisted data across application runs."""
    for name in tuple(os.environ):
        if name.startswith(("MARABRAIN_", "EMBEDDING_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("MARABRAIN_WORKSPACE_ROOT", str(workspace_root))
    executable = tmp_path / "crystalline"
    executable.write_text(FAKE_ENGINE)
    executable.chmod(0o755)
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("ENGINE_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path), os.environ["PATH"]]))
    monkeypatch.setenv("MARABRAIN_ENGINE_MODE", "managed")
    monkeypatch.setenv("MARABRAIN_ENGINE_STARTUP_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("MARABRAIN_HEALTH_TIMEOUT_SECONDS", "0.5")
    (tmp_path / "persisted").write_text("keep this memory")
    yield tmp_path


@asynccontextmanager
async def application(root: Path, *, command: list[str] | None = None) -> AsyncGenerator[Process]:
    """Run the shipped module with captured protocol output and a diagnostic artifact."""
    with (root / "application-stderr").open("ab") as errors:
        async with await anyio.open_process(command or [sys.executable, "-m", "marabrain"], stderr=errors, start_new_session=True) as process:
            try:
                yield process
            finally:
                with anyio.CancelScope(shield=True):
                    leaked = False
                    if process.returncode is not None:
                        try:
                            os.killpg(process.pid, 0)
                        except ProcessLookupError:
                            leaked = False
                        else:
                            leaked = True
                    if process.returncode is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        with anyio.move_on_after(9):
                            await process.wait()
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        await process.wait()
                    await process.wait()
                    assert not leaked, "Application exited with live descendants in its owned process group."


async def await_file(path: Path) -> None:
    """Wait for a child-generated event with a bounded diagnostic failure."""
    with anyio.fail_after(10):
        while not path.exists():
            await anyio.sleep(0.01)


async def await_http(root: Path) -> None:
    """Wait for Uvicorn's listener-ready event in the diagnostic artifact."""
    with anyio.fail_after(10):
        while b"Uvicorn running" not in (root / "application-stderr").read_bytes():
            await anyio.sleep(0.01)


async def begin_workspace(process: Process) -> BufferedByteReceiveStream:
    """Initialize MCP and request workspace activation without waiting for engine readiness."""
    assert process.stdin is not None and process.stdout is not None
    reader = BufferedByteReceiveStream(process.stdout)
    with anyio.fail_after(10):
        await process.stdin.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
                }
            ).encode()
            + b"\n"
        )
        assert json.loads(await reader.receive_until(b"\n", 100000))["id"] == 1
        await process.stdin.send(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        await process.stdin.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "read_memory", "arguments": {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "ready"}},
                }
            ).encode()
            + b"\n"
        )
    return reader


async def health(process: Process) -> None:
    """Activate a workspace and verify health without any engine noise on protocol stdout."""
    reader = await begin_workspace(process)
    assert process.stdin is not None
    with anyio.fail_after(10):
        activated = json.loads(await reader.receive_until(b"\n", 100000))
        assert activated["id"] == 2 and not activated["result"].get("isError", False)
        await process.stdin.send(b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"health","arguments":{}}}\n')
        result = json.loads(await reader.receive_until(b"\n", 100000))
    assert result["result"]["structuredContent"] == {"status": "ok", "version": "0.4.2"}


async def activation_failed(process: Process, reader: BufferedByteReceiveStream, *, exit_code: int = 0) -> None:
    """Require a bounded activation tool error and the expected transport exit after EOF."""
    with anyio.fail_after(5):
        result = json.loads(await reader.receive_until(b"\n", 100000))
        assert result["id"] == 2 and result["result"]["isError"] is True
        assert process.stdin is not None
        await process.stdin.aclose()
        assert await process.wait() == exit_code


def assert_reaped(root: Path) -> None:
    """Verify every recorded direct engine child has left the process table and data remains."""
    for child in root.glob("child-*"):
        with pytest.raises(ProcessLookupError):
            os.kill(int(child.name.removeprefix("child-")), 0)
    assert (root / "persisted").read_text() == "keep this memory"


@pytest.mark.asyncio
async def test_managed_stdio_waits_for_readiness_and_reaps_on_eof(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify delayed workspace readiness precedes a successful tool call and EOF reaps its daemon."""
    monkeypatch.setenv("READY_DELAY", "0.15")
    async with application(runtime) as process:
        await health(process)
        assert process.stdin is not None
        await process.stdin.aclose()
        with anyio.fail_after(5):
            assert await process.wait() == 0
    assert_reaped(runtime)


@pytest.mark.parametrize("failure", ["DIE_STARTUP", "HANG_PROBE", "late"])
@pytest.mark.asyncio
async def test_startup_failure_never_serves_and_reaps_children(runtime: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """Verify early death, hung status, and late readiness fail workspace activation and reap children."""
    monkeypatch.setenv("MARABRAIN_ENGINE_STARTUP_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("READY_DELAY", "2" if failure == "late" else "0")
    if failure != "late":
        monkeypatch.setenv(failure, "1")
    async with application(runtime) as process:
        reader = await begin_workspace(process)
        await activation_failed(process, reader, exit_code=1 if failure == "DIE_STARTUP" else 0)
    assert_reaped(runtime)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("phase", ["serving", "readiness", "stubborn"])
@pytest.mark.asyncio
async def test_signal_reaps_owned_daemon(runtime: Path, monkeypatch: pytest.MonkeyPatch, signum: signal.Signals, phase: str) -> None:
    """Verify TERM and INT stop startup or serving and escalate for an uncooperative daemon."""
    if phase == "readiness":
        monkeypatch.setenv("HANG_PROBE", "1")
    if phase == "stubborn":
        monkeypatch.setenv("IGNORE_TERM", "1")
    async with application(runtime) as process:
        if phase == "readiness":
            await begin_workspace(process)
            await await_file(runtime / "probe-hanging")
        else:
            await health(process)
        process.send_signal(signum)
        with anyio.fail_after(9):
            await process.wait()
    assert_reaped(runtime)


@pytest.mark.asyncio
async def test_later_daemon_death_fails_application(runtime: Path) -> None:
    """Verify an unexpected daemon exit tears down an active MCP connection with failure."""
    async with application(runtime) as process:
        await health(process)
        owner = int((runtime / "owner").read_text().split()[0])
        os.kill(owner, signal.SIGKILL)
        with anyio.fail_after(5):
            assert await process.wait() != 0
    assert_reaped(runtime)


@pytest.mark.asyncio
async def test_eof_during_readiness_reaps_owned_daemon(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify EOF cancels an in-flight readiness probe and reaps its daemon before serving."""
    monkeypatch.setenv("HANG_PROBE", "1")
    async with application(runtime) as process:
        await begin_workspace(process)
        await await_file(runtime / "probe-hanging")
        assert process.stdin is not None
        await process.stdin.aclose()
        with anyio.fail_after(5):
            await process.wait()
    assert_reaped(runtime)


@pytest.mark.parametrize("mode", ["managed", "external"])
@pytest.mark.asyncio
async def test_existing_daemon_is_preserved_before_configuration(runtime: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Verify managed refusal and external serving both preserve a preexisting daemon and skip initialization."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(anyio.abc.SocketAttribute.local_address)[1]
    initialized = anyio.Event()
    context = workspace_context()
    config = Path(context.environment["XDG_CONFIG_HOME"]) / "crystalline/config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("domains: []\n")

    async def record_initialization(stream: anyio.abc.SocketStream) -> None:
        """Record any forbidden provider probe while allowing it to finish successfully."""
        async with stream:
            await stream.receive()
            initialized.set()
            body = b'{"data":[{"index":0,"embedding":[0.1,0.2,0.3]}]}'
            await stream.send(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)

    async with listener, anyio.create_task_group() as tasks:
        tasks.start_soon(listener.serve, record_initialization)
        async with await anyio.open_process(
            ["crystalline", "serve", "--http", "off"], env=dict(context.environment), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ) as external:
            try:
                await await_file(runtime / "owner")
                monkeypatch.setenv("EMBEDDING_MODEL", "existing-daemon-probe")
                monkeypatch.setenv("EMBEDDING_PROVIDER_URL", f"http://127.0.0.1:{port}")
                monkeypatch.setenv("EMBEDDING_DIMS", "3")
                monkeypatch.setenv("NO_PROXY", "127.0.0.1")
                monkeypatch.setenv("MARABRAIN_ENGINE_MODE", mode)
                async with application(runtime) as process:
                    if mode == "external":
                        await health(process)
                        assert process.stdin is not None
                        await process.stdin.aclose()
                        with anyio.fail_after(5):
                            assert await process.wait() == 0
                    else:
                        reader = await begin_workspace(process)
                        await activation_failed(process, reader)
                assert external.returncode is None
                assert (runtime / "checked-app").exists()
                assert len(list(runtime.glob("launched-*"))) == 1
                assert not initialized.is_set()
                assert config.read_text() == "domains: []\n"
            finally:
                external.terminate()
                await external.wait()
        tasks.cancel_scope.cancel()
    assert_reaped(runtime)


@pytest.mark.asyncio
async def test_competing_start_never_adopts_the_lock_holder(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a contender fails while its child still waits for the lock and preserves the winner."""
    monkeypatch.setenv("FORCE_RACE", "1")
    monkeypatch.setenv("INSTANCE", "winner")
    async with application(runtime) as winner:
        await health(winner)
        monkeypatch.setenv("INSTANCE", "contender")
        async with application(runtime) as contender:
            reader = await begin_workspace(contender)
            await activation_failed(contender, reader, exit_code=1)
        assert (runtime / "checked-contender").exists()
        assert len(list(runtime.glob("launched-*"))) == 2
        os.kill(int((runtime / "owner").read_text().split()[0]), 0)
        assert winner.stdin is not None
        await winner.stdin.aclose()
        with anyio.fail_after(5):
            assert await winner.wait() == 0
    assert_reaped(runtime)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, None])
@pytest.mark.asyncio
async def test_signal_interrupts_blocking_embedding_initialization(runtime: Path, monkeypatch: pytest.MonkeyPatch, signum: signal.Signals | None) -> None:
    """Verify signals and EOF cancel an in-flight provider probe before engine launch or config writes."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(anyio.abc.SocketAttribute.local_address)[1]
    received = anyio.Event()

    async def hold_request(stream: anyio.abc.SocketStream) -> None:
        """Hold the provider request open until application shutdown has been observed."""
        async with stream:
            await stream.receive()
            received.set()
            await anyio.sleep_forever()

    monkeypatch.setenv("EMBEDDING_MODEL", "probe-test")
    monkeypatch.setenv("EMBEDDING_DIMS", "3")
    monkeypatch.setenv("EMBEDDING_PROVIDER_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    config = Path(workspace_context().environment["XDG_CONFIG_HOME"]) / "crystalline/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("domains: []\n")
    async with listener, anyio.create_task_group() as tasks:
        tasks.start_soon(listener.serve, hold_request)
        async with application(runtime) as process:
            await begin_workspace(process)
            with anyio.fail_after(10):
                await received.wait()
            listing = await anyio.run_process(["ps", "-A", "-o", "pid=,ppid="])
            children = [int(pid) for pid, parent in (line.split() for line in listing.stdout.decode().splitlines()) if int(parent) == process.pid]
            assert len(children) == 1
            if signum is None:
                assert process.stdin is not None
                await process.stdin.aclose()
            else:
                process.send_signal(signum)
            with anyio.fail_after(5):
                await process.wait()
            with pytest.raises(ProcessLookupError):
                os.kill(children[0], 0)
        tasks.cancel_scope.cancel()
    assert not list(runtime.glob("launched-*"))
    assert config.read_text() == "domains: []\n"
    assert_reaped(runtime)


@pytest.mark.asyncio
async def test_initialized_secret_overlay_reaches_owned_daemon(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify successful initialization forwards a resolved file credential without persisting its value."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(anyio.abc.SocketAttribute.local_address)[1]

    async def respond_to_probe(stream: anyio.abc.SocketStream) -> None:
        """Answer the initializer's real provider request with the configured vector dimensions."""
        async with stream:
            request = bytearray()
            while b"\r\n\r\n" not in request:
                request += await stream.receive()
            assert b"authorization: bearer lifecycle-secret" in bytes(request).lower()
            body = b'{"data":[{"index":0,"embedding":[0.1,0.2,0.3]}]}'
            await stream.send(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)

    key_file = runtime / "api-key"
    key_file.write_text("lifecycle-secret\n")
    monkeypatch.setenv("EMBEDDING_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("EMBEDDING_MODEL", "probe-test")
    monkeypatch.setenv("EMBEDDING_DIMS", "3")
    monkeypatch.setenv("EMBEDDING_PROVIDER_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    async with listener, anyio.create_task_group() as tasks:
        tasks.start_soon(listener.serve, respond_to_probe)
        async with application(runtime) as process:
            await health(process)
            assert (runtime / "environment-key").read_text() == "lifecycle-secret"
            config = (Path(workspace_context().environment["XDG_CONFIG_HOME"]) / "crystalline/config.yaml").read_text()
            assert "probe-test" in config and "lifecycle-secret" not in config
            assert process.stdin is not None
            await process.stdin.aclose()
            with anyio.fail_after(5):
                assert await process.wait() == 0
        tasks.cancel_scope.cancel()
    assert b"lifecycle-secret" not in (runtime / "application-stderr").read_bytes()
    assert_reaped(runtime)


@pytest.mark.parametrize("kind", ["file", "null"])
@pytest.mark.asyncio
async def test_external_stdio_accepts_regular_file_and_null_input(runtime: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """Verify external mode accepts redirected stdin without a pipe-only event registration."""
    monkeypatch.setenv("MARABRAIN_ENGINE_MODE", "external")
    source = runtime / "input"
    source.touch()
    with source.open("rb") as input_file:
        with anyio.fail_after(5):
            result = await anyio.run_process([sys.executable, "-m", "marabrain"], stdin=input_file if kind == "file" else subprocess.DEVNULL)
    assert result.stdout == b""
    assert not list(runtime.glob("launched-*"))


@pytest.mark.parametrize("finish", ["term", "death"])
@pytest.mark.asyncio
async def test_managed_http_shares_readiness_and_cleanup(runtime: Path, monkeypatch: pytest.MonkeyPatch, unused_tcp_port: int, finish: str) -> None:
    """Verify authenticated HTTP starts after engine readiness and exits on TERM or engine death."""
    monkeypatch.setenv("MARABRAIN_TRANSPORT", "http")
    monkeypatch.setenv("MARABRAIN_BEARER_SECRET", "s" * 32)
    monkeypatch.setenv("MARABRAIN_PORT", str(unused_tcp_port))
    monkeypatch.setenv("READY_DELAY", "0.15")
    async with application(runtime) as process:
        await await_http(runtime)
        async with Client(f"http://127.0.0.1:{unused_tcp_port}/mcp", auth="s" * 32) as client:
            await client.call_tool("read_memory", {"workspace_id": WORKSPACE_ID, "domain": "notes", "permalink": "ready"})
            result = await client.call_tool("health", {})
            assert result.structured_content == {"status": "ok", "version": "0.4.2"}
        if finish == "term":
            process.send_signal(signal.SIGTERM)
        else:
            os.kill(int((runtime / "owner").read_text().split()[0]), signal.SIGKILL)
        with anyio.fail_after(9):
            code = await process.wait()
        if finish == "death":
            assert code != 0
    assert_reaped(runtime)


@pytest.mark.asyncio
async def test_transport_startup_failure_reaps_owned_daemon(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify an HTTP bind failure exits without launching any lazy workspace engine."""
    async with await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0) as listener:
        monkeypatch.setenv("MARABRAIN_TRANSPORT", "http")
        monkeypatch.setenv("MARABRAIN_BEARER_SECRET", "s" * 32)
        monkeypatch.setenv("MARABRAIN_PORT", str(listener.extra(anyio.abc.SocketAttribute.local_address)[1]))
        async with application(runtime) as process:
            with anyio.fail_after(9):
                assert await process.wait() != 0
    assert len(list(runtime.glob("launched-*"))) == 0
    assert_reaped(runtime)
