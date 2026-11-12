"""Environment-backed configuration for the MaraBrain MCP server."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, TypeAdapter, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

from marabrain.credentials import resolve_secret
from marabrain.llm_config import LLMSettings
from marabrain.models import Domain


class Transport(StrEnum):
    """Supported MCP server transports."""

    STDIO = "stdio"
    HTTP = "http"


class EngineMode(StrEnum):
    """Supported engine lifecycle modes."""

    EXTERNAL = "external"
    MANAGED = "managed"


class Settings(BaseSettings):
    """Immutable server settings loaded from MARABRAIN_ environment variables."""

    model_config = SettingsConfigDict(env_prefix="MARABRAIN_", frozen=True, hide_input_in_errors=True)

    transport: Transport = Transport.STDIO
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 9110
    log_level: Literal["critical", "error", "warning", "info", "debug"] = "info"
    bearer_secret: SecretStr | None = None
    bearer_secret_file: Path | None = None
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "[::1]"]
    allowed_origins: list[str] = []
    allowed_domains: tuple[Domain, ...] | None = None
    domain_command_capacity: Annotated[int, Field(strict=True, gt=0)] | None = None
    health_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 2.0
    command_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 30.0
    startup_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    operation_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    max_transient_processes: Annotated[int, Field(ge=2)] | None = None
    max_capture_bytes: Annotated[int, Field(gt=0)] | None = None
    max_total_capture_bytes: Annotated[int, Field(strict=True, gt=0)] | None = None
    max_workspace_transient_processes: Annotated[int, Field(strict=True, gt=0)] | None = None
    max_workspace_capture_bytes: Annotated[int, Field(strict=True, gt=0)] | None = None
    max_input_bytes: Annotated[int, Field(strict=True, gt=0)] | None = None
    max_total_input_bytes: Annotated[int, Field(strict=True, gt=0)] | None = None
    max_resident_workspaces: Annotated[int, Field(strict=True, gt=0)] | None = None
    engine_mode: EngineMode = EngineMode.EXTERNAL
    engine_startup_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 30.0
    workspace_root: Path | None = None
    llm: LLMSettings = Field(default_factory=LLMSettings)

    @field_validator(
        "domain_command_capacity",
        "max_resident_workspaces",
        "max_total_capture_bytes",
        "max_workspace_transient_processes",
        "max_workspace_capture_bytes",
        "max_input_bytes",
        "max_total_input_bytes",
        mode="before",
    )
    @classmethod
    def parse_optional_capacity(cls, value: object) -> int | None:
        """Parse an optional strict positive capacity without exposing rejected input."""
        if value is None or value == "":
            return None
        capacity = TypeAdapter(Annotated[int, Field(strict=True, gt=0)])
        try:
            return capacity.validate_json(value) if isinstance(value, str) else capacity.validate_python(value)
        except ValidationError:
            raise PydanticCustomError("positive_capacity", "Capacity requires a positive integer.") from None

    @model_validator(mode="after")
    def require_managed_residency(self) -> Self:
        """Reject a resident cap when the application does not own its workspace daemons."""
        if self.max_resident_workspaces is not None and self.engine_mode is EngineMode.EXTERNAL:
            raise PydanticCustomError("managed_residency_required", "Resident workspace capacity requires managed engine mode.")
        return self

    @field_validator("startup_timeout_seconds", "operation_timeout_seconds", "max_transient_processes", "max_capture_bytes", mode="before")
    @classmethod
    def empty_optional_limit(cls, value: float | str | None) -> float | str | None:
        """Treat empty optional budgets and limits as disabled without accepting zero."""
        return None if value == "" else value

    @field_validator("bearer_secret_file", "workspace_root", mode="before")
    @classmethod
    def empty_secret_file(cls, value: Path | str | None) -> Path | str | None:
        """Treat an empty optional filesystem setting as absent."""
        return value or None

    @model_validator(mode="after")
    def require_http_secret(self) -> Self:
        """Resolve the bearer credential and reject invalid HTTP settings."""
        secret = resolve_secret(self.bearer_secret, self.bearer_secret_file)
        if secret is not None and len(secret.get_secret_value()) < 32:
            raise PydanticCustomError("bearer_secret_short", "bearer secret must contain at least 32 characters")
        object.__setattr__(self, "bearer_secret", secret)
        if self.transport == Transport.HTTP and self.bearer_secret is None:
            raise PydanticCustomError("http_secret_required", "HTTP transport requires a bearer secret")
        return self
