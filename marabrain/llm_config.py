"""Typed environment settings for optional LLM compression."""

import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from fastmcp.exceptions import ToolError
from pydantic import (
    AnyHttpUrl,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(StrEnum):
    """Supported generation provider protocols."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class LLMSettings(BaseSettings):
    """Immutable LLM settings; selected options must be supported by the provider and model."""

    model_config = SettingsConfigDict(
        env_prefix="LLM_", populate_by_name=True, hide_input_in_errors=True, env_ignore_empty=True, frozen=True, str_strip_whitespace=True
    )

    provider: LLMProvider = LLMProvider.OPENAI
    model: Annotated[str, Field(min_length=1)] | None = None
    provider_url: AnyHttpUrl | None = None
    api_key: SecretStr | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    temperature: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] | None = None
    max_tokens: Annotated[int, Field(gt=0)] = 32768
    max_input_tokens: Annotated[int, Field(gt=0)] = 256000
    timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 60.0
    compression_cache_max_entries: Annotated[int, Field(strict=True, gt=0)] | None = None
    compression_cache_max_bytes: Annotated[int, Field(strict=True, gt=0)] | None = None
    compression_cache_total_max_bytes: Annotated[int, Field(strict=True, gt=0)] | None = None

    @field_validator("compression_cache_max_entries", "compression_cache_max_bytes", "compression_cache_total_max_bytes", mode="before")
    @classmethod
    def integer_cache_limit(cls, value: JsonValue) -> JsonValue:
        """Parse integer environment strings while preserving strict scalar validation."""
        match value:
            case "":
                return None
            case str() as text if re.fullmatch(r"[+-]?[0-9]+", text):
                return int(text)
            case _:
                return value

    @field_validator("provider_url")
    @classmethod
    def plain_base_url(cls, url: AnyHttpUrl | None) -> AnyHttpUrl | None:
        """Reject URLs containing credentials, a query, or a fragment."""
        if url is not None and (url.username is not None or url.password is not None or url.query is not None or url.fragment is not None):
            raise PydanticCustomError("provider_url_not_base", "Provider URL must not contain credentials, query, or fragment")
        return url

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        """Reject incomplete configuration and incompatible Anthropic options."""
        if (self.model is None) != (self.provider_url is None):
            raise PydanticCustomError("incomplete_llm", "Model and provider URL must be configured together")
        if (self.compression_cache_max_entries is None) != (self.compression_cache_max_bytes is None):
            raise PydanticCustomError("incomplete_compression_cache", "Compression cache max entries and max bytes must be configured together")
        if self.compression_cache_total_max_bytes is not None and self.compression_cache_max_entries is None:
            raise PydanticCustomError("compression_cache_total_without_limits", "Compression cache total max bytes requires max entries and max bytes")
        if self.provider == LLMProvider.ANTHROPIC and self.temperature is not None:
            raise PydanticCustomError("anthropic_temperature", "Anthropic temperature is not supported")
        return self

    def require_configured(self) -> None:
        """Raise a fixed tool error when compression cannot be configured."""
        if self.model is None or self.provider_url is None:
            raise ToolError("LLM compression is not configured.")
        if self.provider == LLMProvider.ANTHROPIC and (self.api_key is None or not self.api_key.get_secret_value()):
            raise ToolError("Anthropic compression requires an API key.")
