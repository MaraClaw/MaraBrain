"""Pytest fixtures shared by the memory tool tests."""

from pathlib import Path
from collections.abc import Iterator
from tempfile import TemporaryDirectory

import pytest
import anyio
from memory_fake import FAKE_CLI, domain_list, respond, workspace_registry

from marabrain.domain_registry import DomainRegistry


@pytest.fixture
def workspace_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Own a short workspace root so Unix control sockets fit the platform path limit."""
    with TemporaryDirectory(prefix="mbw-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        monkeypatch.setenv("MARABRAIN_WORKSPACE_ROOT", str(root))
        yield root


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_root: Path) -> Path:
    """Put a recording fake crystalline first on PATH and return its directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "crystalline"
    executable.write_text(FAKE_CLI)
    executable.chmod(0o755)
    respond(bin_dir)
    domain_list(bin_dir, ["notes"])
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    anyio.run(DomainRegistry.from_environment(30).synchronize)
    anyio.run(workspace_registry().synchronize)
    (bin_dir / "calls.jsonl").unlink()
    return bin_dir
