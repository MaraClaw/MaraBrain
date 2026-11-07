"""Pytest fixtures shared by the memory tool tests."""

import os
from pathlib import Path

import pytest
from memory_fake import FAKE_CLI, respond


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a recording fake crystalline first on PATH and return its directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "crystalline"
    executable.write_text(FAKE_CLI)
    executable.chmod(0o755)
    respond(bin_dir)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
    return bin_dir
