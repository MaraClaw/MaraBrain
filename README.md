# MaraBrain

🦞 Second Brain For OpenClaw-backed Agents.

Python MCP server named `marabrain-mcp`, built with FastMCP. The current interface contains one tool:
`health`, which returns `{"status": "ok", "version": "0.1.0"}`. It reports that
the server can handle an MCP request; it does not check external services.

## Install and run locally

Use [uv](https://docs.astral.sh/uv/). The project requires Python 3.14.4 or newer;
`.python-version` pins the development interpreter.

```sh
uv sync --locked
uv run --no-sync marabrain
```

The default transport is stdio. Your MCP client starts the process and owns its
lifecycle. `uv run --no-sync python -m marabrain` is an equivalent entry point.
Protocol messages use stdout; diagnostics use stderr.

For a client that accepts an `mcpServers` configuration, set `args` to the
absolute path of this checkout:

```json
{
  "mcpServers": {
    "marabrain-mcp": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/MaraBrain", "run", "--no-sync", "marabrain"]
    }
  }
}
```

## HTTP deployment

HTTP uses the stateless Streamable HTTP transport at `/mcp`. A bearer secret of
at least 32 characters is required; startup fails if it is missing or invalid.
Create a random secret once and store it in your deployment's secret manager:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

After exporting that value as `MARABRAIN_BEARER_SECRET`, start the service:

```sh
MARABRAIN_TRANSPORT=http uv run --no-sync marabrain
```

Clients send `Authorization: Bearer <your secret>` on MCP requests. Use a TLS
reverse proxy for remote access, keep its upstream private, and set the allowed
hosts to the hostname it forwards. The shared bearer credential is intended for
trusted service clients; rotate it by updating both the server and clients and
restarting the server.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `MARABRAIN_TRANSPORT` | `stdio` | `stdio` or `http` |
| `MARABRAIN_HOST` | `127.0.0.1` | HTTP bind address |
| `MARABRAIN_PORT` | `9110` | HTTP port, from 1 to 65535 |
| `MARABRAIN_LOG_LEVEL` | `info` | `critical`, `error`, `warning`, `info`, or `debug` |
| `MARABRAIN_BEARER_SECRET` | unset | Required for HTTP; at least 32 characters |
| `MARABRAIN_ALLOWED_HOSTS` | `["127.0.0.1","localhost","[::1]"]` | JSON array of accepted HTTP hosts |
| `MARABRAIN_ALLOWED_ORIGINS` | `[]` | JSON array of accepted browser origins |

Environment variables are read at startup. An originless MCP client is accepted;
browser clients must use an explicitly allowed origin. Avoid wildcard host or
origin entries. HTTP limits concurrent connections to 100, uses a 5-second
keep-alive timeout, and allows 5 seconds for graceful shutdown. Give your process
supervisor a longer termination grace period. Server errors are masked in MCP
responses; credentials are redacted from configuration errors.

## Development checks

```sh
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync ty check
uv build
```

Tests exercise configuration validation and the MCP interface, including
authentication and transport boundaries. No external service credentials are
needed for the test suite.
