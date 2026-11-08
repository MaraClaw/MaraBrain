"""Keep managed remote initialization aligned with the engine's selected configuration."""

from pathlib import Path

import pytest
from test_embeddings import EXISTING_CONFIG, Provider, configure, write_config
from test_embeddings import config_path as config_path
from test_embeddings import provider as provider

from marabrain.embeddings import initialize_embeddings, main


@pytest.mark.parametrize(
    "name",
    [
        "CRYSTALLINE_CONFIG",
        "CRYSTALLINE_EMBEDDINGS_PROVIDER",
        "CRYSTALLINE_EMBEDDINGS_MODEL",
        "CRYSTALLINE_EMBEDDINGS_ENDPOINT",
        "CRYSTALLINE_EMBEDDINGS_API_KEY_ENV",
    ],
)
def test_active_initializer_rejects_incompatible_engine_inputs_before_probe_or_write(
    name: str, provider: Provider, config_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject unsupported selectors and overrides without contacting a provider or changing YAML."""
    write_config(config_path, EXISTING_CONFIG)
    configure(monkeypatch, provider)
    monkeypatch.setenv(name, "private-override-value")
    assert main() == 1
    assert "private-override-value" not in capsys.readouterr().err
    assert provider.requests == []
    assert config_path.read_text() == EXISTING_CONFIG


def test_empty_initializer_preserves_selected_engine_configuration(provider: Provider, config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the no-op path available even when the engine selects another configuration."""
    write_config(config_path, EXISTING_CONFIG)
    monkeypatch.setenv("CRYSTALLINE_CONFIG", "selected.yaml")
    monkeypatch.setenv("CRYSTALLINE_EMBEDDINGS_MODEL", "engine-model")
    assert initialize_embeddings() == {}
    assert provider.requests == []
    assert config_path.read_text() == EXISTING_CONFIG


def test_empty_engine_inputs_allow_remote_initialization(provider: Provider, config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat empty engine selectors and embedding overrides as absent."""
    configure(monkeypatch, provider)
    monkeypatch.setenv("CRYSTALLINE_CONFIG", "")
    monkeypatch.setenv("CRYSTALLINE_EMBEDDINGS_MODEL", "")
    assert main() == 0
    assert len(provider.requests) == 1
    assert config_path.is_file()
