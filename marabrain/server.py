"""MaraBrain MCP server construction and transport startup."""

import logging
import secrets
import sys
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager
from typing import Literal, assert_never

import anyio
import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.context import reset_transport, set_transport
from mcp.server.stdio import stdio_server
from pydantic import BaseModel, ConfigDict, SecretStr

from marabrain import __version__
from marabrain.config import Settings, Transport
from marabrain.domain_registry import DomainRegistry
from marabrain.lifecycle import application_signals, engine_lifecycle, probe_engine
from marabrain.lifecycle_stdio import BufferedInput, CancellableInput
from marabrain.memory_tools import register_memory_tools


class HealthResponse(BaseModel):
    """Immutable readiness response returned once the Crystalline daemon answers."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    version: Literal["0.3.0"] = __version__


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
    """Construct the MCP server with its health and memory tools and configured authentication."""
    auth = BearerVerifier(settings.bearer_secret) if settings.transport == Transport.HTTP and settings.bearer_secret is not None else None
    server = FastMCP("marabrain-mcp", version=__version__, auth=auth, mask_error_details=True, strict_input_validation=True, tasks=False)
    health_timeout = settings.health_timeout_seconds
    policy = None if settings.allowed_domains is None else frozenset(settings.allowed_domains)

    @server.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def health() -> HealthResponse:
        """Report the application version once the daemon answers within the configured health deadline."""
        if not await probe_engine(health_timeout):
            raise ToolError("Crystalline daemon is not responding.")
        return HealthResponse()

    register_memory_tools(server, allowed_domains=policy, command_timeout_seconds=settings.command_timeout_seconds, llm_settings=settings.llm)
    return server


class ApplicationHTTPServer(uvicorn.Server):
    """Leave process signals to the shared application lifecycle."""

    @contextmanager
    def capture_signals(self) -> Generator[None]:
        """Serve without replacing the application's termination handlers."""
        yield


async def run_transport(settings: Settings, stdin: anyio.AsyncFile[str] | None = None) -> None:
    """Serve MCP over the selected transport until EOF, failure, or cancellation."""
    server = create_server(settings)
    transport = settings.transport
    match transport:
        case Transport.STDIO:
            token = set_transport("stdio")
            try:
                async with server._lifespan_manager(), stdio_server(stdin=stdin) as (read_stream, write_stream):
                    await server._mcp_server.run(read_stream, write_stream, server._mcp_server.create_initialization_options())
            finally:
                reset_transport(token)
            return
        case Transport.HTTP:
            app = server.http_app(
                stateless_http=True,
                host_origin_protection=True,
                allowed_hosts=settings.allowed_hosts,
                allowed_origins=settings.allowed_origins,
            )
            http = ApplicationHTTPServer(
                uvicorn.Config(
                    app,
                    host=settings.host,
                    port=settings.port,
                    log_level=settings.log_level,
                    access_log=False,
                    lifespan="on",
                    ws="websockets-sansio",
                    timeout_graceful_shutdown=5,
                    timeout_keep_alive=5,
                    limit_concurrency=100,
                )
            )
            try:
                await http.serve()
            except anyio.get_cancelled_exc_class():
                if http.started:
                    with anyio.move_on_after(5, shield=True):
                        await http.shutdown()
                raise
            return
    assert_never(transport)


async def run_application(settings: Settings) -> None:
    """Apply one signal and engine lifecycle to either configured transport."""
    async with application_signals(), anyio.create_task_group() as tasks:
        stdin: anyio.AsyncFile[str] | None = None
        starting = True
        match settings.transport:
            case Transport.STDIO:
                send, receive = anyio.create_memory_object_stream[str](1)
                startup_lines = deque[str]()
                stdin = BufferedInput(sys.stdin, receive, startup_lines)

                async def watch_input() -> None:
                    """Preserve protocol input while observing EOF throughout engine startup."""
                    async with send:
                        async for line in CancellableInput(sys.stdin):
                            if starting:
                                startup_lines.append(line)
                            else:
                                await send.send(line)
                    if starting:
                        tasks.cancel_scope.cancel()

                tasks.start_soon(watch_input)
            case Transport.HTTP:
                receive = None
        try:
            async with engine_lifecycle(settings):
                await DomainRegistry.from_environment(settings.command_timeout_seconds).synchronize()
                starting = False
                await run_transport(settings, stdin)
        finally:
            tasks.cancel_scope.cancel()
            if receive is not None:
                await receive.aclose()


def main() -> None:
    """Run the MCP server using the environment-configured lifecycle and transport."""
    settings = Settings()
    logger = logging.getLogger("marabrain")
    logger.setLevel(settings.log_level.upper())
    logger.propagate = False
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    # FastMCP DEBUG can log tool arguments, so never enable framework DEBUG.
    logging.getLogger("fastmcp").setLevel(max(logging.INFO, logging.getLevelNamesMapping()[settings.log_level.upper()]))
    anyio.run(run_application, settings)
