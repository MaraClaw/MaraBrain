"""Verify the immutable managed residency policy and actual launcher forwarding."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError
from test_docker_runtime import SECRET, launcher as launcher

from marabrain.config import EngineMode, Settings


@pytest.mark.parametrize("mode", list(EngineMode))
@pytest.mark.parametrize("raw", [None, "", "1", "8"])
def test_residency_environment_requires_managed_ownership(monkeypatch: pytest.MonkeyPatch, mode: EngineMode, raw: str | None) -> None:
    """Disable absent limits in both modes and accept configured limits only with owned daemons."""
    monkeypatch.setenv("MARABRAIN_ENGINE_MODE", mode.value)
    monkeypatch.delenv("MARABRAIN_MAX_RESIDENT_WORKSPACES", raising=False)
    if raw is not None:
        monkeypatch.setenv("MARABRAIN_MAX_RESIDENT_WORKSPACES", raw)
    if raw and mode is EngineMode.EXTERNAL:
        with pytest.raises(ValidationError, match="requires managed"):
            Settings()
    else:
        assert Settings().max_resident_workspaces == (int(raw) if raw else None)


@pytest.mark.parametrize("raw", ["private-secret", " ", "{}", "[]", "null", "true", "false", "1.5", "1.0", "1e2", "0", "-1", '"2"'])
def test_residency_environment_rejects_redacted_invalid_scalars(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Reject malformed environment scalars without echoing private input."""
    monkeypatch.setenv("MARABRAIN_ENGINE_MODE", "managed")
    monkeypatch.setenv("MARABRAIN_MAX_RESIDENT_WORKSPACES", raw)
    with pytest.raises(ValidationError) as error:
        Settings()
    assert error.value.errors()[0]["loc"] == ("max_resident_workspaces",)
    assert "secret" not in str(error.value) and "input_value" not in str(error.value)


@pytest.mark.parametrize("value", [True, False, 1.0, 2.5, 0, -1, {}, [], "private-secret"])
def test_residency_constructor_rejects_coercion(value: JsonValue) -> None:
    """Require a strict positive integer even for programmatic settings."""
    with pytest.raises(ValidationError) as error:
        Settings.model_validate({"engine_mode": "managed", "max_resident_workspaces": value})
    assert error.value.errors()[0]["loc"] == ("max_resident_workspaces",)
    assert "secret" not in str(error.value) and "input_value" not in str(error.value)


def test_residency_policy_is_a_frozen_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep parsed residency policy independent of environment changes and assignment."""
    monkeypatch.setenv("MARABRAIN_ENGINE_MODE", "managed")
    monkeypatch.setenv("MARABRAIN_MAX_RESIDENT_WORKSPACES", "2")
    settings = Settings()
    monkeypatch.setenv("MARABRAIN_MAX_RESIDENT_WORKSPACES", "7")
    with pytest.raises(ValidationError, match="frozen_instance"):
        setattr(settings, "max_resident_workspaces", 3)
    assert settings.max_resident_workspaces == 2
    assert Settings().max_resident_workspaces == 7


@pytest.mark.parametrize(
    "exported,dotenv,expected,valid",
    [
        (None, None, None, True),
        ("", None, "", True),
        ("2", None, "2", True),
        ("private-secret", "3", "3", True),
        ("2", "", "", True),
        ("2", "0", "0", False),
        ("2", "true", "true", False),
        ("2", "1.0", "1.0", False),
        (None, "private-secret", "private-secret", False),
    ],
)
def test_launcher_validates_and_runs_identical_managed_residency(
    launcher: Path, exported: str | None, dotenv: str | None, expected: str | None, valid: bool
) -> None:
    """Apply dotenv precedence and managed ownership in real preflight settings and runtime arguments."""
    name = "MARABRAIN_MAX_RESIDENT_WORKSPACES"
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_"))}
    environment.update(
        PATH=f"{launcher}{os.pathsep}{os.environ['PATH']}",
        DOCKER_LOG=str(launcher / "calls"),
        MARABRAIN_BEARER_SECRET=SECRET,
        MARABRAIN_ENGINE_MODE="external",
        DOCKER_VALIDATE_PREFLIGHT="1",
    )
    if exported is not None:
        environment[name] = exported
    if dotenv is not None:
        (launcher / ".env").write_text(f"{name}='{dotenv}'\nMARABRAIN_ENGINE_MODE=external\n")
    result = subprocess.run([str(launcher / "start-from-docker.sh")], env=environment, capture_output=True, text=True, timeout=20, check=False)
    calls = [json.loads(line) for line in (launcher / "calls").read_text().splitlines()]
    assert (result.returncode == 0) is valid, result.stderr
    assert len(calls) == (3 if valid else 2)
    assert "private-secret" not in result.stderr
    for call in calls[1:]:
        assert ["--env", "MARABRAIN_ENGINE_MODE=managed"] in [call["args"][index : index + 2] for index in range(len(call["args"]) - 1)]
        assert ["--env", name] in [call["args"][index : index + 2] for index in range(len(call["args"]) - 1)]
        assert call["env"].get(name) == expected
