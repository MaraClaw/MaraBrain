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

    entries: OrderedDict[CacheKey, str] = field(default_factory=OrderedDict[CacheKey, str])
    size: int = 0


@dataclass(slots=True)
class _Flight:
    """Track mutable waiter ownership until one producer has completely retired."""

    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)
    done: anyio.Event = field(default_factory=anyio.Event)
    waiters: int = 0
    result: str | ToolError | None = None
    purged: bool = False


class CompressionCache:
    """Own per-workspace-domain LRU state, one application-wide recency order, and producers under captured immutable LLM settings.

    All methods run on the lifespan's event loop. Bookkeeping, including purging, is
    synchronous between await points, so joining, leaving, storing, evicting, and
    marking retirement are atomic there.
    """

    def __init__(self, settings: LLMSettings, *, compressor: Compressor = compress_memory) -> None:
        """Capture settings and a provider adapter for this server instance."""
        self._settings = settings
        self._compressor = compressor
        self._domains: dict[tuple[str, str], _DomainCache] = {}
        self._recency: OrderedDict[CacheKey, None] = OrderedDict()
        self._total = 0
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
            self._recency.clear()
            self._total = 0
            self._flights.clear()

    async def get(self, domain: str, permalink: str, query: str, content: str, *, workspace_id: str) -> str:
        """Compress validated source identity, sharing privately within each workspace's resolved domain.

        The four inputs are the caller's already resolved identity and exact provider
        inputs, scoped by the required workspace ID even when caching is disabled.
        Callers must authorize and validate a fresh read before every lookup.
        """
        await anyio.lowlevel.checkpoint_if_cancelled()
        if self._settings.compression_cache_max_entries is None or self._settings.compression_cache_max_bytes is None:
            return await self._compressor(self._settings, query, content)
        bucket_id = (workspace_id, domain)
        key = (workspace_id, domain, permalink, query, sha256(content.encode("utf-8")).digest())
        while True:
            tasks = self._tasks
            if tasks is None:
                raise ToolError("LLM compression cache is not running.")
            bucket = self._domains.get(bucket_id)
            if bucket is not None and (cached := bucket.entries.get(key)) is not None:
                bucket.entries.move_to_end(key)
                self._recency.move_to_end(key)
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

    def purge(self, domain: str, permalink: str, *, workspace_id: str) -> None:
        """Drop every retained entry for one memory under any query and stop its running flights from storing.

        Flights already running still answer their waiters. Other permalinks, resolved
        domains, and workspaces are untouched. The cost is linear in the memory's
        domain bucket and the running flights, and the call never yields.
        """
        for key, flight in self._flights.items():
            if key[:3] == (workspace_id, domain, permalink):
                flight.purged = True
        bucket = self._domains.get((workspace_id, domain))
        if bucket is not None:
            for key in [entry for entry in bucket.entries if entry[2] == permalink]:
                self._discard(key)

    def _store(self, key: CacheKey, value: str) -> None:
        """Retain a fitting string, then evict least recently used entries until every limit holds."""
        max_entries, max_bytes = self._settings.compression_cache_max_entries, self._settings.compression_cache_max_bytes
        assert max_entries is not None and max_bytes is not None
        max_total = self._settings.compression_cache_total_max_bytes
        size = len(value.encode("utf-8"))
        if size > max_bytes or (max_total is not None and size > max_total):
            return
        bucket = self._domains.setdefault((key[0], key[1]), _DomainCache())
        bucket.entries[key] = value
        bucket.size += size
        self._recency[key] = None
        self._total += size
        while len(bucket.entries) > max_entries or bucket.size > max_bytes:
            self._discard(next(iter(bucket.entries)))
        while max_total is not None and self._total > max_total:
            self._discard(next(iter(self._recency)))

    def _discard(self, key: CacheKey) -> None:
        """Remove one retained entry from its bucket and the global recency order, dropping the bucket once empty."""
        bucket_id = (key[0], key[1])
        bucket = self._domains[bucket_id]
        size = len(bucket.entries.pop(key).encode("utf-8"))
        bucket.size -= size
        self._total -= size
        del self._recency[key]
        if not bucket.entries:
            del self._domains[bucket_id]

    async def _produce(self, key: CacheKey, content: str, flight: _Flight) -> None:
        """Publish successful strings or expected errors only after provider cleanup, storing unless the flight was purged."""
        try:
            with flight.scope:
                try:
                    value = await self._compressor(self._settings, key[3], content)
                except ToolError as error:
                    flight.result = error
                else:
                    if not flight.scope.cancel_called and self._tasks is not None:
                        if not flight.purged:
                            self._store(key, value)
                        flight.result = value
        finally:
            del self._flights[key]
            flight.done.set()
