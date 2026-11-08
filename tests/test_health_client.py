"""Smoke client credential and environment precedence behavior."""

from pathlib import Path

import pytest
from pydantic_core import PydanticCustomError

import test_health


@pytest.mark.asyncio
async def test_smoke_client_rejects_conflicting_sources_without_leaking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The smoke client rejects raw and file credentials together without exposing either value."""
    secret = "private-credential-" * 3
    path = tmp_path / "secret"
    path.write_text(secret)
    monkeypatch.setattr(test_health, "__file__", str(tmp_path / "test_health.py"))
    monkeypatch.setenv("MARABRAIN_BEARER_SECRET", secret)
    monkeypatch.setenv("MARABRAIN_BEARER_SECRET_FILE", str(path))
    with pytest.raises(PydanticCustomError) as error:
        await test_health.main()
    assert secret not in str(error.value)
