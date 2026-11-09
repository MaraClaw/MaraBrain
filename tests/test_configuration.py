"""Test server configuration snapshots and validation."""

import pytest
from pydantic import ValidationError

from marabrain.config import EngineMode, Settings


def test_settings_capture_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify configuration snapshots retain their own parsed values."""
    monkeypatch.setenv("MARABRAIN_ALLOWED_DOMAINS", '["Notes", "other"]')
    monkeypatch.setenv("MARABRAIN_ENGINE_MODE", "managed")
    first = Settings()
    monkeypatch.setenv("MARABRAIN_ALLOWED_DOMAINS", "[]")
    second = Settings()
    assert first.allowed_domains == ("Notes", "other")
    assert second.allowed_domains == ()
    assert first.engine_mode is EngineMode.MANAGED
    monkeypatch.delenv("MARABRAIN_ALLOWED_DOMAINS")
    defaults = Settings()
    assert defaults.allowed_domains is None
    assert (defaults.health_timeout_seconds, defaults.command_timeout_seconds, defaults.engine_startup_timeout_seconds) == (2.0, 30.0, 30.0)
    assert (defaults.startup_timeout_seconds, defaults.operation_timeout_seconds) == (None, None)


@pytest.mark.parametrize(
    "name", ["HEALTH_TIMEOUT_SECONDS", "COMMAND_TIMEOUT_SECONDS", "ENGINE_STARTUP_TIMEOUT_SECONDS", "STARTUP_TIMEOUT_SECONDS", "OPERATION_TIMEOUT_SECONDS"]
)
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "invalid"])
def test_bad_deadlines_rejected(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    """Reject nonpositive and nonfinite deadlines from the environment."""
    monkeypatch.setenv(f"MARABRAIN_{name}", value)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    ("startup", "operation", "expected"),
    [(None, None, (None, None)), ("", "", (None, None)), ("0.125", "", (0.125, None)), ("", "9.5", (None, 9.5)), ("2.5", "7", (2.5, 7.0))],
)
def test_optional_budgets_are_independent(
    monkeypatch: pytest.MonkeyPatch, startup: str | None, operation: str | None, expected: tuple[float | None, float | None]
) -> None:
    """Parse independent positive totals while leaving absent or empty totals disabled."""
    for name, value in (("STARTUP_TIMEOUT_SECONDS", startup), ("OPERATION_TIMEOUT_SECONDS", operation)):
        monkeypatch.delenv(f"MARABRAIN_{name}", raising=False)
        if value is not None:
            monkeypatch.setenv(f"MARABRAIN_{name}", value)
    settings = Settings()
    assert (settings.startup_timeout_seconds, settings.operation_timeout_seconds) == expected


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((None, None), (None, None)),
        (("", ""), (None, None)),
        (("2", ""), (2, None)),
        (("", "1"), (None, 1)),
        (("7", "65536"), (7, 65536)),
    ],
)
def test_optional_limits_are_independent(
    monkeypatch: pytest.MonkeyPatch, values: tuple[str | None, str | None], expected: tuple[int | None, int | None]
) -> None:
    """Parse independent integer limits while leaving absent or empty limits disabled."""
    for name, value in zip(("MAX_TRANSIENT_PROCESSES", "MAX_CAPTURE_BYTES"), values, strict=True):
        monkeypatch.delenv(f"MARABRAIN_{name}", raising=False)
        if value is not None:
            monkeypatch.setenv(f"MARABRAIN_{name}", value)
    settings = Settings()
    assert (settings.max_transient_processes, settings.max_capture_bytes) == expected


@pytest.mark.parametrize("name", ["MAX_TRANSIENT_PROCESSES", "MAX_CAPTURE_BYTES"])
@pytest.mark.parametrize("value", ["0", "-1", "2.5", "nan", "inf", "invalid", " "])
def test_bad_integer_limits_rejected(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    """Reject nonpositive, fractional and malformed environment limits."""
    monkeypatch.setenv(f"MARABRAIN_{name}", value)
    with pytest.raises(ValidationError) as invalid:
        Settings()
    assert invalid.value.errors()[0]["loc"] == (name.lower(),)


def test_transient_capacity_requires_two_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject capacity that cannot reserve both data and control lanes."""
    monkeypatch.setenv("MARABRAIN_MAX_TRANSIENT_PROCESSES", "1")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("name", ["max_transient_processes", "max_capture_bytes"])
def test_programmatic_limits_reject_fractional_values(name: str) -> None:
    """Reject fractional constructor values instead of truncating them."""
    with pytest.raises(ValidationError) as invalid:
        Settings.model_validate({name: 2.5})
    assert invalid.value.errors()[0]["loc"] == (name,)
    assert invalid.value.errors()[0]["type"] == "int_from_float"


@pytest.mark.parametrize("value", ['["-bad"]', '[" spaced "]', '[""]', '["a\\nb"]'])
def test_bad_domains_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Reject configured domains that violate the memory domain contract."""
    monkeypatch.setenv("MARABRAIN_ALLOWED_DOMAINS", value)
    with pytest.raises(ValidationError):
        Settings()
