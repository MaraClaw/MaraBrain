"""Bound compressed strings and share provider work within one server lifespan."""

from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hashlib import sha256

import anyio
from anyio.abc import TaskGroup
from fastmcp.exceptions import ToolError

from marabrain.compression import compress_memory
from marabrain.llm_config import LLMSettings

type Compressor = Callable[[LLMSettings, str, str], Awaitable[str]]
type CacheKey = tuple[str, str, str, str, bytes]


@dataclass(slots=True)
class _DomainCache:
    """Maintain mutable LRU order and retained payload accounting for one workspace domain."""

    max_entries: int
    max_bytes: int
    entries: OrderedDict[CacheKey, str] = field(default_factory=OrderedDict[CacheKey, str])
    size: int = 0

    def store(self, key: CacheKey, value: str) -> None:
        """Retain fitting strings and evict the oldest entries until both limits hold."""
        size = len(value.encode("utf-8"))
        if size > self.max_bytes:
            return
        self.entries[key] = value
        self.size += size
        while len(self.entries) > self.max_entries or self.size > self.max_bytes:
            _, evicted = self.entries.popitem(last=False)
            self.size -= len(evicted.encode("utf-8"))


@dataclass(slots=True)
class _Flight:
    """Track mutable waiter ownership until one producer has completely retired."""

    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)
    done: anyio.Event = field(default_factory=anyio.Event)
    waiters: int = 0
    result: str | ToolError | None = None


class CompressionCache:
    """Own per-workspace-domain LRU state and producers under captured immutable LLM settings.

    All methods run on the lifespan's event loop. Bookkeeping is synchronous between
    await points, so joining, leaving, and marking retirement are atomic there.
    """

    def __init__(self, settings: LLMSettings, *, compressor: Compressor = compress_memory) -> None:
        """Capture settings and a provider adapter for this server instance."""
        self._settings = settings
        self._compressor = compressor
        self._domains: dict[tuple[str, str], _DomainCache] = {}
        self._flights: dict[CacheKey, _Flight] = {}
        self._tasks: TaskGroup | None = None

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[None]:
        """Own shared producers, drain them on shutdown, and discard retained data."""
        try:
            async with anyio.create_task_group() as tasks:
                self._tasks = tasks
                try:
                    yield
                finally:
                    self._tasks = None
                    tasks.cancel_scope.cancel()
        finally:
            self._domains.clear()
            self._flights.clear()

    async def get(self, domain: str, permalink: str, query: str, content: str, *, workspace_id: str) -> str:
        """Compress validated source identity, sharing privately within each workspace's resolved domain.

        The four inputs are the caller's already resolved identity and exact provider
        inputs, scoped by the required workspace ID even when caching is disabled.
        Callers must authorize and validate a fresh read before every lookup.
        """
        await anyio.lowlevel.checkpoint_if_cancelled()
        max_entries, max_bytes = self._settings.compression_cache_max_entries, self._settings.compression_cache_max_bytes
        if max_entries is None or max_bytes is None:
            return await self._compressor(self._settings, query, content)
        bucket = (workspace_id, domain)
        cache = self._domains.get(bucket)
        if cache is None:
            cache = self._domains[bucket] = _DomainCache(max_entries, max_bytes)
        key = (workspace_id, domain, permalink, query, sha256(content.encode("utf-8")).digest())
        while True:
            tasks = self._tasks
            if tasks is None:
                raise ToolError("LLM compression cache is not running.")
            cached = cache.entries.get(key)
            if cached is not None:
                cache.entries.move_to_end(key)
                return cached
            flight = self._flights.get(key)
            if flight is not None and flight.scope.cancel_called:
                await flight.done.wait()
                continue
            if flight is None:
                flight = _Flight()
                self._flights[key] = flight
                tasks.start_soon(self._produce, key, content, flight)
            flight.waiters += 1
            try:
                await flight.done.wait()
                match flight.result:
                    case str() as value:
                        return value
                    case ToolError() as error:
                        raise ToolError(str(error)) from None
                    case None:
                        raise ToolError("LLM compression failed.")
            finally:
                flight.waiters -= 1
                if flight.waiters == 0 and not flight.done.is_set():
                    flight.scope.cancel()
                    with anyio.CancelScope(shield=True):
                        await flight.done.wait()

    async def _produce(self, key: CacheKey, content: str, flight: _Flight) -> None:
        """Publish successful strings or expected errors only after provider cleanup."""
        try:
            with flight.scope:
                try:
                    value = await self._compressor(self._settings, key[3], content)
                except ToolError as error:
                    flight.result = error
                else:
                    if not flight.scope.cancel_called and self._tasks is not None:
                        self._domains[(key[0], key[1])].store(key, value)
                        flight.result = value
        finally:
            del self._flights[key]
            flight.done.set()
