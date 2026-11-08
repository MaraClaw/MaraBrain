"""Resolve startup credentials without exposing their contents or paths in errors."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_core import PydanticCustomError


def resolve_secret(raw: str | SecretStr | None, file: Path | str | None) -> SecretStr | None:
    """Resolve one credential source, rejecting malformed values with fixed errors."""
    value = raw.get_secret_value() if isinstance(raw, SecretStr) else raw or ""
    if value and file:
        raise PydanticCustomError("credential_conflict", "credential sources conflict")
    if file:
        try:
            value = Path(file).read_bytes().decode("utf-8")
        except OSError, UnicodeDecodeError, ValueError:
            raise PydanticCustomError("credential_file", "cannot read credential file") from None
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        if not value:
            raise PydanticCustomError("credential_empty", "credential file is empty")
    if any(character in value for character in "\r\n\x00"):
        raise PydanticCustomError("credential_invalid", "credential contains invalid characters")
    return SecretStr(value) if value else None
