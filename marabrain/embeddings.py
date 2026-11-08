"""Probe a remote embeddings provider and configure Crystalline to use it at startup."""

import os
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Final, Literal, Self

import httpx
import yaml
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

from marabrain.credentials import resolve_secret

ENV_PREFIX: Final = "EMBEDDING_"
ENV_NAMES: Final = ("EMBEDDING_MODEL", "EMBEDDING_PROVIDER_URL", "EMBEDDING_DIMS", "EMBEDDING_API_KEY", "EMBEDDING_API_KEY_FILE")
TIMEOUT_SECONDS: Final = 10.0
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
PROBE_INPUT: Final = "MaraBrain embeddings probe"

type YamlValue = str | int | float | bool | bytes | date | datetime | None | list[YamlValue] | dict[str, YamlValue]
CONFIG_ADAPTER: Final = TypeAdapter(dict[str, YamlValue])


class EmbeddingsError(Exception):
    """Startup failure whose message never contains credentials, headers, or response bodies."""


class EmbeddingSettings(BaseModel):
    """Remote embedding provider settings parsed from EMBEDDING_ environment variables."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True, str_strip_whitespace=True)

    model: Annotated[str, Field(min_length=1)]
    provider_url: AnyHttpUrl
    dims: Annotated[int, Field(gt=0)]
    api_key: SecretStr | None = None
    api_key_file: Path | None = None

    @field_validator("api_key_file", mode="before")
    @classmethod
    def empty_key_file(cls, value: Path | str | None) -> Path | str | None:
        """Treat an empty credential file setting as absent."""
        return value or None

    @model_validator(mode="after")
    def resolve_api_key(self) -> Self:
        """Resolve the API key once for direct model consumers."""
        object.__setattr__(self, "api_key", resolve_secret(self.api_key, self.api_key_file))
        return self

    @field_validator("provider_url")
    @classmethod
    def require_plain_base(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        """Reject provider URLs that embed credentials, a query, or a fragment."""
        if url.username or url.password or url.query or url.fragment:
            raise PydanticCustomError("provider_url_not_base", "Input should be an http(s) base URL without credentials, query, or fragment")
        return url

    @property
    def base_url(self) -> str:
        """Return the provider base URL without a trailing slash."""
        return str(self.provider_url).rstrip("/")


class EmbeddingItem(BaseModel):
    """Single embedding entry of a provider response."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    index: Literal[0]
    embedding: list[Annotated[float, Field(strict=True, allow_inf_nan=False)]]


class EmbeddingsResponse(BaseModel):
    """Provider response that must hold exactly one embedding."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    data: Annotated[list[EmbeddingItem], Field(min_length=1, max_length=1)]


def load_settings() -> EmbeddingSettings | None:
    """Return validated settings, or None when no EMBEDDING_ variable is set."""
    if not any(os.environ.get(name) for name in ENV_NAMES):
        return None
    try:
        return EmbeddingSettings.model_validate({name.removeprefix(ENV_PREFIX).lower(): os.environ[name] for name in ENV_NAMES if os.environ.get(name)})
    except ValidationError as error:
        issues = error.errors(include_input=False, include_url=False, include_context=False)
        problems = "; ".join(f"{ENV_PREFIX}{str(issue['loc'][0]).upper() if issue['loc'] else 'API_KEY'}: {issue['msg']}" for issue in issues)
        raise EmbeddingsError(f"invalid configuration ({problems})") from None


def probe(settings: EmbeddingSettings) -> None:
    """Request one embedding and raise unless the provider returns a valid vector of the configured dimensions."""
    headers = {"Authorization": f"Bearer {settings.api_key.get_secret_value()}"} if settings.api_key else {}
    request = {"model": settings.model, "input": [PROBE_INPUT]}
    deadline = time.monotonic() + TIMEOUT_SECONDS
    payload = bytearray()
    try:
        with (
            httpx.Client(timeout=TIMEOUT_SECONDS, limits=httpx.Limits(max_connections=1, max_keepalive_connections=0), follow_redirects=False) as client,
            client.stream("POST", f"{settings.base_url}/embeddings", json=request, headers=headers) as response,
        ):
            if not response.is_success:
                raise EmbeddingsError(f"provider returned HTTP {response.status_code}")
            for chunk in response.iter_bytes():
                payload += chunk
                if len(payload) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                    raise EmbeddingsError("provider response exceeded the size or time limit")
    except httpx.HTTPError as error:
        raise EmbeddingsError(f"provider request failed ({type(error).__name__})") from None
    try:
        result = EmbeddingsResponse.model_validate_json(payload)
    except ValidationError:
        raise EmbeddingsError("provider response is not a valid embeddings response") from None
    returned = len(result.data[0].embedding)
    if returned != settings.dims:
        raise EmbeddingsError(f"provider returned {returned} dimensions but EMBEDDING_DIMS is {settings.dims}")


def write_config(settings: EmbeddingSettings) -> None:
    """Replace the embeddings section of Crystalline's config.yaml atomically, keeping every other section and never storing the API key."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = config_home / "crystalline" / "config.yaml"
    embeddings: dict[str, YamlValue] = {"provider": "openai-compatible", "model": settings.model, "endpoint": settings.base_url}
    if settings.api_key is not None:
        embeddings["api_key_env"] = "EMBEDDING_API_KEY"
    config: dict[str, YamlValue]
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        config = {} if loaded is None else CONFIG_ADAPTER.validate_python(loaded)
    except FileNotFoundError:
        config = {}
    except OSError as error:
        raise EmbeddingsError(f"cannot read {path}: {error.strerror}") from None
    except yaml.YAMLError, UnicodeDecodeError, ValidationError:
        raise EmbeddingsError(f"{path} is not a valid Crystalline configuration mapping") from None
    config["embeddings"] = embeddings
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".config.yaml.")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                yaml.safe_dump(config, stream, sort_keys=False, allow_unicode=True)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:
        raise EmbeddingsError(f"cannot write {path}: {error.strerror}") from None


def initialize_embeddings() -> dict[str, str]:
    """Validate and configure embeddings, returning the daemon's credential environment overlay."""
    settings = load_settings()
    if settings is None:
        return {}
    probe(settings)
    write_config(settings)
    print(f"marabrain.embeddings: configured remote embeddings (model {settings.model}, {settings.dims} dimensions)", file=sys.stderr)
    return {"EMBEDDING_API_KEY": settings.api_key.get_secret_value()} if settings.api_key is not None else {}


def main() -> int:
    """Configure remote embeddings when EMBEDDING_ variables are set and return the process exit status."""
    try:
        initialize_embeddings()
    except EmbeddingsError as error:
        print(f"marabrain.embeddings: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
