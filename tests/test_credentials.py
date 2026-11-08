"""Test runtime credential source resolution and redaction."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from pydantic_core import PydanticCustomError

from marabrain.config import Settings
from marabrain.credentials import resolve_secret


@pytest.mark.parametrize("suffix", [b"", b"\n", b"\r\n"])
def test_file_secret_preserves_content(tmp_path: Path, suffix: bytes) -> None:
    """Preserve significant spaces while removing at most one terminal newline."""
    path = tmp_path / "secret"
    path.write_bytes(b"  value  " + suffix)
    secret = resolve_secret("", path)
    assert secret is not None
    assert secret.get_secret_value() == "  value  "


@pytest.mark.parametrize("content", [b"", b"\n", b"value\n\n", b"value\r", b"va\x00lue", b"\xff"])
def test_bad_file_is_rejected_safely(tmp_path: Path, content: bytes) -> None:
    """Reject empty, multiline, NUL, and non-UTF-8 file values without disclosing the path."""
    path = tmp_path / "private-path"
    path.write_bytes(content)
    with pytest.raises(PydanticCustomError) as error:
        resolve_secret(None, path)
    assert str(path) not in str(error.value)
    assert "value" not in str(error.value)


def test_bearer_file_snapshot_and_conflict(tmp_path: Path) -> None:
    """Load a protected bearer snapshot and reject simultaneous credential sources."""
    path = tmp_path / "secret"
    path.write_text("a" * 32 + "\n")
    first = Settings(bearer_secret_file=path)
    path.write_text("b" * 32)
    second = Settings(bearer_secret_file=path)
    assert first.bearer_secret == SecretStr("a" * 32)
    assert second.bearer_secret == SecretStr("b" * 32)
    with pytest.raises(ValidationError) as error:
        Settings(bearer_secret=SecretStr("private" * 6), bearer_secret_file=path)
    assert "private" not in str(error.value)
    assert str(path) not in str(error.value)


def test_short_file_and_missing_file_are_safe(tmp_path: Path) -> None:
    """Reject missing or short bearer files without leaking their contents."""
    path = tmp_path / "secret"
    with pytest.raises(ValidationError):
        Settings(bearer_secret_file=path)
    path.write_text("short-secret")
    with pytest.raises(ValidationError) as error:
        Settings(bearer_secret_file=path)
    assert "short-secret" not in str(error.value)
