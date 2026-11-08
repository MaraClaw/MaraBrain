"""Pytest fixtures shared by the memory tool tests."""

from pathlib import Path

import pytest
import anyio
from memory_fake import FAKE_CLI, domain_list, respond

from marabrain.domain_registry import DomainRegistry


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    (bin_dir / "calls.jsonl").unlink()
    return bin_dir
