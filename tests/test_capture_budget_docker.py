"""Exercise aggregate-byte policy forwarding with the launcher's real configuration preflight."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_docker_runtime import SECRET
from test_docker_runtime import launcher as launcher


@pytest.mark.parametrize(
    "exported,dotenv,expected,valid",
    [
        (None, None, None, True),
        ("", None, "", True),
        ("4096", None, "4096", True),
        ("invalid", "2", "2", True),
        ("4096", "", "", True),
        *[("4096", value, value, False) for value in ("0", "-1", "true", "1.0", "2.5", "private-scalar")],
    ],
)
def test_launcher_validates_and_forwards_aggregate_budget(launcher: Path, exported: str | None, dotenv: str | None, expected: str | None, valid: bool) -> None:
    """Apply sourced precedence and identical aggregate policy while preserving managed residency forwarding."""
    name = "MARABRAIN_MAX_TOTAL_CAPTURE_BYTES"
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_"))}
    environment.update(
        PATH=f"{launcher}{os.pathsep}{os.environ['PATH']}",
        DOCKER_LOG=str(launcher / "calls"),
        DOCKER_VALIDATE_PREFLIGHT="1",
        MARABRAIN_BEARER_SECRET=SECRET,
        MARABRAIN_ENGINE_MODE="external",
        MARABRAIN_MAX_RESIDENT_WORKSPACES="2",
    )
    if exported is not None:
        environment[name] = exported
    if dotenv is not None:
        (launcher / ".env").write_text(f"{name}='{dotenv}'\n")
    result = subprocess.run([str(launcher / "start-from-docker.sh")], env=environment, capture_output=True, text=True, timeout=20, check=False)
    calls = [json.loads(line) for line in (launcher / "calls").read_text().splitlines()]
    assert (result.returncode == 0) == valid, result.stderr
    assert len(calls) == (3 if valid else 2)
    assert "private-scalar" not in result.stderr
    for call in calls[1:]:
        assert ["--env", name] in [call["args"][index : index + 2] for index in range(len(call["args"]) - 1)]
        assert call["env"].get(name) == expected
        assert "MARABRAIN_ENGINE_MODE=managed" in call["args"]
        assert "MARABRAIN_MAX_RESIDENT_WORKSPACES" in call["args"]
        assert call["env"]["MARABRAIN_MAX_RESIDENT_WORKSPACES"] == "2"
