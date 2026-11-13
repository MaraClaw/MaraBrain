"""Exercise the pinned Crystalline release through a credential-free MCP subprocess."""

import os
import shutil
import sys
from pathlib import Path
from subprocess import CompletedProcess
from typing import Literal

import anyio
import pytest
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import StdioTransport
from mcp.types import TextContent
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from marabrain.engine_context import EngineContext
from marabrain.workspace_storage import prepare_workspace


class EmbeddingsStatus(BaseModel):
    """Parse the native readiness fields needed to rule out text-only search."""

    model_config = ConfigDict(frozen=True)

    active_model: Literal["granite-embedding-97m-multilingual-r2"]
    provider: bool
    hybrid_available: bool
    embedded_chunks: int
    total_chunks: int


class EngineStatus(BaseModel):
    """Parse the isolated daemon's identity and embedding readiness."""

    model_config = ConfigDict(frozen=True)

    version: Literal["0.32.1"]
    pid: int
    http: None
    embeddings: EmbeddingsStatus


async def run_cli(arguments: list[str], environment: dict[str, str], timeout: float = 15) -> CompletedProcess[bytes]:
    """Run an isolated native command within a deadline and expose its failure output."""
    with anyio.fail_after(timeout):
        process = await anyio.run_process(["crystalline", *arguments], env=environment, check=False)
    assert process.returncode == 0, process.stderr.decode()
    return process


def payload(result: CallToolResult) -> dict[str, JsonValue]:
    """Require successful native JSON from an MCP tool response."""
    assert not result.is_error, result.content
    return TypeAdapter(dict[str, JsonValue]).validate_python(result.structured_content)


