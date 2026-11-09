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
    """Mutate one app-owned activation result and wake every independent request waiter."""

    ready: anyio.Event = field(default_factory=anyio.Event)
    runtime: WorkspaceRuntime | None = None
    error: ToolError | None = None


class WorkspaceManager:
    """Retain mutable workspace activation ownership for one application lifespan."""

    def __init__(self, settings: Settings, context: EngineContext) -> None:
        """Capture the storage root and policies without starting or adopting any engine."""
        self._settings = settings
        self._context = context
        self._root: Path = (settings.workspace_root if settings.workspace_root is not None else context.state_home / "marabrain" / "workspaces").absolute()
        self._tasks: TaskGroup | None = None
        self._activations: dict[str, _Activation] = {}

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
            for activation in self._activations.values():
                activation.ready.set()
            self._activations.clear()

    async def get(self, workspace_id: str, *, deadline: float | None = None) -> WorkspaceRuntime:
        """Join one activation without assigning its ownership or startup budget to the waiter."""
        with deadline_scope(deadline):
            tasks = self._tasks
            if tasks is None:
                raise ToolError("Workspace manager is not running.")
            activation = self._activations.get(workspace_id)
            if activation is None or activation.error is not None:
                activation = _Activation()
                self._activations[workspace_id] = activation
                tasks.start_soon(self._activate, workspace_id, activation)
            await activation.ready.wait()
            if activation.error is not None:
                raise activation.error
            if activation.runtime is None or self._tasks is None:
                raise ToolError("Workspace runtime is unavailable.")
            return activation.runtime

    def admit(self, names: list[str]) -> None:
        """Reject denied supplied domains before any workspace activation side effects."""
        policy = self._settings.allowed_domains
        if policy is not None and any(name not in policy for name in names):
            raise ToolError("Access to the requested domain is not permitted.")

    async def _activate(self, workspace_id: str, activation: _Activation) -> None:
        """Enter and exit an engine's scopes in this supervisor task for its entire lifetime."""
        deadline = deadline_after(self._settings.startup_timeout_seconds)
        try:
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
            activation.ready.set()

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
        try:
            with anyio.fail_after(self._settings.health_timeout_seconds):
                for activation in tuple(self._activations.values()):
                    await activation.ready.wait()
                    if activation.error is not None or activation.runtime is None:
                        return False
                    if not await probe_engine(self._settings.health_timeout_seconds, context=activation.runtime.context, deadline=deadline):
                        return False
                return True
        except TimeoutError, TransientLimitError:
            return False
