"""Drive shared workspace data/control stdout pressure with barrier-controlled real children."""

import os
import sys
import threading
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream
from fastmcp.exceptions import ToolError

from marabrain.config import Settings
from marabrain.crystalline import run_json
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import run_startup_command
from marabrain.transient import CaptureLimitError, StdoutCapture


@pytest.mark.parametrize("control", [False, True])
async def test_workspace_data_and_control_share_bytes_but_applications_do_not(cli: Path, monkeypatch: pytest.MonkeyPatch, control: bool) -> None:
    """Refuse a sibling workspace's capture while either lane owns bytes, then reuse them after consumption."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    (cli / "crystalline").write_text(
        f"#!{sys.executable}\nimport os, socket, sys\n"
        f"s = socket.create_connection(('127.0.0.1', {port})) if sys.argv[-1] == 'held' else None\n"
        "if s: s.sendall(str(os.getpid()).encode())\nos.write(1, b'{}')\nif s: s.recv(1)\n"
    )
    app = EngineContext.for_application(Settings(max_transient_processes=3, max_capture_bytes=1000000, max_total_capture_bytes=2))
    first = app.for_workspace("first", cli)
    second = app.for_workspace("second", cli)
    independent = EngineContext.for_application(Settings(max_total_capture_bytes=2)).for_workspace("independent", cli)
    retained, release, done = threading.Event(), anyio.Event(), anyio.Event()
    arrived, receive = anyio.create_memory_object_stream[int](1)
    pids: list[int] = []
    append = StdoutCapture.append

    def observe(capture: StdoutCapture, chunk: bytes) -> None:
        """Publish successful retention rather than relying on OS write timing."""
        append(capture, chunk)
        retained.set()

    async def barrier(connection: SocketStream) -> None:
        """Hold the real child live until the competing operations have completed."""
        async with connection:
            await arrived.send(int(await connection.receive()))
            await release.wait()
            await connection.send(b"x")

    async def hold() -> None:
        """Retain one lane's complete raw payload until the test releases the child."""
        if control:
            with await run_startup_command([str(cli / "crystalline"), "held"], first) as raw:
                assert raw == b"{}"
        else:
            assert await run_json(["held"], context=first) == {}
        done.set()

    monkeypatch.setattr(StdoutCapture, "append", observe)
    with anyio.fail_after(10):
        async with listener, arrived, receive, anyio.create_task_group() as tasks:
            tasks.start_soon(listener.serve, barrier)
            tasks.start_soon(hold)
            pids.append(await receive.receive())
            try:
                assert await anyio.to_thread.run_sync(retained.wait, 5)
                if control:
                    with pytest.raises(ToolError, match="capture limit"):
                        await run_json(["read"], context=second)
                else:
                    with pytest.raises(CaptureLimitError):
                        await run_startup_command([str(cli / "crystalline"), "status"], second)
                assert await run_json(["read"], context=independent) == {}
            finally:
                release.set()
            await done.wait()
            tasks.cancel_scope.cancel()
    [pid] = pids
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not [worker for worker in threading.enumerate() if worker.name == "marabrain-cli"]
    assert await run_json(["read"], context=second) == {}
    with await run_startup_command([str(cli / "crystalline"), "status"], second) as raw:
        assert raw == b"{}"
