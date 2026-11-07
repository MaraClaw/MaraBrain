# MaraBrain

🦞 Second Brain For OpenClaw-backed Agents.

Python MCP server named `marabrain-mcp`, built with FastMCP. The interface contains
six tools and no resources or prompts. `health` returns `{"status": "ok", "version": "0.1.0"}`
only after the [Crystalline](https://github.com/jordiboehme/crystalline) background engine
answers `crystalline ctl status`. Missing executables, failed probes, and probes taking
longer than two seconds produce an MCP tool error instead of a successful response.
The other five tools manage memories through the Crystalline CLI; see [Memory tools](#memory-tools).

## Memory tools

The tools run the pinned Crystalline 0.31.0 `crystalline --json` command without a shell,
send content on stdin, and return its JSON object unchanged (native fields, no added keys).
The `crystalline` executable must be on `PATH`, with the target domains registered
in the same HOME/XDG configuration used by MaraBrain. `health` also requires its
daemon to be running; the Docker image manages both processes. Each memory call
runs the command once with a 30-second deadline and never retries.

| Tool | Arguments | Effect |
| --- | --- | --- |
| `create_memory` | `domain`, `title`, `content`, optional `tags` (list), `folder` | Creates a Markdown memory; an existing one is never overwritten |
| `read_memory` | `domain`, `permalink` | Returns full Markdown with frontmatter, `checksum`, links, observations, and relations |
| `edit_memory` | `domain`, `permalink`, `operation`, optional `expected_checksum` | Applies one edit operation |
| `delete_memory` | `domain`, `permalink` | Permanently deletes without confirmation; no checksum guard |
| `search_memories` | required nonempty `query` and nonempty `domains` (list); optional `tags` (list), `limit` (1-100, default 10), `page` (1 or more, default 1) | Searches the named domains |

`search_memories` requires both query text and at least one domain; a missing, null, empty,
or whitespace-only value is rejected before anything runs. Each hit includes the `domain` and
`permalink` to pass to `read_memory`. Edit `operation` is an object whose `type` selects the fields:

| `type` | Fields besides `content` |
| --- | --- |
| `append`, `prepend` | none |
| `find_replace` | `find_text`; optional `expected_replacements` (0 or more) |
| `replace_section` | `section`; optional `include_subsections` (default false) |
| `insert_before_section`, `insert_after_section` | `section` |

```json
{"domain": "notes", "permalink": "team/plan", "expected_checksum": "<checksum from read_memory>",
 "operation": {"type": "replace_section", "section": "Status", "content": "Shipped."}}
```

Section content is the section body; the heading line stays. Pass the `checksum` from
`read_memory` as `expected_checksum` so an edit is refused if the memory changed after
that read; without it the last write wins. Domain administration, metadata editing, and
attachments are not exposed.

Arguments are validated before anything runs. Domains, titles, permalinks, folders, and
queries must not start with a hyphen, contain control characters, or have surrounding
whitespace. Permalinks and folders must be canonical relative paths (no leading,
trailing, or doubled slashes, `.` or `..` segments, backslashes, or URLs). Tags must not
contain commas, and no text may contain NUL. Failures return a fixed error message: Crystalline
unavailable, a nonzero exit code, malformed output, or a timeout. Raw CLI output and
diagnostics are never returned. A timeout or crash during a mutation does not guarantee
that nothing was applied; read the memory to check.

## Install and run locally

Use [uv](https://docs.astral.sh/uv/). The project requires Python 3.14.4 or newer;
`.python-version` pins the development interpreter.

Install Crystalline using its [upstream installation instructions](https://github.com/jordiboehme/crystalline#get-started),
then run `crystalline serve --http off` in a separate terminal before calling
MaraBrain's `health` tool. The daemon and MaraBrain must use the same user and
HOME/XDG paths so the control socket is shared. Docker manages this daemon for you.

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
dependencies, and copies the Crystalline v0.31.0 binary from its official
multi-architecture image pinned by digest. It runs HTTP and the supervised
Crystalline daemon as a non-root user. Copy `.env.example` to `.env`
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

Crystalline's own HTTP listener is disabled. Its configuration, database, control
socket, and model cache live under `/data`, mounted from the named Docker volume
`marabrain-data`. This volume survives container replacement; back it up before
removing it. Existing volumes must be writable by UID/GID 65532. On first startup,
Crystalline may download its embedding model; subsequent starts reuse the cache.
Health checks engine and store responsiveness, not embedding-model readiness or
semantic-search quality. See the upstream [deployment guide](https://github.com/jordiboehme/crystalline/blob/v0.31.0/docs/deployment.md)
for offline model provisioning.

### Remote embeddings

Set `EMBEDDING_MODEL`, `EMBEDDING_PROVIDER_URL`, and `EMBEDDING_DIMS` together in
`.env` to use an OpenAI-compatible embedding provider. Set `EMBEDDING_API_KEY`
when the endpoint requires bearer authentication; otherwise leave it empty.

| Environment variable | Meaning |
| --- | --- |
| `EMBEDDING_MODEL` | Model name accepted by the provider |
| `EMBEDDING_PROVIDER_URL` | HTTP or HTTPS API base, including any version prefix, without `/embeddings` |
| `EMBEDDING_DIMS` | Expected positive vector length, checked against the provider response |
| `EMBEDDING_API_KEY` | Optional bearer token, passed only at runtime |

Before starting Crystalline, the container sends a small embedding request and
checks its response and vector length. Invalid settings, authentication failures,
unreachable providers, and mismatched dimensions stop startup. Crystalline does
not support a `dimensions` request field, so choose a provider or model that
already returns the expected vector length.

After the check succeeds, startup merges the provider settings into
`/data/config/crystalline/config.yaml`, preserving unrelated settings and domains.
The file stores the API key's environment variable name, never its value. The
remote provider avoids the default Granite embedding-model download. Restart the
container after changing these variables; the startup check does not continuously
monitor the provider, and MCP health continues to check the Crystalline daemon.

Leaving all four variables unset or empty preserves the existing Crystalline
configuration and skips the remote-provider check. A fresh volume then uses the
default local model. Changing providers or models does not automatically migrate
existing vectors; follow Crystalline's indexing guidance before reusing an index
with a different embedding space.

Override `MARABRAIN_IMAGE`, `MARABRAIN_CONTAINER`, `MARABRAIN_HOST_PORT`, or
`MARABRAIN_DATA_VOLUME` to choose an image tag, container name, local port, or
persistent volume. The host and origin allowlists
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
