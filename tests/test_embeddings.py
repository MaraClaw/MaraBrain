"""Behavior tests for the startup embeddings initializer against a local HTTP provider."""

import json
import os
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from marabrain.embeddings import main

SECRET = "sk-test-secret-value"
LEAK = "LEAKED-RESPONSE-BODY"
EXISTING_CONFIG = "service:\n  http: false\ndomains:\n  - name: notes\ngithub:\n  token_env: GITHUB_TOKEN\n"
EMBEDDING_VARIABLES = ("EMBEDDING_MODEL", "EMBEDDING_PROVIDER_URL", "EMBEDDING_DIMS", "EMBEDDING_API_KEY")


@dataclass(slots=True)
class Request:
    """One request received by the local provider."""

    path: str
    authorization: str | None
    keys: set[str]
    model: str
    inputs: list[str]


@dataclass(slots=True)
class Provider:
    """Local provider replaying a configured response; mutable because tests choose the reply and read the recorded requests."""

    url: str = ""
    status: int = 200
    body: bytes = b""
    requests: list[Request] = field(default_factory=list[Request])


def vector_body(dims: int) -> bytes:
    """Return an OpenAI-style embeddings response holding one vector of the given size."""
    return json.dumps({"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": [0.25] * dims}], "model": "m"}).encode()


@pytest.fixture
def provider() -> Iterator[Provider]:
    """Serve a local HTTP provider on an ephemeral loopback port and record its requests."""
    state = Provider(body=vector_body(3))

    class Handler(BaseHTTPRequestHandler):
        """Record each POST and answer with the provider's configured reply."""

        def do_POST(self) -> None:
            """Record the request and send the configured status and body."""
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            state.requests.append(Request(self.path, self.headers.get("Authorization"), set(body), body["model"], body["input"]))
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, format: str, *args: str) -> None:
            """Silence per-request logging."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture(autouse=True)
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the environment and return the config.yaml path Crystalline would use."""
    for name in EMBEDDING_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    return tmp_path / "xdg" / "crystalline" / "config.yaml"


def configure(monkeypatch: pytest.MonkeyPatch, provider: Provider, **overrides: str) -> None:
    """Set a valid embeddings environment for the local provider, applying per-variable overrides."""
    values = {
        "EMBEDDING_MODEL": "test-model",
        "EMBEDDING_PROVIDER_URL": f"{provider.url}/v1",
        "EMBEDDING_DIMS": "3",
        "EMBEDDING_API_KEY": SECRET,
    } | overrides
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def write_config(path: Path, text: str) -> None:
    """Create the Crystalline config file with the given content."""
    path.parent.mkdir(parents=True)
    path.write_text(text)


