"""Capture one application's immutable Crystalline execution environment."""

import hashlib
import json
import os
import pwd
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

from marabrain.config import Settings
from marabrain.transient import TransientRuntime, WorkspaceTransients


@dataclass(frozen=True, slots=True)
class EngineContext:
    """Bind resolved HOME/XDG defaults and configuration identity without exposing secrets."""

    environment: Mapping[str, str] = field(repr=False)
    state_home: Path
    config_identity: str
    transients: TransientRuntime = field(default_factory=TransientRuntime, repr=False, compare=False)
    workspace_transients: WorkspaceTransients | None = field(default=None, repr=False, compare=False)

    @classmethod
    def for_application(cls, settings: Settings) -> EngineContext:
        """Bind programmatic limits to one entry-time environment capture."""
        return replace(
            cls.capture(),
            transients=TransientRuntime(
                settings.max_transient_processes,
                settings.max_capture_bytes,
                settings.max_total_capture_bytes,
                max_workspace_transient_processes=settings.max_workspace_transient_processes,
                max_workspace_capture_bytes=settings.max_workspace_capture_bytes,
                max_total_input_bytes=settings.max_total_input_bytes,
            ),
        )

    @property
    def data_transients(self) -> TransientRuntime | WorkspaceTransients:
        """Return the workspace share for memory and registry commands, else the application runtime."""
        return self.transients if self.workspace_transients is None else self.workspace_transients

    def for_workspace(self, workspace_id: str, root: Path) -> EngineContext:
        """Derive private workspace selectors and share while retaining application-wide transient quotas."""
        from marabrain.workspace_storage import workspace_path

        private = workspace_path(workspace_id, root)
        environment = {name: value for name, value in self.environment.items() if not name.startswith(("CRYSTALLINE_", "XDG_"))}
        selectors = {
            "HOME": private / "h",
            "XDG_CONFIG_HOME": private / "c",
            "XDG_STATE_HOME": private / "s",
            "XDG_DATA_HOME": private / "d",
            "XDG_CACHE_HOME": private / "k",
            "XDG_RUNTIME_DIR": private / "r",
            "XDG_CONFIG_DIRS": private / "c",
            "XDG_DATA_DIRS": private / "d",
        }
        environment.update({name: str(path) for name, path in selectors.items()})
        identity = {"workspace_id": workspace_id, **{name: str(path) for name, path in selectors.items()}}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return EngineContext(environment, selectors["XDG_STATE_HOME"], fingerprint, self.transients, self.transients.for_workspace(workspace_id))

    def __post_init__(self) -> None:
        """Detach caller-owned mappings and expose only an immutable environment snapshot."""
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    @classmethod
    def capture(
        cls,
        *,
        max_transient_processes: int | None = None,
        max_capture_bytes: int | None = None,
        max_total_capture_bytes: int | None = None,
        max_workspace_transient_processes: int | None = None,
        max_workspace_capture_bytes: int | None = None,
        max_total_input_bytes: int | None = None,
    ) -> EngineContext:
        """Snapshot the environment and pin filesystem defaults before ambient state changes."""
        environment = dict(os.environ)
        home = Path(environment.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir).absolute()
        environment["HOME"] = str(home)
        defaults = {
            "XDG_CONFIG_HOME": home / ".config",
            "XDG_STATE_HOME": home / ".local" / "state",
            "XDG_DATA_HOME": home / ".local" / "share",
            "XDG_CACHE_HOME": home / ".cache",
        }
        for name, default in defaults.items():
            selected = Path(environment.get(name) or default)
            environment[name] = str(selected if selected.is_absolute() else default)
        if environment.get("CRYSTALLINE_CONFIG"):
            selected = environment["CRYSTALLINE_CONFIG"]
            if selected == "~" or selected.startswith("~/"):
                selected = str(home) + selected[1:]
            environment["CRYSTALLINE_CONFIG"] = str(Path(selected).absolute())
        identity = {name: value for name, value in environment.items() if name in ("HOME", "XDG_RUNTIME_DIR", *defaults) or name.startswith("CRYSTALLINE_")}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        transients = TransientRuntime(
            max_transient_processes,
            max_capture_bytes,
            max_total_capture_bytes,
            max_workspace_transient_processes=max_workspace_transient_processes,
            max_workspace_capture_bytes=max_workspace_capture_bytes,
            max_total_input_bytes=max_total_input_bytes,
        )
        return cls(environment, Path(environment["XDG_STATE_HOME"]), fingerprint, transients)
