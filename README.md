# MaraBrain

🦞 Second Brain For OpenClaw-backed Agents.

Python MCP server named `marabrain-mcp`, built with FastMCP. The current interface contains one tool:
`health`, which returns `{"status": "ok", "version": "0.1.0"}` only after the
[Crystalline](https://github.com/jordiboehme/crystalline) background engine answers
`crystalline ctl status`. Missing executables, failed probes, and probes taking
longer than two seconds produce an MCP tool error instead of a successful response.

## Install and run locally

Use [uv](https://docs.astral.sh/uv/). The project requires Python 3.14.4 or newer;
`.python-version` pins the development interpreter.

Install Crystalline using its [upstream installation instructions](https://github.com/jordiboehme/crystalline#get-started),
then run `crystalline serve --http off` in a separate terminal before calling
MaraBrain's `health` tool. The daemon and MaraBrain must use the same user and
HOME/XDG paths so the control socket is shared.

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

## Docker

The multi-stage image uses `python:3.15.2-slim`, installs locked production
dependencies, and runs HTTP as a non-root user. Copy `.env.example` to `.env`
and set `MARABRAIN_BEARER_SECRET` to a generated secret as described above, then run:

```sh
./start-from-docker.sh
```

The launcher sources `.env` beside the script before validating configuration,
regardless of your current directory. Values in `.env` override exported values.
If `.env` is absent, it uses the exported environment. Container startup always
uses HTTP, even if `.env` sets `MARABRAIN_TRANSPORT=stdio` for local use.

The launcher builds `marabrain:latest` and runs a foreground container named
`marabrain`, streaming its logs and publishing `http://127.0.0.1:9110/mcp`.
Press `Ctrl+C` to stop the server and end log monitoring. It uses a read-only root
filesystem, a temporary `/tmp`, dropped Linux capabilities, and an authenticated
MCP healthcheck. The secret is passed at runtime and is never baked into the
image. Docker administrators can inspect container environment variables; keep
access to the Docker daemon restricted.

Override `MARABRAIN_IMAGE`, `MARABRAIN_CONTAINER`, or `MARABRAIN_HOST_PORT` to
choose an image tag, container name, or local port. The host and origin allowlists
from the table above are also forwarded. The host port remains bound to loopback;
put a TLS reverse proxy in front for remote access. The script validates the
configuration before starting the service and never replaces an existing
container automatically.

When overriding `MARABRAIN_ALLOWED_HOSTS`, retain `127.0.0.1` alongside your
public hostname so the container's internal healthcheck remains authorized.

```sh
docker logs -f marabrain
docker stop marabrain
docker rm marabrain
```

## Test local MCP health

With the HTTP server running, open another terminal and run:

```sh
uv run --no-sync python test_health.py
```

The script reads the bearer secret from `.env` beside the script or the exported
environment (exported values take precedence). It calls `health` at
`http://127.0.0.1:9110/mcp`, using `MARABRAIN_HOST_PORT` or `MARABRAIN_PORT` if
configured. A valid `ok` response prints `PASS` and the response JSON. Connection,
authentication, tool, or response validation failures exit with a nonzero status.

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
