"""Capture one application's immutable Crystalline execution environment."""

import hashlib
import json
import os
import pwd
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class EngineContext:
    """Bind resolved HOME/XDG defaults and configuration identity without exposing secrets."""

    environment: Mapping[str, str] = field(repr=False)
    state_home: Path
    config_identity: str

    def __post_init__(self) -> None:
        """Detach caller-owned mappings and expose only an immutable environment snapshot."""
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    @classmethod
    def capture(cls) -> EngineContext:
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
        return cls(environment, Path(environment["XDG_STATE_HOME"]), fingerprint)
