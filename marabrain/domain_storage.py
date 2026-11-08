"""Protect persistent registry files and serialize cooperating server processes."""

import fcntl
import os
import stat
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio
from fastmcp.exceptions import ToolError

from marabrain.engine_context import EngineContext


def safe_path(path: Path) -> Path:
    """Reject symlinks in every existing component of an absolute storage path."""
    absolute = path.expanduser().absolute()
    if ".." in absolute.parts:
        raise ToolError("Domain storage path is unsafe.")
    for component in (*reversed(absolute.parents), absolute):
        if component.is_symlink():
            raise ToolError("Domain storage path is unsafe.")
    return absolute


@dataclass(frozen=True, slots=True)
class DomainStorage:
    """Local persistent state protected by a cooperative POSIX process lock."""

    root: Path
    timeout: float

    @classmethod
    def from_environment(cls, timeout: float, *, context: EngineContext | None = None) -> DomainStorage:
        """Capture the writable XDG state location independently of the working directory."""
        captured = context if context is not None else EngineContext.capture()
        return cls(captured.state_home / "marabrain", timeout)

    @asynccontextmanager
    async def locked(self) -> AsyncGenerator[Path]:
        """Serialize snapshots and registration without holding a database transaction over CLI calls."""
        try:
            root = safe_path(self.root)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = root.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ToolError("Domain storage must be private and owned by the server user.")
            lock = os.open(root / "registry.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(lock, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ToolError("Domain storage path is unsafe.")
                try:
                    with anyio.fail_after(self.timeout):
                        while True:
                            try:
                                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                await anyio.sleep(0.01)
                    database = safe_path(root / "domains.sqlite3")
                    for suffix in ("-journal", "-wal", "-shm"):
                        safe_path(root / (database.name + suffix))
                    yield database
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError, TimeoutError:
            raise ToolError("Domain registry storage is unavailable or busy.") from None

    def domain_path(self, name: str) -> Path:
        """Prepare a domain directory while refusing symlinked roots and manifests."""
        path = safe_path(self.root / "domains" / name)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe_path(path / "MANIFEST.md")
        for directory, subdirectories, files in path.walk():
            for entry in (*subdirectories, *files):
                if (directory / entry).is_symlink():
                    raise ToolError("Domain storage path is unsafe.")
        return path
