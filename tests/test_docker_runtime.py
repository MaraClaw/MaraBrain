"""Behavior of the Docker launcher and image entrypoint without a Docker daemon."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECRET = "secret-value-not-for-argv-" * 2


@pytest.fixture
def launcher(tmp_path: Path) -> Path:
    """Copy the launcher and install a fake Docker executable in an isolated directory."""
    shutil.copy(ROOT / "start-from-docker.sh", tmp_path)
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['DOCKER_LOG'], 'a') as log:\n"
        "    log.write(json.dumps({'args': sys.argv[1:], 'env': dict(os.environ)}) + '\\n')\n"
    )
    docker.chmod(0o755)
    return tmp_path


@pytest.mark.parametrize("domains", [None, "[]", '["project notes"]'])
def test_launcher_forwards_controls_and_mounts_in_both_containers(launcher: Path, domains: str | None) -> None:
    """Preflight and runtime receive identical controls and read-only credential mounts."""
    bearer = launcher / "bearer"
    key = launcher / "key"
    bearer.write_text(SECRET)
    key.write_text(SECRET)
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_"))}
    environment.update(
        PATH=f"{launcher}{os.pathsep}{os.environ['PATH']}",
        DOCKER_LOG=str(launcher / "calls"),
        MARABRAIN_BEARER_SECRET_FILE=str(bearer),
        EMBEDDING_API_KEY_FILE=str(key),
        MARABRAIN_HEALTH_TIMEOUT_SECONDS="4.5",
        MARABRAIN_COMMAND_TIMEOUT_SECONDS="42",
        MARABRAIN_ENGINE_STARTUP_TIMEOUT_SECONDS="12",
        MARABRAIN_LOG_LEVEL="debug",
        LLM_MODEL="compression-model",
        LLM_PROVIDER="openai",
        LLM_PROVIDER_URL="https://api.openai.com/v1",
        LLM_API_KEY=SECRET,
        LLM_REASONING_EFFORT="low",
        LLM_TEMPERATURE="0.5",
        LLM_MAX_INPUT_TOKENS="256000",
        LLM_MAX_TOKENS="2048",
        LLM_TIMEOUT_SECONDS="30",
    )
    if domains is not None:
        environment["MARABRAIN_ALLOWED_DOMAINS"] = domains
    result = subprocess.run([str(launcher / "start-from-docker.sh")], env=environment, capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (launcher / "calls").read_text().splitlines()]
    assert len(calls) == 3
    for call in calls[1:]:
        args = call["args"]
        assert f"type=bind,source={bearer},target=/run/secrets/marabrain_bearer,readonly" in args
        assert f"type=bind,source={key},target=/run/secrets/embedding_api_key,readonly" in args
        for name in (
            "MARABRAIN_ALLOWED_DOMAINS",
            "MARABRAIN_HEALTH_TIMEOUT_SECONDS",
            "MARABRAIN_COMMAND_TIMEOUT_SECONDS",
            "MARABRAIN_ENGINE_STARTUP_TIMEOUT_SECONDS",
            "MARABRAIN_LOG_LEVEL",
            "LLM_MODEL",
            "LLM_PROVIDER",
            "LLM_PROVIDER_URL",
            "LLM_API_KEY",
            "LLM_REASONING_EFFORT",
            "LLM_TEMPERATURE",
            "LLM_MAX_INPUT_TOKENS",
            "LLM_MAX_TOKENS",
            "LLM_TIMEOUT_SECONDS",
        ):
            assert name in args
            assert call["env"].get(name) == environment.get(name)
        assert SECRET not in json.dumps(args)


def test_launcher_rejects_missing_credential_before_docker(launcher: Path) -> None:
    """A nonexistent host credential fails before building or launching a container."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("MARABRAIN_", "EMBEDDING_"))}
    environment.update(
        PATH=f"{launcher}{os.pathsep}{os.environ['PATH']}", DOCKER_LOG=str(launcher / "calls"), MARABRAIN_BEARER_SECRET_FILE=str(launcher / "missing")
    )
    result = subprocess.run([str(launcher / "start-from-docker.sh")], env=environment, capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode != 0
    assert not (launcher / "calls").exists()


@pytest.mark.parametrize("default", [True, False])
def test_entrypoint_executes_default_managed_and_alternate_directly(tmp_path: Path, default: bool) -> None:
    """Only the default image command forces managed mode and the command is exec'd."""
    command = tmp_path / ("marabrain" if default else "alternate")
    command.write_text('#!/bin/sh\nprintf \'%s %s\' "$$" "${MARABRAIN_ENGINE_MODE:-unset}"\n')
    command.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}", "MARABRAIN_ENGINE_MODE": "external"}
    process = subprocess.Popen([str(ROOT / "docker-entrypoint.sh"), command.name], env=environment, stdout=subprocess.PIPE, text=True)
    output, _ = process.communicate(timeout=10)
    assert process.returncode == 0
    assert output == f"{process.pid} {'managed' if default else 'external'}"
