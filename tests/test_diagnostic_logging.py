"""Behavioral checks for safe diagnostics at the CLI and stdio boundaries."""

import logging
import os
import select
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import httpx
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from memory_fake import NOISE, respond

from marabrain.crystalline import run_json


@pytest.mark.parametrize("stdout,exit_code,category", [("private-output", 7, "exit"), ("private-output", 0, "malformed_output")])
async def test_failure_logs_only_safe_fields(cli: Path, caplog: pytest.LogCaptureFixture, stdout: str, exit_code: int, category: str) -> None:
    """Record failure categories without logging command input or output."""
    respond(cli, stdout=stdout, exit_code=exit_code)
    caplog.set_level(logging.DEBUG, logger="marabrain.crystalline")
    with pytest.raises(ToolError):
        await run_json(["delete", "private-argument"], "private-content")
    assert f"category={category}" in caplog.text
    assert "operation=delete" in caplog.text
    assert "elapsed_ms=" in caplog.text
    if exit_code:
        assert "exit_code=7" in caplog.text
    assert all(secret not in caplog.text for secret in ("private-argument", "private-content", stdout, NOISE))


@pytest.mark.parametrize("failure", ["timeout", "unavailable"])
async def test_process_failures_log_category(cli: Path, caplog: pytest.LogCaptureFixture, failure: str) -> None:
    """Distinguish timeout and unavailable process failures without exposing details."""
    caplog.set_level(logging.DEBUG, logger="marabrain.crystalline")
    if failure == "timeout":
        respond(cli, hang=True)
    else:
        (cli / "crystalline").unlink()
    with pytest.raises(ToolError):
        await run_json(["read", "private-argument"], timeout_seconds=0.1)
    assert f"category={failure}" in caplog.text
    assert "operation=read" in caplog.text and "elapsed_ms=" in caplog.text
    assert "private-argument" not in caplog.text and NOISE not in caplog.text


@pytest.mark.parametrize("level,start,complete", [("debug", True, True), ("info", False, True), ("error", False, False)])
async def test_stdio_process_filters_logs_and_hides_arguments(cli: Path, tmp_path: Path, level: str, start: bool, complete: bool) -> None:
    """Keep debug arguments private while logging to stderr at the configured level."""
    log = tmp_path / "stderr.log"
    transport = StdioTransport(
        sys.executable,
        ["-m", "marabrain"],
        env={**os.environ, "MARABRAIN_ENGINE_MODE": "external", "MARABRAIN_TRANSPORT": "stdio", "MARABRAIN_LOG_LEVEL": level},
        log_file=log,
    )
    async with Client(transport, timeout=10) as client:
        result = await client.call_tool("create_memory", {"domain": "notes", "title": "private-title", "content": "private-content"})
        assert result.structured_content is not None
    output = log.read_text()
    assert ("crystalline.start" in output) is start
    assert ("crystalline.complete" in output) is complete
    assert "private-title" not in output and "private-content" not in output and NOISE not in output


def test_http_process_emits_safe_debug_logs(cli: Path) -> None:
    """Serve an authenticated HTTP tool call with debug logs only on stderr."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    secret = "http-secret-" * 4
    env = {
        **os.environ,
        "MARABRAIN_ENGINE_MODE": "external",
        "MARABRAIN_TRANSPORT": "http",
        "MARABRAIN_PORT": str(port),
        "MARABRAIN_LOG_LEVEL": "debug",
        "MARABRAIN_BEARER_SECRET": secret,
    }
    process = subprocess.Popen([sys.executable, "-m", "marabrain"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    startup = b""
    try:
        assert process.stderr is not None
        while b"Uvicorn running" not in startup:
            ready, _, _ = select.select([process.stderr], [], [], 10)
            assert ready, "HTTP server did not start"
            chunk = os.read(process.stderr.fileno(), 4096)
            assert chunk, "HTTP server exited before startup"
            startup += chunk
        response = httpx.post(
            f"http://127.0.0.1:{port}/mcp",
            headers={"authorization": f"Bearer {secret}", "accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "create_memory", "arguments": {"domain": "notes", "title": "private-title", "content": "private-content"}},
            },
            timeout=10,
        )
        assert response.status_code == 200
        assert '"isError":true' not in response.text
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)
    output = (startup + stderr).decode()
    assert stdout == b""
    assert "crystalline.start operation=write" in output and "crystalline.complete operation=write" in output
    assert all(value not in output for value in (secret, "private-title", "private-content", NOISE))