@pytest.mark.integration
async def test_real_engine_memory_lifecycle(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Round-trip a memory through real MCP tools and local embedding search, then stop the daemon."""
    executable = shutil.which("crystalline")
    assert executable is not None, "Install Crystalline 0.32.1 on PATH before running tests/integration."
    root = tmp_path_factory.getbasetemp().resolve()
    environment = {
        "PATH": os.pathsep.join((str(Path(executable).parent), os.defpath)),
        "HOME": str(root / "h"),
        "XDG_CONFIG_HOME": str(root / "c"),
        "XDG_STATE_HOME": str(root),
        "XDG_CACHE_HOME": str(root / "k"),
        "XDG_DATA_HOME": str(root / "d"),
        "XDG_RUNTIME_DIR": str(root / "r"),
        "TMPDIR": str(root / "t"),
        "MARABRAIN_TRANSPORT": "stdio",
        "MARABRAIN_ENGINE_MODE": "managed",
        "MARABRAIN_ENGINE_STARTUP_TIMEOUT_SECONDS": "120",
        "MARABRAIN_STARTUP_TIMEOUT_SECONDS": "120",
        "MARABRAIN_WORKSPACE_ROOT": str(root / "w"),
        "MARABRAIN_MAX_RESIDENT_WORKSPACES": "1",
        "MARABRAIN_MAX_TOTAL_CAPTURE_BYTES": "1048576",
    }
    for directory in ("h", "c", "k", "d", "r", "t"):
        (root / directory).mkdir(mode=0o700)

    with anyio.fail_after(360):
        version = await run_cli(["--version"], environment)
        assert version.stdout.decode().splitlines()[0] == "crystalline 0.32.1"
        await run_cli(["--json", "model", "download"], environment, timeout=240)
        workspace_id = "integration-primary"
        contexts: dict[str, EngineContext] = {}
        for identity in (workspace_id, "integration-pressure"):
            prepare_workspace(identity, root / "w")
            context = EngineContext(environment, root, "integration-model-download").for_workspace(identity, root / "w")
            shutil.copytree(root / "k", context.environment["XDG_CACHE_HOME"], dirs_exist_ok=True)
            contexts[identity] = context
        workspace_environment = dict(contexts[workspace_id].environment)

        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "marabrain"],
            cwd=str(Path(__file__).resolve().parents[2]),
            env=environment,
            keep_alive=False,
            log_file=root / "server.log",
        )
        async with Client(transport, timeout=30, init_timeout=120) as client:
            health = payload(await client.call_tool("health"))
            assert health == {"status": "ok", "version": "0.4.7"}

            target = {"domain": "integration", "permalink": "lunar-greenhouse"}
            request_target = {"workspace_id": workspace_id, **target}
            content = "The lunar greenhouse grows tomatoes using hydroponic nutrient solution."
            created = payload(
                await client.call_tool(
                    "create_memory",
                    {"workspace_id": workspace_id, "domain": "integration", "title": "Lunar greenhouse", "content": content, "tags": ["integration"]},
                )
            )
            assert created == {
                **target,
                "action": "created",
                "path": "lunar-greenhouse.md",
                "status": "stable",
                "title": "Lunar greenhouse",
                "type": "engram",
            }
            original = payload(await client.call_tool("read_memory", request_target))
            original_content = original["content"]
            assert isinstance(original_content, str)
            assert content in original_content
            assert original["url"] == "crystalline://integration/lunar-greenhouse"

            addition = "Harvest the ripe tomatoes on Friday."
            edited = payload(await client.call_tool("edit_memory", {**request_target, "operation": {"type": "append", "content": addition}}))
            assert edited == {**target, "operation": "append", "path": "lunar-greenhouse.md"}
            updated = payload(await client.call_tool("read_memory", request_target))
            updated_content = updated["content"]
            assert isinstance(updated_content, str)
            assert content in updated_content and addition in updated_content
            assert updated["checksum"] != original["checksum"]

            with anyio.fail_after(120):
                while True:
                    status = EngineStatus.model_validate_json((await run_cli(["--json", "ctl", "status"], workspace_environment)).stdout)
                    if status.embeddings.provider:
                        break
                    await anyio.sleep(0.2)
                await run_cli(["--json", "ctl", "sync", "--domain", "integration", "--embed"], workspace_environment, timeout=30)
                while True:
                    status = EngineStatus.model_validate_json((await run_cli(["--json", "ctl", "status"], workspace_environment)).stdout)
                    if status.embeddings.hybrid_available and status.embeddings.embedded_chunks == status.embeddings.total_chunks > 0:
                        break
                    await anyio.sleep(0.2)

            found = payload(
                await client.call_tool("search_memories", {"workspace_id": workspace_id, "query": "hydroponic tomatoes", "domains": ["integration"]})
            )
            assert found["mode"] == "hybrid", found
            assert found["count"] == 1
            hits = found["hits"]
            assert isinstance(hits, list) and len(hits) == 1
            hit = hits[0]
            assert isinstance(hit, dict)
            assert hit["domain"] == target["domain"] and hit["permalink"] == target["permalink"]

            await client.call_tool(
                "create_memory",
                {"workspace_id": "integration-pressure", "domain": "integration", "title": "Pressure", "content": "Retire the idle primary daemon."},
            )
            with pytest.raises(ProcessLookupError):
                os.kill(status.pid, 0)
            pressure_status = EngineStatus.model_validate_json(
                (await run_cli(["--json", "ctl", "status"], dict(contexts["integration-pressure"].environment))).stdout
            )
            restored = payload(await client.call_tool("read_memory", request_target))
            assert restored["content"] == updated["content"]
            assert restored["checksum"] == updated["checksum"]
            with pytest.raises(ProcessLookupError):
                os.kill(pressure_status.pid, 0)
            previous_pid = status.pid
            status = EngineStatus.model_validate_json((await run_cli(["--json", "ctl", "status"], workspace_environment)).stdout)
            assert status.pid != previous_pid

            deleted = payload(await client.call_tool("delete_memory", request_target))
            assert deleted == {**target, "deleted": True, "path": "lunar-greenhouse.md"}
            missing = await client.call_tool("read_memory", request_target, raise_on_error=False)
            assert missing.is_error
            assert missing.structured_content is None
            assert len(missing.content) == 1
            error = missing.content[0]
            assert isinstance(error, TextContent)
            assert error.text == "Crystalline command failed with exit code 1."

        with pytest.raises(ProcessLookupError):
            os.kill(status.pid, 0)
