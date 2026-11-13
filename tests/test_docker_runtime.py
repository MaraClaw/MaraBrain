"""Behavior of the Docker launcher and image entrypoint without a Docker daemon."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from marabrain.config import Settings

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
        "if os.environ.get('DOCKER_VALIDATE_PREFLIGHT') and sys.argv[1] == 'run' and '--name' not in sys.argv:\n"
        "    environment = {k: v for k, v in os.environ.items() if not k.startswith(('MARABRAIN_', 'EMBEDDING_', 'LLM_'))}\n"
        "    for index, argument in enumerate(sys.argv[:-1]):\n"
        "        if argument == '--env':\n"
        "            name, separator, value = sys.argv[index + 1].partition('=')\n"
        "            value = value if separator else os.environ.get(name)\n"
        "            if value is not None:\n"
        "                environment[name] = value\n"
        "    os.execve(sys.executable, [sys.executable, *sys.argv[sys.argv.index('python') + 1:]], environment)\n"
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
    assert "--rm" in calls[2]["args"]
    assert "type=volume,source=marabrain-data,target=/data" in calls[2]["args"]
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


@pytest.mark.parametrize(
    ("startup", "operation", "expected"),
    [(None, None, (None, None)), ("", "", (None, None)), ("1.25", "", (1.25, None)), ("", "8.5", (None, 8.5)), ("1.25", "8.5", (1.25, 8.5))],
)
def test_launcher_forwards_optional_budgets_for_settings(
    launcher: Path, monkeypatch: pytest.MonkeyPatch, startup: str | None, operation: str | None, expected: tuple[float | None, float | None]
) -> None:
    """Parse actual preflight and runtime forwarding from unset, blank, or configured shell values."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_"))}
    environment.update(PATH=f"{launcher}{os.pathsep}{os.environ['PATH']}", DOCKER_LOG=str(launcher / "calls"))
    budgets = {"MARABRAIN_STARTUP_TIMEOUT_SECONDS": startup, "MARABRAIN_OPERATION_TIMEOUT_SECONDS": operation}
    (launcher / ".env").write_text("".join(f"{name}={value}\n" for name, value in budgets.items() if value is not None))
    result = subprocess.run([str(launcher / "start-from-docker.sh")], env=environment, capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (launcher / "calls").read_text().splitlines()]
    assert len(calls) == 3
    for call in calls[1:]:
        with monkeypatch.context() as container:
            for name, value in budgets.items():
                container.delenv(name, raising=False)
                assert any(call["args"][index : index + 2] == ["--env", name] for index in range(len(call["args"]) - 1))
                assert call["env"].get(name) == value
                if name in call["env"]:
                    container.setenv(name, call["env"][name])
            settings = Settings()
            assert (settings.startup_timeout_seconds, settings.operation_timeout_seconds) == expected


@pytest.mark.parametrize(
    ("exported", "dotenv", "expected"),
    [
        ((None, None), None, (None, None)),
        (("", ""), None, (None, None)),
        (("2", "1"), None, (2, 1)),
        (("3", "256"), None, (3, 256)),
        (("2", ""), None, (2, None)),
        (("", "1"), None, (None, 1)),
        ((None, None), ("4", "512"), (4, 512)),
        (("invalid", "invalid"), ("2", "1"), (2, 1)),
        (("3", "256"), ("", ""), (None, None)),
        (("3", "256"), ("1", "8"), None),
        (("3", "256"), ("0", "8"), None),
        (("3", "256"), ("3", "0"), None),
        (("3", "256"), ("-1", "8"), None),
        (("3", "256"), ("3", "-1"), None),
        (("3", "256"), ("invalid", "8"), None),
        (("3", "256"), ("3", "invalid"), None),
        (("3", "256"), ("2.5", "8"), None),
        (("3", "256"), ("3", "8.5"), None),
    ],
)
def test_launcher_forwards_optional_limits_for_settings(
    launcher: Path,
    exported: tuple[str | None, str | None],
    dotenv: tuple[str | None, str | None] | None,
    expected: tuple[int | None, int | None] | None,
) -> None:
    """Parse each container's forwarded limits, including sourced overrides and invalid values."""
    names = ("MARABRAIN_MAX_TRANSIENT_PROCESSES", "MARABRAIN_MAX_CAPTURE_BYTES")
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_"))}
    environment.update(PATH=f"{launcher}{os.pathsep}{os.environ['PATH']}", DOCKER_LOG=str(launcher / "calls"), MARABRAIN_BEARER_SECRET=SECRET)
    environment.update((name, value) for name, value in zip(names, exported, strict=True) if value is not None)
    if dotenv is not None:
        (launcher / ".env").write_text("".join(f"{name}={value}\n" for name, value in zip(names, dotenv, strict=True) if value is not None))
    result = subprocess.run([str(launcher / "start-from-docker.sh")], env=environment, capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (launcher / "calls").read_text().splitlines()]
    assert len(calls) == 3
    for call in calls[1:]:
        with pytest.MonkeyPatch.context() as container:
            for name in tuple(os.environ):
                if name.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_")):
                    container.delenv(name)
            for index, argument in enumerate(call["args"][:-1]):
                if argument == "--env":
                    name, separator, value = call["args"][index + 1].partition("=")
                    value = value if separator else call["env"].get(name)
                    if value is not None:
                        container.setenv(name, value)
            if expected is None:
                with pytest.raises(ValidationError) as invalid:
                    Settings()
                assert {error["loc"][0] for error in invalid.value.errors()} <= {"max_transient_processes", "max_capture_bytes"}
            else:
                settings = Settings()
                assert (settings.max_transient_processes, settings.max_capture_bytes) == expected


