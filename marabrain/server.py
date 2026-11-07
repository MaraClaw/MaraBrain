"""MaraBrain MCP server construction and transport startup."""

import secrets
import subprocess
from typing import Literal, assert_never

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import BaseModel, ConfigDict, SecretStr

from marabrain import __version__
from marabrain.config import Settings, Transport


class HealthResponse(BaseModel):
    """Immutable readiness response returned once the Crystalline daemon answers."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    version: Literal["0.1.0"] = __version__


class BearerVerifier(TokenVerifier):
    """Token verifier for the configured shared bearer secret."""

    def __init__(self, secret: SecretStr) -> None:
        """Initialize the verifier with the shared bearer secret."""
        super().__init__()
        self._secret = secret

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an access token for a matching secret, or None for a mismatch."""
        if secrets.compare_digest(token.encode(), self._secret.get_secret_value().encode()):
            return AccessToken(token=token, client_id="marabrain", scopes=[])
        return None


def create_server(settings: Settings) -> FastMCP:
    """Construct the MCP server with its health tool and configured authentication."""
    auth = BearerVerifier(settings.bearer_secret) if settings.transport == Transport.HTTP and settings.bearer_secret is not None else None
    server = FastMCP("marabrain-mcp", version=__version__, auth=auth, mask_error_details=True, strict_input_validation=True, tasks=False)

    @server.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def health() -> HealthResponse:
        """Report the application version once the Crystalline daemon answers its status probe, or fail within 2 seconds."""
        try:
            with anyio.fail_after(2):
                await anyio.run_process(["crystalline", "ctl", "status"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError, TimeoutError) as error:
            raise ToolError("Crystalline daemon is not responding.") from error
        return HealthResponse()

    return server


def main() -> None:
    """Run the MCP server using the environment-configured transport."""
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
