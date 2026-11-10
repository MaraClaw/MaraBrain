"""Dispose retained prefixes on real command failure, deadline, cancellation, and launch errors."""

import os
import sys
import threading
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream
from fastmcp.exceptions import ToolError

from marabrain.crystalline import run_json
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import run_startup_command
from marabrain.transient import CaptureLimitError, StdoutCapture


@pytest.mark.parametrize("control", [False, True])
@pytest.mark.parametrize("failure", ["cancel", "timeout", "overflow"])
async def test_partial_capture_failure_reaps_child_and_releases_owned_prefix(cli: Path, monkeypatch: pytest.MonkeyPatch, control: bool, failure: str) -> None:
    """Retain a prefix before triggering failure, then require the complete budget to be reusable."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    (cli / "crystalline").write_text(
        f"#!{sys.executable}\nimport os,socket\ns=socket.create_connection(('127.0.0.1', {port}))\n"
        "s.sendall(str(os.getpid()).encode())\nos.write(1,b'{}')\ns.recv(1)\nos.write(1,b'x')\n"
    )
    context = EngineContext.capture(max_transient_processes=2, max_total_capture_bytes=2)
    retained, release, done = threading.Event(), anyio.Event(), anyio.Event()
    arrived, receive = anyio.create_memory_object_stream[int](1)
    captures: list[StdoutCapture] = []
    pids: list[int] = []
    scope = anyio.CancelScope()
    append = StdoutCapture.append

    def observe(capture: StdoutCapture, chunk: bytes) -> None:
        """Publish the accepted prefix before the next child write or failure can proceed."""
        append(capture, chunk)
        if capture not in captures:
            captures.append(capture)
        retained.set()

    async def barrier(connection: SocketStream) -> None:
        """Prevent the second write until the captured prefix has been observed."""
        async with connection:
            await arrived.send(int(await connection.receive()))
            await release.wait()
            await connection.send(b"x")

    async def request() -> None:
        """Observe the correct public error or cancellation from the actual child runner."""
        with scope:
            if control:
                error = TimeoutError if failure == "timeout" else CaptureLimitError
                if failure == "cancel":
                    await run_startup_command([str(cli / "crystalline")], context)
                else:
                    with pytest.raises(error):
                        await run_startup_command([str(cli / "crystalline")], context, deadline=anyio.current_time() + 2)
            else:
                if failure == "cancel":
                    await run_json(["read"], context=context)
                else:
                    with pytest.raises(ToolError, match="did not answer within" if failure == "timeout" else "capture limit"):
                        await run_json(["read"], context=context, timeout_seconds=2)
        done.set()

    monkeypatch.setattr(StdoutCapture, "append", observe)
    with anyio.fail_after(10):
        async with listener, arrived, receive, anyio.create_task_group() as tasks:
            tasks.start_soon(listener.serve, barrier)
            tasks.start_soon(request)
            pids.append(await receive.receive())
            try:
                assert await anyio.to_thread.run_sync(retained.wait, 5)
                assert captures[0].output == b"{}"
                if failure == "cancel":
                    scope.cancel()
                if failure == "overflow":
                    release.set()
                await done.wait()
            finally:
                tasks.cancel_scope.cancel()
    assert scope.cancelled_caught == (failure == "cancel")
    assert captures[0].output == b""
    [pid] = pids
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not [worker for worker in threading.enumerate() if worker.name == "marabrain-cli"]
    (cli / "crystalline").write_text(f"#!{sys.executable}\nprint('{{}}', end='')\n")
    assert await run_json(["read"], context=context) == {}


@pytest.mark.parametrize("control", [False, True])
async def test_launch_failure_leaves_full_byte_budget_reusable(cli: Path, control: bool) -> None:
    """Dispose a capture whose child was never created without poisoning later successful captures."""
    context = EngineContext.capture(max_total_capture_bytes=2)
    (cli / "crystalline").unlink()
    if control:
        with pytest.raises(FileNotFoundError):
            await run_startup_command([str(cli / "crystalline")], context)
    else:
        with pytest.raises(ToolError, match="is unavailable"):
            await run_json(["read"], context=context)
    executable = cli / "crystalline"
    executable.write_text(f"#!{sys.executable}\nprint('{{}}', end='')\n")
    executable.chmod(0o755)
    assert await run_json(["read"], context=context) == {}
