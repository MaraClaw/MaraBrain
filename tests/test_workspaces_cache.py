"""Verify compression reuse never crosses a workspace boundary."""

import anyio
import pytest

from marabrain.compression_cache import CompressionCache
from marabrain.llm_config import LLMSettings


@pytest.mark.asyncio
async def test_identical_tenant_inputs_have_independent_flights_and_lru_budgets() -> None:
    """Coalesce within one workspace while independently compressing and retaining another's inputs."""
    calls: list[str] = []
    release = anyio.Event()
    both_started = anyio.Event()

    async def compress(settings: LLMSettings, query: str, content: str) -> str:
        """Hold independent provider calls until both workspace producers have started."""
        calls.append(content)
        if len(calls) == 2:
            both_started.set()
        await release.wait()
        return f"compressed {content}"

    cache = CompressionCache(LLMSettings(compression_cache_max_entries=1, compression_cache_max_bytes=128), compressor=compress)
    results: list[str] = []

    async def read(workspace_id: str) -> None:
        """Read identical source inputs scoped to an explicit workspace."""
        results.append(await cache.get("notes", "shared", "query", "same", workspace_id=workspace_id))

    async with cache.lifespan():
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(read, "alpha")
            tasks.start_soon(read, "alpha")
            tasks.start_soon(read, "beta")
            with anyio.fail_after(2):
                await both_started.wait()
            release.set()
        assert calls == ["same", "same"]
        assert results == ["compressed same"] * 3
        await cache.get("notes", "other", "query", "other", workspace_id="alpha")
        await cache.get("notes", "shared", "query", "same", workspace_id="beta")
        assert calls == ["same", "same", "other"]
