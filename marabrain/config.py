"""Environment-backed configuration for the MaraBrain MCP server."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Transport(StrEnum):
    """Supported MCP server transports."""

    STDIO = "stdio"
    HTTP = "http"


class Settings(BaseSettings):
    """Immutable server settings loaded from MARABRAIN_ environment variables."""

    model_config = SettingsConfigDict(env_prefix="MARABRAIN_", frozen=True, hide_input_in_errors=True)

    transport: Transport = Transport.STDIO
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 9110
    log_level: Literal["critical", "error", "warning", "info", "debug"] = "info"
    bearer_secret: Annotated[SecretStr, Field(min_length=32)] | None = None
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "[::1]"]
    allowed_origins: list[str] = []

    @model_validator(mode="after")
    def require_http_secret(self) -> Self:
        """Reject HTTP settings without a bearer secret."""
        if self.transport == Transport.HTTP and self.bearer_secret is None:
            raise PydanticCustomError("http_secret_required", "HTTP transport requires a bearer secret")
        return self
