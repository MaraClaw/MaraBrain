"""Deterministic producer ownership, retirement, failure, and shutdown contracts."""

from dataclasses import dataclass, field
from functools import partial

import anyio
import pytest
from anyio.abc import TaskStatus
from fastmcp.exceptions import ToolError

from marabrain.compression_cache import CompressionCache
from marabrain.llm_config import LLMSettings


@dataclass(slots=True)
class Provider:
    """Control mutable provider work and shielded cleanup through explicit barriers."""

    started: anyio.Event = field(default_factory=anyio.Event)
    release: anyio.Event = field(default_factory=anyio.Event)
    cleanup_started: anyio.Event = field(default_factory=anyio.Event)
    cleanup_release: anyio.Event = field(default_factory=anyio.Event)
    calls: int = 0
    cleaned: int = 0
    hold_cleanup: bool = False
    fail: bool = False

    async def __call__(self, settings: LLMSettings, query: str, content: str) -> str:
        """Expose awaited work and cleanup without providing cache behavior in the fake."""
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
            if self.fail and query == "bad":
                raise ToolError("LLM compression failed.")
            return f"context {self.calls}"
        finally:
            with anyio.CancelScope(shield=True):
                self.cleanup_started.set()
                if self.hold_cleanup:
                    await self.cleanup_release.wait()
                self.cleaned += 1


def cache_for(provider: Provider, enabled: bool = True) -> CompressionCache:
    """Build an isolated application with optional uniform cache limits."""
    settings = LLMSettings(compression_cache_max_entries=3 if enabled else None, compression_cache_max_bytes=100 if enabled else None)
    return CompressionCache(settings, compressor=provider)


@pytest.mark.parametrize("cancel_index", [None, 0, 1])
@pytest.mark.parametrize("domain", ["notes", "Exact Name"])
async def test_identical_waiters_share_success_despite_one_cancellation(cancel_index: int | None, domain: str) -> None:
    """Preserve the shared producer when either initiator or follower leaves."""
    provider = Provider()
    cache = cache_for(provider)
    results: list[str] = []

    async def read(*, task_status: TaskStatus[anyio.CancelScope]) -> None:
        """Return a cancellable waiter whose result is recorded only on success."""
        with anyio.CancelScope() as scope:
            task_status.started(scope)
            results.append(await cache.get(domain, "p", "q", "content", workspace_id="test-workspace"))

    with anyio.fail_after(5):
        async with cache.lifespan(), anyio.create_task_group() as waiters:
            scopes = [await waiters.start(read), await waiters.start(read)]
            await anyio.wait_all_tasks_blocked()
            if cancel_index is not None:
                scopes[cancel_index].cancel()
                await anyio.wait_all_tasks_blocked()
            assert provider.calls == 1 and provider.cleaned == 0
            provider.release.set()
        assert results == ["context 1"] * (2 if cancel_index is None else 1)
    assert provider.cleaned == 1


async def test_disabled_cache_neither_reuses_nor_coalesces() -> None:
    """Retain independent provider calls when both scalar limits are disabled."""
    provider = Provider()
    cache = cache_for(provider, enabled=False)

    with anyio.fail_after(5):
        async with cache.lifespan(), anyio.create_task_group() as waiters:
            waiters.start_soon(partial(cache.get, workspace_id="test-workspace"), "notes", "p", "q", "content")
            waiters.start_soon(partial(cache.get, workspace_id="test-workspace"), "notes", "p", "q", "content")
            await anyio.wait_all_tasks_blocked()
            assert provider.calls == 2
            provider.release.set()
        await cache.get("notes", "p", "q", "content", workspace_id="test-workspace")
    assert provider.calls == provider.cleaned == 3


async def test_last_waiter_drains_retirement_before_same_key_replacement() -> None:
    """Keep cancelled work indexed until cleanup completes before starting replacement work."""
    provider = Provider(hold_cleanup=True)
    cache = cache_for(provider)
    stopped = anyio.Event()
    results: list[str] = []

    async def first(*, task_status: TaskStatus[anyio.CancelScope]) -> None:
        """Signal caller completion only after its cancelled producer has drained."""
        with anyio.CancelScope() as scope:
            task_status.started(scope)
            await cache.get("notes", "p", "q", "content", workspace_id="test-workspace")
        stopped.set()

    async def replacement() -> None:
        """Request the same key while the original provider is retiring."""
        results.append(await cache.get("notes", "p", "q", "content", workspace_id="test-workspace"))

    with anyio.fail_after(5):
        async with cache.lifespan(), anyio.create_task_group() as waiters:
            try:
                scope = await waiters.start(first)
                await provider.started.wait()
                scope.cancel()
                await provider.cleanup_started.wait()
                waiters.start_soon(replacement)
                await anyio.wait_all_tasks_blocked()
                assert provider.calls == 1 and not stopped.is_set() and not results
                provider.cleanup_release.set()
                await stopped.wait()
                await anyio.wait_all_tasks_blocked()
                assert provider.calls == 2 and provider.cleaned == 1
                provider.release.set()
            finally:
                provider.cleanup_release.set()
                provider.release.set()
        assert results == ["context 2"]
        async with cache.lifespan():
            assert await cache.get("notes", "p", "q", "content", workspace_id="test-workspace") == "context 3"
    assert provider.calls == provider.cleaned == 3