@pytest.mark.parametrize(
    ("exported", "dotenv", "valid"),
    [
        *((values, None, True) for values in ((None, None, None), ("", "", ""), (None, "", None), (None, None, ""))),
        *((values, None, True) for values in (("1", "1", "1"), ("2", None, None), (None, "3", "4096"))),
        ((None, None, None), ("2", "3", "4096"), True),
        (("invalid", "invalid", "invalid"), ("2", "3", "4096"), True),
        (("2", "3", "4096"), ("1", "4", "512"), True),
        *((("2", "3", "4096"), values, True) for values in (("", "", ""), ("", None, None), (None, "", ""))),
        *(
            (tuple(value if index == target else "2" for index in range(3)), None, False)
            for target in range(3)
            for value in ("0", "-1", "1.5", "true", "false", "private-scalar", "{}")
        ),
        *((values, None, False) for values in (("2", "3", None), ("2", None, "4096"), ("2", "3", ""), ("2", "", "4096"))),
        (("2", "3", "4096"), ("0", None, None), False),
        (("2", "3", "4096"), (None, "", None), False),
    ],
)
def test_launcher_forwards_uniform_limits_and_validates_preflight(
    launcher: Path, exported: tuple[str | None, ...], dotenv: tuple[str | None, ...] | None, valid: bool
) -> None:
    """Verify scalar precedence, disabled values and preflight rejection through constructed Docker environments."""
    names = ("MARABRAIN_DOMAIN_COMMAND_CAPACITY", "LLM_COMPRESSION_CACHE_MAX_ENTRIES", "LLM_COMPRESSION_CACHE_MAX_BYTES")
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_"))}
    environment.update(
        PATH=f"{launcher}{os.pathsep}{os.environ['PATH']}",
        DOCKER_LOG=str(launcher / "calls"),
        MARABRAIN_BEARER_SECRET=SECRET,
        DOCKER_VALIDATE_PREFLIGHT="1",
    )
    environment.update((name, value) for name, value in zip(names, exported, strict=True) if value is not None)
    if dotenv is not None:
        (launcher / ".env").write_text("".join(f"{name}='{value}'\n" for name, value in zip(names, dotenv, strict=True) if value is not None))
    result = subprocess.run([str(launcher / "start-from-docker.sh")], env=environment, capture_output=True, text=True, timeout=20, check=False)
    calls = [json.loads(line) for line in (launcher / "calls").read_text().splitlines()]
    assert (result.returncode == 0) == valid, result.stderr
    assert len(calls) == (3 if valid else 2)
    assert valid or "ValidationError" in result.stderr
    assert "private-scalar" not in result.stderr
    expected = tuple(override if override is not None else value for value, override in zip(exported, dotenv or (None,) * 3, strict=True))
    for call in calls[1:]:
        with pytest.MonkeyPatch.context() as container:
            for name in tuple(os.environ):
                if name.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_")):
                    container.delenv(name)
            for name, value in zip(names, expected, strict=True):
                assert any(call["args"][index : index + 2] == ["--env", name] for index in range(len(call["args"]) - 1))
                assert call["env"].get(name) == value
                if value is not None:
                    container.setenv(name, value)
            if valid:
                settings = Settings()
                limits = tuple(int(value) if value else None for value in expected)
                assert (settings.domain_command_capacity, settings.llm.compression_cache_max_entries, settings.llm.compression_cache_max_bytes) == limits


