"""Own one persistent isolated Crystalline runtime per exact workspace identity."""

import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import assert_never

import anyio
from anyio.abc import TaskGroup
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from marabrain.config import EngineMode, Settings
from marabrain.deadlines import deadline_after, deadline_scope
from marabrain.domain_admission import DomainCommandAdmission
from marabrain.domain_registry import DomainRegistry
from marabrain.engine_context import EngineContext
from marabrain.lifecycle import EngineLifecycleError, managed_engine, probe_engine, startup_remaining
from marabrain.transient import TransientLimitError
from marabrain.workspace_storage import prepare_workspace


@dataclass(frozen=True, slots=True)
class WorkspaceRuntime:
    """Expose only the ready workspace's bound execution, registry, and domain admission."""

    context: EngineContext
    registry: DomainRegistry
    admission: DomainCommandAdmission


@dataclass(slots=True)
class _Activation:
    """Mutate one generation's reservations and supervisor-owned startup and cleanup state."""

    ready: anyio.Event = field(default_factory=anyio.Event)
    done: anyio.Event = field(default_factory=anyio.Event)
    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)
    predecessor: anyio.Event | None = None
    runtime: WorkspaceRuntime | None = None
    error: ToolError | None = None
    users: int = 0
    last_used: int = 0


class WorkspaceManager:
    """Retain mutable workspace activation ownership for one application lifespan."""

    def __init__(self, settings: Settings, context: EngineContext) -> None:
        """Capture the storage root and policies without starting or adopting any engine."""
        self._settings = settings
        self._context = context
        self._root: Path = (settings.workspace_root if settings.workspace_root is not None else context.state_home / "marabrain" / "workspaces").absolute()
        self._tasks: TaskGroup | None = None
        self._activations: dict[str, _Activation] = {}
        self._pending: dict[str, _Activation] = {}
        self._recency = 0

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[None]:
        """Own activation supervisors and drain their engine cleanup before closing."""
        if self._tasks is not None:
            raise EngineLifecycleError("Workspace manager lifespan is already active.")
        try:
            async with anyio.create_task_group() as tasks:
                self._tasks = tasks
                try:
                    yield
                finally:
                    self._tasks = None
                    tasks.cancel_scope.cancel()
        finally:
            for activation in (*self._activations.values(), *self._pending.values()):
                activation.ready.set()
            self._activations.clear()
            self._pending.clear()

    async def get(self, workspace_id: str, *, deadline: float | None = None) -> WorkspaceRuntime:
        """Retain direct acquisition compatibility only when runtime retirement is disabled."""
        if self._settings.max_resident_workspaces is not None:
            raise ToolError("Workspace runtime requires a lease.")
        async with self.lease(workspace_id, deadline=deadline) as runtime:
            return runtime

    def _reserve(self, workspace_id: str) -> _Activation:
        """Atomically reserve use or one idle victim's slot without queuing for busy capacity."""
        tasks = self._tasks
        if tasks is None:
            raise ToolError("Workspace manager is not running.")
        activation = self._pending.get(workspace_id) or self._activations.get(workspace_id)
        if activation is None:
            activation = _Activation()
            limit = self._settings.max_resident_workspaces
            if limit is not None and len(self._activations) + len(self._pending) >= limit:
                victims = [row for row in self._activations.values() if row.runtime is not None and row.users == 0 and not row.scope.cancel_called]
                if not victims:
                    raise ToolError("Resident workspace capacity is busy.")
                victim = min(victims, key=lambda row: row.last_used)
                victim.scope.cancel()
                activation.predecessor = victim.done
                self._pending[workspace_id] = activation
            else:
                self._activations[workspace_id] = activation
            tasks.start_soon(self._activate, workspace_id, activation)
        if activation.scope.cancel_called:
            raise ToolError("Resident workspace capacity is busy.")
        activation.users += 1
        return activation

    @asynccontextmanager
    async def lease(self, workspace_id: str, *, deadline: float | None = None) -> AsyncGenerator[WorkspaceRuntime]:
        """Pin a generation before awaiting readiness through the caller's complete engine phase."""
        with deadline_scope(deadline):
            activation = self._reserve(workspace_id)
            used = False
            try:
                await activation.ready.wait()
                if activation.error is not None:
                    raise activation.error
                if activation.runtime is None or self._tasks is None:
                    raise ToolError("Workspace runtime is unavailable.")
                used = True
                yield activation.runtime
            finally:
                activation.users -= 1
                if used:
                    self._recency += 1
                    activation.last_used = self._recency

    def admit(self, names: list[str]) -> None:
        """Reject denied supplied domains before any workspace activation side effects."""
        policy = self._settings.allowed_domains
        if policy is not None and any(name not in policy for name in names):
            raise ToolError("Access to the requested domain is not permitted.")

    async def _activate(self, workspace_id: str, activation: _Activation) -> None:
        """Enter and exit an engine's scopes in this supervisor task for its entire lifetime."""
        try:
            # Enclose managed_engine's watcher group so planned retirement exits it before reaping.
            with activation.scope:
                if activation.predecessor is not None:
                    await activation.predecessor.wait()
                    del self._pending[workspace_id]
                    self._activations[workspace_id] = activation
                deadline = deadline_after(self._settings.startup_timeout_seconds)
                with anyio.fail_after(startup_remaining(deadline)) as startup:
                    remaining = startup_remaining(deadline)
                    storage_timeout = self._settings.command_timeout_seconds if remaining is None else min(remaining, self._settings.command_timeout_seconds)
                    await anyio.to_thread.run_sync(prepare_workspace, workspace_id, self._root, storage_timeout)
                    startup_remaining(deadline)
                    context = self._context.for_workspace(workspace_id, self._root)
                    async with self._engine(context, deadline):
                        policy = None if self._settings.allowed_domains is None else frozenset(self._settings.allowed_domains)
                        registry = DomainRegistry.from_environment(self._settings.command_timeout_seconds, policy, context=context)
                        await registry.synchronize(deadline=deadline)
                        startup_remaining(deadline)
                        # Keep the scope on its original stack while releasing its preparation-only deadline.
                        startup.deadline = float("inf")
                        activation.runtime = WorkspaceRuntime(context, registry, DomainCommandAdmission(self._settings.domain_command_capacity))
                        activation.ready.set()
                        await anyio.sleep_forever()
        except ExceptionGroup as errors:
            if activation.runtime is not None or errors.subgroup(EngineLifecycleError) is not None:
                raise
            activation.error = ToolError("Workspace runtime activation failed.")
        except ToolError as error:
            if activation.runtime is not None:
                raise
            activation.error = error
        except TransientLimitError as error:
            if activation.runtime is not None:
                raise
            activation.error = ToolError(str(error))
        except EngineLifecycleError, OSError, TimeoutError, ValidationError, subprocess.CalledProcessError:
            if activation.runtime is not None:
                raise
            activation.error = ToolError("Workspace runtime activation failed.")
        finally:
            for entries in (self._activations, self._pending):
                if entries.get(workspace_id) is activation:
                    del entries[workspace_id]
            activation.ready.set()
            activation.done.set()

    @asynccontextmanager
    async def _engine(self, context: EngineContext, deadline: float | None) -> AsyncGenerator[None]:
        """Require a workspace daemon externally or own its managed lifecycle in the caller task."""
        mode = self._settings.engine_mode
        match mode:
            case EngineMode.EXTERNAL:
                if not await probe_engine(self._settings.health_timeout_seconds, context=context, deadline=deadline):
                    raise ToolError("Workspace Crystalline daemon is not responding.")
                yield
                return
            case EngineMode.MANAGED:
                async with managed_engine(self._settings, context=context, deadline=deadline):
                    yield
                return
        assert_never(mode)

    async def health(self) -> bool:
        """Probe activated workspace engines under one total health deadline without using a default daemon."""
        if self._tasks is None:
            return False
        deadline = anyio.current_time() + self._settings.health_timeout_seconds
        snapshot = tuple(row for row in self._activations.values() if not row.scope.cancel_called)
        for activation in snapshot:
            activation.users += 1
        try:
            with anyio.fail_after(self._settings.health_timeout_seconds):
                for activation in snapshot:
                    await activation.ready.wait()
                    if activation.error is not None or activation.runtime is None:
                        return False
                    if not await probe_engine(self._settings.health_timeout_seconds, context=activation.runtime.context, deadline=deadline):
                        return False
                return True
        except TimeoutError, TransientLimitError:
            return False
        finally:
            for activation in snapshot:
                activation.users -= 1