def test_without_variables_does_nothing(provider: Provider, config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify missing or empty variables leave the existing config byte-identical and contact no provider."""
    write_config(config_path, EXISTING_CONFIG)
    assert main() == 0
    for name in EMBEDDING_VARIABLES:
        monkeypatch.setenv(name, "")
    assert main() == 0
    assert config_path.read_text() == EXISTING_CONFIG
    assert provider.requests == []


def test_valid_probe_writes_remote_embeddings_and_keeps_other_settings(provider: Provider, config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the probe request shape, the merged config, restrictive permissions, and absence of the credential."""
    write_config(config_path, EXISTING_CONFIG + "embeddings:\n  provider: local\n  api_key_env: OLD_KEY\n  stale: value\n")
    configure(monkeypatch, provider, EMBEDDING_PROVIDER_URL=f"{provider.url}/v1/")
    assert main() == 0
    [request] = provider.requests
    assert request.path == "/v1/embeddings"
    assert request.authorization == f"Bearer {SECRET}"
    assert request.keys == {"model", "input"}
    assert request.model == "test-model"
    assert len(request.inputs) == 1
    config = yaml.safe_load(config_path.read_text())
    assert config["embeddings"] == {
        "provider": "openai-compatible",
        "model": "test-model",
        "endpoint": f"{provider.url}/v1",
        "api_key_env": "EMBEDDING_API_KEY",
    }
    assert {key: config[key] for key in ("service", "domains", "github")} == yaml.safe_load(EXISTING_CONFIG)
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert [entry.name for entry in config_path.parent.iterdir()] == ["config.yaml"]
    assert SECRET not in config_path.read_text()


def test_keyless_provider_sends_no_authorization_and_omits_key_variable(provider: Provider, config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a keyless provider gets no bearer header and a previous api_key_env is removed."""
    write_config(config_path, EXISTING_CONFIG + "embeddings:\n  api_key_env: OLD_KEY\n")
    configure(monkeypatch, provider, EMBEDDING_API_KEY="")
    assert main() == 0
    assert provider.requests[0].authorization is None
    assert yaml.safe_load(config_path.read_text())["embeddings"] == {
        "provider": "openai-compatible",
        "model": "test-model",
        "endpoint": f"{provider.url}/v1",
    }


def test_missing_config_is_created_under_home_when_xdg_is_unset(provider: Provider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the config path falls back to HOME/.config/crystalline."""
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    configure(monkeypatch, provider)
    assert main() == 0
    written = tmp_path / "home" / ".config" / "crystalline" / "config.yaml"
    assert list(yaml.safe_load(written.read_text())) == ["embeddings"]


@pytest.mark.parametrize(
    "status,body,message",
    [
        (401, json.dumps({"error": LEAK}).encode(), "HTTP 401"),
        (500, LEAK.encode(), "HTTP 500"),
        (302, b"", "HTTP 302"),
        (200, LEAK.encode(), "not a valid embeddings response"),
        (200, json.dumps({"data": []}).encode(), "not a valid embeddings response"),
        (200, json.dumps({"data": [{"index": 0, "embedding": [1, 2, 3]}] * 2}).encode(), "not a valid embeddings response"),
        (200, json.dumps({"data": [{"index": 1, "embedding": [1, 2, 3]}]}).encode(), "not a valid embeddings response"),
        (200, json.dumps({"data": [{"index": 0, "embedding": ["a", "b", "c"]}]}).encode(), "not a valid embeddings response"),
        (200, json.dumps({"data": [{"index": 0, "embedding": [True, False, True]}]}).encode(), "not a valid embeddings response"),
        (200, b'{"data": [{"index": 0, "embedding": [NaN, 1, 2]}]}', "not a valid embeddings response"),
        (200, vector_body(4), "returned 4 dimensions but EMBEDDING_DIMS is 3"),
    ],
)
def test_provider_failures_exit_nonzero_without_writing_or_leaking(
    provider: Provider, config_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: int, body: bytes, message: str
) -> None:
    """Verify bad status, JSON, schema, vector, and dimension outcomes fail clearly, leave config untouched, and expose no secret or body."""
    provider.status, provider.body = status, body
    write_config(config_path, EXISTING_CONFIG)
    configure(monkeypatch, provider)
    assert main() == 1
    error = capsys.readouterr().err
    assert message in error
    assert SECRET not in error
    assert LEAK not in error
    assert config_path.read_text() == EXISTING_CONFIG
    assert [entry.name for entry in config_path.parent.iterdir()] == ["config.yaml"]


def test_unreachable_provider_exits_nonzero(config_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify a refused connection fails clearly without creating a config."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    configure(monkeypatch, Provider(url=f"http://127.0.0.1:{port}"))
    assert main() == 1
    error = capsys.readouterr().err
    assert "provider request failed" in error
    assert SECRET not in error
    assert not config_path.exists()


@pytest.mark.parametrize(
    "variables,message",
    [
        ({"EMBEDDING_MODEL": "m"}, "EMBEDDING_PROVIDER_URL: Field required"),
        ({"EMBEDDING_API_KEY": SECRET}, "EMBEDDING_MODEL: Field required"),
        ({"EMBEDDING_MODEL": "m", "EMBEDDING_PROVIDER_URL": "http://127.0.0.1:9"}, "EMBEDDING_DIMS: Field required"),
        ({"EMBEDDING_MODEL": "   "}, "EMBEDDING_MODEL"),
        ({"EMBEDDING_DIMS": "0"}, "EMBEDDING_DIMS"),
        ({"EMBEDDING_DIMS": "-4"}, "EMBEDDING_DIMS"),
        ({"EMBEDDING_DIMS": "1.5"}, "EMBEDDING_DIMS"),
        ({"EMBEDDING_DIMS": "many"}, "EMBEDDING_DIMS"),
        ({"EMBEDDING_PROVIDER_URL": "not a url"}, "EMBEDDING_PROVIDER_URL"),
        ({"EMBEDDING_PROVIDER_URL": "ftp://127.0.0.1/v1"}, "EMBEDDING_PROVIDER_URL"),
        ({"EMBEDDING_PROVIDER_URL": "http://user:p4ssw0rd@127.0.0.1:9/v1"}, "EMBEDDING_PROVIDER_URL"),
        ({"EMBEDDING_PROVIDER_URL": "http://127.0.0.1:9/v1?token=p4ssw0rd"}, "EMBEDDING_PROVIDER_URL"),
        ({"EMBEDDING_PROVIDER_URL": "http://127.0.0.1:9/v1#p4ssw0rd"}, "EMBEDDING_PROVIDER_URL"),
    ],
)
def test_invalid_environment_exits_nonzero_before_any_request(
    provider: Provider, config_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], variables: dict[str, str], message: str
) -> None:
    """Verify partial or malformed variables fail naming the variable, send nothing, write nothing, and echo no input values."""
    configure(monkeypatch, provider)
    for name in EMBEDDING_VARIABLES:
        monkeypatch.delenv(name)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    assert main() == 1
    error = capsys.readouterr().err
    assert message in error
    assert SECRET not in error
    assert "p4ssw0rd" not in error
    assert provider.requests == []
    assert not config_path.exists()


@pytest.mark.parametrize("content", ["github: {token: " + LEAK + "\n", "- " + LEAK + "\n", "42\n", "[]\n", "---\na: 1\n---\nb: 2\n"])
def test_malformed_existing_config_is_left_untouched(
    provider: Provider, config_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    """Verify a config that is not a YAML mapping fails clearly, stays byte-identical, and is never echoed."""
    write_config(config_path, content)
    configure(monkeypatch, provider)
    assert main() == 1
    error = capsys.readouterr().err
    assert "not a valid Crystalline configuration mapping" in error
    assert LEAK not in error
    assert config_path.read_text() == content


def test_module_entry_point_exit_codes(provider: Provider, config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify `python -m marabrain.embeddings` exits 0 as a no-op, writes config on success, and exits 1 on a provider error."""
    command = [sys.executable, "-m", "marabrain.embeddings"]
    noop = subprocess.run(command, env=dict(os.environ), capture_output=True, text=True, timeout=30, check=False)
    assert (noop.returncode, noop.stdout, noop.stderr) == (0, "", "")
    assert not config_path.exists()
    configure(monkeypatch, provider)
    done = subprocess.run(command, env=dict(os.environ), capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0
    assert yaml.safe_load(config_path.read_text())["embeddings"]["model"] == "test-model"
    provider.status = 401
    failed = subprocess.run(command, env=dict(os.environ), capture_output=True, text=True, timeout=30, check=False)
    assert failed.returncode == 1
    assert SECRET not in failed.stderr