@pytest.mark.parametrize(
    ("exported", "dotenv", "valid"),
    [
        *((values, None, True) for values in ((None, None), ("", ""), ("16", None), (None, "64"), ("16", "64"), ("1", "1"))),
        ((None, None), ("16", "64"), True),
        (("invalid", "invalid"), ("16", "64"), True),
        (("16", "64"), ("", ""), True),
        (("16", "64"), ("32", None), True),
        *(
            (tuple(value if index == target else "16" for index in range(2)), None, False)
            for target in range(2)
            for value in ("0", "-1", "1.5", "true", "false", "private-scalar", "{}")
        ),
        (("16", "64"), ("0", None), False),
    ],
)
def test_launcher_forwards_input_limits_and_validates_preflight(
    launcher: Path, exported: tuple[str | None, ...], dotenv: tuple[str | None, ...] | None, valid: bool
) -> None:
    """Verify both input limits reach preflight and runtime with sourced precedence, and invalid values fail preflight."""
    names = ("MARABRAIN_MAX_INPUT_BYTES", "MARABRAIN_MAX_TOTAL_INPUT_BYTES")
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_"))}
    environment.update(
        PATH=f"{launcher}{os.pathsep}{os.environ['PATH']}", DOCKER_LOG=str(launcher / "calls"), MARABRAIN_BEARER_SECRET=SECRET, DOCKER_VALIDATE_PREFLIGHT="1"
    )
    environment.update((name, value) for name, value in zip(names, exported, strict=True) if value is not None)
    if dotenv is not None:
        (launcher / ".env").write_text("".join(f"{name}='{value}'\n" for name, value in zip(names, dotenv, strict=True) if value is not None))
    result = subprocess.run([str(launcher / "start-from-docker.sh")], env=environment, capture_output=True, text=True, timeout=20, check=False)
    calls = [json.loads(line) for line in (launcher / "calls").read_text().splitlines()]
    assert (result.returncode == 0) == valid, result.stderr
    assert len(calls) == (3 if valid else 2)
    assert valid or "ValidationError" in result.stderr
    assert "private-scalar" not in result.stderr
    expected = tuple(override if override is not None else value for value, override in zip(exported, dotenv or (None,) * 2, strict=True))
    for call in calls[1:]:
        with pytest.MonkeyPatch.context() as container:
            for name in tuple(os.environ):
                if name.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_")):
                    container.delenv(name)
            for name, value in zip(names, expected, strict=True):
                assert any(call["args"][index : index + 2] == ["--env", name] for index in range(len(call["args"]) - 1))
                assert call["env"].get(name) == value
                if value is not None:
                    container.setenv(name, value)
            if valid:
                settings = Settings()
                assert (settings.max_input_bytes, settings.max_total_input_bytes) == tuple(int(value) if value else None for value in expected)


@pytest.mark.parametrize(
    ("exported", "dotenv", "valid"),
    [
        *(
            (values, None, True)
            for values in ((None, None, None), ("", "", ""), (None, None, ""), ("2", "64", None), ("2", "64", ""), ("2", "64", "128"), ("1", "1", "1"))
        ),
        ((None, None, None), ("2", "64", "128"), True),
        (("invalid", "invalid", "invalid"), ("2", "64", "128"), True),
        (("2", "64", "128"), ("", "", ""), True),
        (("2", "64", "128"), (None, None, ""), True),
        (("2", "64", "128"), ("3", "256", "512"), True),
        *((("2", "64", value), None, False) for value in ("0", "-1", "1.5", "true", "private-scalar", "{}", "1e3")),
        *((values, None, False) for values in ((None, None, "128"), ("", "", "128"), ("2", None, "128"), (None, "64", "128"), ("2", "", "128"))),
        (("2", "64", "128"), ("", "", None), False),
        (("2", "64", "128"), (None, None, "0"), False),
    ],
)
def test_launcher_forwards_total_ceiling_and_validates_preflight(
    launcher: Path, exported: tuple[str | None, ...], dotenv: tuple[str | None, ...] | None, valid: bool
) -> None:
    """Forward the application-wide cache ceiling to both containers and reject it in preflight without the per-pair limits."""
    names = ("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "LLM_COMPRESSION_CACHE_MAX_BYTES", "LLM_COMPRESSION_CACHE_TOTAL_MAX_BYTES")
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_"))}
    environment.update(
        PATH=f"{launcher}{os.pathsep}{os.environ['PATH']}",
        DOCKER_LOG=str(launcher / "calls"),
        MARABRAIN_BEARER_SECRET=SECRET,
        DOCKER_VALIDATE_PREFLIGHT="1",
    )
    environment.update((name, value) for name, value in zip(names, exported, strict=True) if value is not None)
    if dotenv is not None:
        (launcher / ".env").write_text("".join(f"{name}='{value}'\n" for name, value in zip(names, dotenv, strict=True) if value is not None))
    result = subprocess.run([str(launcher / "start-from-docker.sh")], env=environment, capture_output=True, text=True, timeout=20, check=False)
    calls = [json.loads(line) for line in (launcher / "calls").read_text().splitlines()]
    assert (result.returncode == 0) == valid, result.stderr
    assert len(calls) == (3 if valid else 2)
    assert valid or "ValidationError" in result.stderr
    assert "private-scalar" not in result.stderr
    expected = tuple(override if override is not None else value for value, override in zip(exported, dotenv or (None,) * 3, strict=True))
    for call in calls[1:]:
        with pytest.MonkeyPatch.context() as container:
            for name in tuple(os.environ):
                if name.startswith(("MARABRAIN_", "EMBEDDING_", "LLM_")):
                    container.delenv(name)
            for name, value in zip(names, expected, strict=True):
                assert any(call["args"][index : index + 2] == ["--env", name] for index in range(len(call["args"]) - 1))
                assert call["env"].get(name) == value
                if value is not None:
                    container.setenv(name, value)
            if valid:
                settings = Settings()
                limits = tuple(int(value) if value else None for value in expected)
                assert (
                    settings.llm.compression_cache_max_entries,
                    settings.llm.compression_cache_max_bytes,
                    settings.llm.compression_cache_total_max_bytes,
                ) == limits


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
