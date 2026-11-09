"""Bind exact workspace identities to private, non-symlinked persistent storage."""

import fcntl
import hashlib
import os
import stat
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Never

from fastmcp.exceptions import ToolError


def workspace_path(workspace_id: str, root: Path) -> Path:
    """Return a bounded opaque path without interpreting any part of the exact identifier."""
    if not workspace_id:
        raise ToolError("Workspace identity is invalid.")
    try:
        token = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:16]
    except UnicodeError:
        raise ToolError("Workspace identity is invalid.") from None
    return root.absolute() / token


def _private(info: os.stat_result) -> None:
    """Refuse shared permissions or ownership on private workspace objects."""
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ToolError("Workspace storage must be private and owned by the server user.")


def _directory(stack: ExitStack, parent: int, name: str) -> int:
    """Open one directory relative to a pinned parent without following a symlink."""
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        pass
    else:
        os.fsync(parent)
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    stack.callback(os.close, descriptor)
    return descriptor


def _scan_error(error: OSError) -> Never:
    """Fail storage validation when any descendant cannot be inspected."""
    raise error


def prepare_workspace(workspace_id: str, root: Path, timeout: float = 30.0) -> None:
    """Create isolated storage, checking its durable exact-ID binding before engine use."""
    path = workspace_path(workspace_id, root)
    # Crystalline 0.31.0 selects the control socket from XDG_STATE_HOME.
    socket = path / "s" / "crystalline" / "service.sock"
    if len(os.fsencode(socket)) > (103 if sys.platform == "darwin" else 107):
        raise ToolError("Workspace engine socket path is too long.")
    if ".." in path.parts:
        raise ToolError("Workspace storage path is unsafe.")
    try:
        with ExitStack() as stack:
            descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, descriptor)
            for component in root.absolute().parts[1:]:
                descriptor = _directory(stack, descriptor, component)
            _private(os.fstat(descriptor))
            descriptor = _directory(stack, descriptor, path.name)
            _private(os.fstat(descriptor))
            identity = os.open("identity", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=descriptor)
            with os.fdopen(identity, "r+b") as handle:
                info = os.fstat(handle.fileno())
                _private(info)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ToolError("Workspace storage path is unsafe.")
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ToolError("Workspace identity storage is busy.") from None
                        time.sleep(min(0.01, remaining))
                expected = workspace_id.encode("utf-8")
                existing = handle.read(len(expected) + 1)
                if existing:
                    if existing != expected:
                        raise ToolError("Workspace identity does not match its storage.")
                else:
                    # Refuse unclaimed data rather than adopting an existing store.
                    if os.listdir(descriptor) != ["identity"]:
                        raise ToolError("Workspace storage has no identity binding.")
                    handle.write(expected)
                    handle.flush()
                    os.fsync(handle.fileno())
                    os.fsync(descriptor)
                for name in ("h", "c", "s", "d", "k", "r"):
                    child = _directory(stack, descriptor, name)
                    _private(os.fstat(child))
                # Existing daemon/config/database/domain entries must never redirect outside this workspace.
                for directory, subdirectories, files in path.walk(follow_symlinks=False, on_error=_scan_error):
                    for name in (*subdirectories, *files):
                        info = (directory / name).lstat()
                        if stat.S_ISLNK(info.st_mode):
                            raise ToolError("Workspace storage path is unsafe.")
                        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                            raise ToolError("Workspace storage must be private and owned by the server user.")
    except OSError:
        raise ToolError("Workspace storage is unavailable or unsafe.") from None