async def test_expected_error_fans_out_without_cancelling_unrelated_work() -> None:
    """Deliver shared errors to matching waiters and allow an uncached recovery attempt."""
    provider = Provider(fail=True)
    cache = cache_for(provider)
    errors: list[str] = []
    results: list[str] = []

    async def read(query: str) -> None:
        """Record expected failures at the waiter boundary."""
        try:
            results.append(await cache.get("notes", "p", query, "content", workspace_id="test-workspace"))
        except ToolError as error:
            errors.append(str(error))

    with anyio.fail_after(5):
        async with cache.lifespan():
            async with anyio.create_task_group() as waiters:
                for query in ("bad", "bad", "good"):
                    waiters.start_soon(read, query)
                await anyio.wait_all_tasks_blocked()
                assert provider.calls == 2
                provider.release.set()
            assert errors == ["LLM compression failed."] * 2 and len(results) == 1
            provider.fail = False
            assert await cache.get("notes", "p", "bad", "content", workspace_id="test-workspace") == "context 3"
    assert provider.calls == provider.cleaned == 3


@pytest.mark.parametrize("cancel_lifespan", [False, True])
async def test_shutdown_drains_producer_cleanup_and_wakes_waiters(cancel_lifespan: bool) -> None:
    """Retire owned providers fully on normal shutdown and outer lifespan cancellation."""
    provider = Provider(hold_cleanup=True)
    cache = cache_for(provider)
    stopped = anyio.Event()
    errors: list[str] = []
    shutdown = anyio.Event()

    async def serve(*, task_status: TaskStatus[anyio.CancelScope]) -> None:
        """Own the cache lifespan independently of outstanding request tasks."""
        with anyio.CancelScope() as scope:
            async with cache.lifespan():
                task_status.started(scope)
                await shutdown.wait()
        stopped.set()

    async def read() -> None:
        """Observe the failure of a request whose server is shutting down."""
        try:
            await cache.get("notes", "p", "q", "content", workspace_id="test-workspace")
        except ToolError as error:
            errors.append(str(error))

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks:
            try:
                scope = await tasks.start(serve)
                tasks.start_soon(read)
                await provider.started.wait()
                if cancel_lifespan:
                    scope.cancel()
                else:
                    shutdown.set()
                await provider.cleanup_started.wait()
                await anyio.wait_all_tasks_blocked()
                assert not stopped.is_set() and provider.cleaned == 0
            finally:
                provider.cleanup_release.set()
                shutdown.set()
        assert stopped.is_set() and provider.calls == provider.cleaned == 1
        assert errors == ["LLM compression failed."]


async def test_purge_lets_a_running_flight_answer_every_waiter_without_storing() -> None:
    """Deliver a purged flight's result to all waiters, retain nothing, and store again for flights started afterwards."""
    provider = Provider()
    cache = cache_for(provider)
    results: list[str] = []

    async def read() -> None:
        """Record the result one waiter receives."""
        results.append(await cache.get("notes", "p", "q", "content", workspace_id="test-workspace"))

    with anyio.fail_after(5):
        async with cache.lifespan():
            async with anyio.create_task_group() as waiters:
                waiters.start_soon(read)
                waiters.start_soon(read)
                await anyio.wait_all_tasks_blocked()
                assert provider.calls == 1
                cache.purge("notes", "p", workspace_id="test-workspace")
                provider.release.set()
            assert results == ["context 1"] * 2
            assert cache._domains == {} and cache._recency == {} and cache._total == 0
            assert await cache.get("notes", "p", "q", "content", workspace_id="test-workspace") == "context 2"
            assert await cache.get("notes", "p", "q", "content", workspace_id="test-workspace") == "context 2"
    assert provider.calls == provider.cleaned == 2


async def test_purge_spares_running_flights_of_other_memories() -> None:
    """Keep storing flights for other permalinks, resolved domains, and workspaces while the purged memory's flight stores nothing."""
    provider = Provider()
    cache = cache_for(provider)
    memories = [
        ("test-workspace", "notes", "p"),
        ("test-workspace", "notes", "other"),
        ("test-workspace", "Exact Name", "p"),
        ("other-workspace", "notes", "p"),
    ]

    async def read(memory: tuple[str, str, str]) -> None:
        """Request one memory's compression with identical query and source text."""
        workspace, domain, permalink = memory
        await cache.get(domain, permalink, "q", "content", workspace_id=workspace)

    with anyio.fail_after(5):
        async with cache.lifespan():
            async with anyio.create_task_group() as readers:
                for memory in memories:
                    readers.start_soon(read, memory)
                await anyio.wait_all_tasks_blocked()
                assert provider.calls == 4
                cache.purge("notes", "p", workspace_id="test-workspace")
                provider.release.set()
            assert provider.calls == 4
            for memory in memories[1:]:
                await read(memory)
            assert provider.calls == 4
            await read(memories[0])
            assert provider.calls == 5
    assert provider.cleaned == 5
