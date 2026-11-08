"""Synchronize Crystalline domains and register missing file domains persistently."""

import re
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
import anyio
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, StrictBool, TypeAdapter, ValidationError

from marabrain.crystalline import run_json
from marabrain.deadlines import check_deadline, deadline_scope
from marabrain.domain_storage import DomainStorage
from marabrain.engine_context import EngineContext
from marabrain.models import Domain


class ListedDomain(BaseModel):
    """Validated identity from the pinned CLI, retaining its exact identifier."""

    model_config = ConfigDict(frozen=True)
    name: Domain
    canonical_name: Domain | None = None
    aliases: tuple[Domain, ...] = ()
    shadowed: StrictBool = False


class DomainSnapshot(BaseModel):
    """Complete domain-list response required before replacing the local snapshot."""

    model_config = ConfigDict(frozen=True)
    domains: tuple[ListedDomain, ...]


def new_name(name: str) -> str:
    """Lowercase ASCII domain names without rewriting punctuation or path characters."""
    normalized = name.lower()
    if not name.isascii() or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", normalized) is None or ".." in normalized:
        raise ToolError("A new domain requires a safe ASCII name of at most 64 characters.")
    return normalized


@dataclass(frozen=True, slots=True)
class DomainRegistry:
    """Persistent exact identifiers with atomic snapshots and serialized registration."""

    storage: DomainStorage
    policy: frozenset[str] | None = None
    context: EngineContext = field(default_factory=EngineContext.capture)

    @classmethod
    def from_environment(cls, timeout: float, policy: frozenset[str] | None = None, *, context: EngineContext | None = None) -> DomainRegistry:
        """Capture storage and authorization settings for one server instance."""
        captured = context if context is not None else EngineContext.capture()
        return cls(DomainStorage.from_environment(timeout, context=captured), policy, captured)

    def admit(self, names: list[str]) -> None:
        """Retain exact-name policy checks before any registration or memory mutation."""
        if self.policy is not None and any(name not in self.policy for name in names):
            raise ToolError("Access to the requested domain is not permitted.")

    async def _prepare(self, database: aiosqlite.Connection) -> None:
        """Create the registry schema and refuse reuse with another engine configuration."""
        await database.execute("CREATE TABLE IF NOT EXISTS domains (name TEXT PRIMARY KEY, normalized TEXT NOT NULL UNIQUE)")
        await database.execute("CREATE TABLE IF NOT EXISTS aliases (name TEXT PRIMARY KEY, target TEXT NOT NULL)")
        await database.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        config = self.context.config_identity
        await database.execute("INSERT OR IGNORE INTO metadata VALUES ('config', ?)", (config,))
        async with database.execute("SELECT value FROM metadata WHERE key = 'config'") as cursor:
            row = await cursor.fetchone()
        if row != (config,):
            legacy = str(Path(self.context.environment["XDG_CONFIG_HOME"]) / "crystalline")
            if row != (legacy,) or any(name.startswith("CRYSTALLINE_") and value for name, value in self.context.environment.items()):
                raise ToolError("Domain registry belongs to another Crystalline configuration.")
            await database.execute("DELETE FROM metadata WHERE key = 'synchronized'")
            await database.execute("UPDATE metadata SET value = ? WHERE key = 'config'", (config,))
        await database.commit()

    async def _snapshot(self, database: aiosqlite.Connection, deadline: float | None = None) -> dict[str, str]:
        """Validate a complete engine response before atomically replacing domain rows."""
        result = await run_json(["domain", "list"], timeout_seconds=self.storage.timeout, context=self.context, deadline=deadline)
        try:
            snapshot = DomainSnapshot.model_validate(result)
        except ValidationError:
            raise ToolError("Crystalline returned an invalid domain list.") from None
        names = [domain.name for domain in snapshot.domains]
        if len({name.lower() for name in names}) != len(names):
            raise ToolError("Crystalline domain names have an ambiguous normalization collision.")
        routes = {name.lower(): name for name in names}
        aliases: dict[str, str] = {}
        for domain in snapshot.domains:
            spellings = list(domain.aliases)
            if domain.canonical_name is not None and not domain.shadowed and domain.canonical_name not in names:
                spellings.append(domain.canonical_name)
            for spelling in spellings:
                normalized = spelling.lower()
                if normalized in routes and routes[normalized] != domain.name:
                    raise ToolError("Crystalline domain names have an ambiguous normalization collision.")
                routes[normalized] = domain.name
                aliases[spelling] = domain.name
        # No transaction is open during the engine command above.
        await database.execute("BEGIN IMMEDIATE")
        await database.execute("DELETE FROM domains")
        await database.execute("DELETE FROM aliases")
        await database.executemany("INSERT INTO domains VALUES (?, ?)", [(name, name.lower()) for name in names])
        await database.executemany("INSERT INTO aliases VALUES (?, ?)", aliases.items())
        await database.execute("INSERT OR REPLACE INTO metadata VALUES ('synchronized', 'yes')")
        await database.commit()
        return routes

    async def synchronize(self, *, deadline: float | None = None) -> None:
        """Fail startup if the complete Crystalline snapshot cannot be persisted safely."""
        try:
            with deadline_scope(deadline):
                async with self.storage.locked() as path:
                    async with self._database(path) as database:
                        await self._prepare(database)
                        await self._snapshot(database, deadline)
        except sqlite3.Error:
            raise ToolError("Domain registry storage failed.") from None

    @asynccontextmanager
    async def _database(self, path: Path) -> AsyncGenerator[aiosqlite.Connection]:
        """Close the SQLite worker even when an MCP request or startup is cancelled."""
        connection: sqlite3.Connection | None = None

        def connect() -> sqlite3.Connection:
            """Retain the raw handle if cancellation prevents aiosqlite from claiming it."""
            nonlocal connection
            connection = sqlite3.connect(path, check_same_thread=False)
            return connection

        database = aiosqlite.Connection(connect, iter_chunk_size=64)
        connected = False
        try:
            await database
            connected = True
            yield database
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await database.close()
                finally:
                    # Cancelled acquisition stops the worker without awaiting or closing its connector result.
                    await anyio.to_thread.run_sync(database._thread.join)
                    if not connected and connection is not None:
                        connection.close()

    def _resolve(self, requested: str, routes: dict[str, str]) -> str:
        """Retain exact existing names and resolve case variants without changing identifiers."""
        existing = routes.get(requested.lower())
        return existing if existing is not None else new_name(requested)

    async def resolve(self, requested: list[str], *, create: bool = False, deadline: float | None = None) -> list[str]:
        """Resolve authorized domains consistently, registering only for memory creation."""
        self.admit(requested)
        try:
            with deadline_scope(deadline):
                async with self.storage.locked() as path:
                    async with self._database(path) as database:
                        await self._prepare(database)
                        async with database.execute("SELECT value FROM metadata WHERE key = 'synchronized'") as cursor:
                            synchronized = await cursor.fetchone()
                        if synchronized is None:
                            routes = await self._snapshot(database, deadline)
                        else:
                            async with database.execute("SELECT name, name FROM domains UNION ALL SELECT name, target FROM aliases") as cursor:
                                entries = TypeAdapter(list[tuple[str, str]]).validate_python(await cursor.fetchall())
                                routes = {name.lower(): target for name, target in entries}
                        resolved = [self._resolve(name, routes) for name in requested]
                        self.admit(resolved)
                        if create and resolved[0] not in routes.values():
                            # Reconcile a previous crash, cancelled add, or another process before mutating the engine.
                            routes = await self._snapshot(database, deadline)
                            resolved = [self._resolve(name, routes) for name in requested]
                            self.admit(resolved)
                            if resolved[0] not in routes.values():
                                await self._register(database, resolved[0], deadline)
                        return resolved
        except sqlite3.Error:
            raise ToolError("Domain registry storage failed.") from None

    async def _register(self, database: aiosqlite.Connection, name: str, deadline: float | None = None) -> None:
        """Initialize and register a safe file domain before recording a successful receipt."""
        path = self.storage.domain_path(new_name(name))
        await database.execute("DELETE FROM metadata WHERE key = 'synchronized'")
        await database.commit()
        try:
            await run_json(["domain", "init", str(path), "--name", name], timeout_seconds=self.storage.timeout, context=self.context, deadline=deadline)
            receipt = await run_json(
                ["domain", "add", name, "--path", str(path)], timeout_seconds=self.storage.timeout, context=self.context, deadline=deadline
            )
            if receipt.get("registered") != name or receipt.get("path") != str(path):
                raise ToolError("Crystalline returned an invalid domain registration receipt.")
        except ToolError:
            # An add can commit configuration before failing to index or losing its response.
            # A valid listing reconciles that outcome; the failed request never writes a memory.
            check_deadline(deadline)
            await self._snapshot(database, deadline)
            raise
        await database.execute("INSERT INTO domains VALUES (?, ?)", (name, name.lower()))
        await database.execute("INSERT OR REPLACE INTO metadata VALUES ('synchronized', 'yes')")
        await database.commit()
