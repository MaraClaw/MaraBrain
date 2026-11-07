import secrets
from typing import Literal, assert_never

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import BaseModel, ConfigDict, SecretStr

from marabrain import __version__
from marabrain.config import Settings, Transport


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    version: Literal["0.1.0"] = __version__


class BearerVerifier(TokenVerifier):
    def __init__(self, secret: SecretStr) -> None:
        super().__init__()
        self._secret = secret

    async def verify_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(token.encode(), self._secret.get_secret_value().encode()):
            return AccessToken(token=token, client_id="marabrain", scopes=[])
        return None


def create_server(settings: Settings) -> FastMCP:
    auth = BearerVerifier(settings.bearer_secret) if settings.transport == Transport.HTTP and settings.bearer_secret is not None else None
    server = FastMCP("marabrain-mcp", version=__version__, auth=auth, mask_error_details=True, strict_input_validation=True, tasks=False)

    @server.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def health() -> HealthResponse:
        """Report server liveness and the running application version."""
        return HealthResponse()

    return server


def main() -> None:
    settings = Settings()
    server = create_server(settings)
    transport = settings.transport
    match transport:
        case Transport.STDIO:
            server.run(transport=transport.value, show_banner=False, log_level=settings.log_level)
            return
        case Transport.HTTP:
            server.run(
                transport=transport.value,
                show_banner=False,
                host=settings.host,
                port=settings.port,
                log_level=settings.log_level,
                stateless_http=True,
                host_origin_protection=True,
                allowed_hosts=settings.allowed_hosts,
                allowed_origins=settings.allowed_origins,
                uvicorn_config={"timeout_graceful_shutdown": 5, "timeout_keep_alive": 5, "limit_concurrency": 100},
            )
            return
    assert_never(transport)
