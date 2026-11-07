# PROJECT KNOWLEDGE BASE

**Generated:** 2026-11-07
**Discovery base:** `79dbd1e` on `develop`, including uncommitted implementation.

## OVERVIEW

Python FastMCP server named `marabrain-mcp`. Its current MCP interface exposes
exactly one tool, `health`, returning `{"status":"ok","version":"0.1.0"}`;
there are no resources or prompts. Health also requires the Crystalline daemon
to answer `crystalline ctl status` within two seconds; engine failure is a tool error.

## STRUCTURE

```text
MaraBrain/
|-- marabrain/             # Runtime package; configuration and server factory
|-- docs/                  # Python docstring standard
|-- tests/test_server.py   # Automated configuration and MCP transport tests
|-- test_health.py         # Client smoke check against an existing HTTP server
|-- start-from-docker.sh   # Build, validate, and run a foreground container
|-- docker-entrypoint.sh   # Supervise Crystalline and the MCP server
|-- Dockerfile            # Locked production installation; HTTP-only runtime
|-- .env.example          # Local and Docker settings; blank bearer credential
|-- pyproject.toml        # Packaging, dependencies, tests, lint, and type checks
|-- uv.lock               # Generated dependency resolution
`-- README.md             # Client configuration and deployment instructions
```

## WHERE TO LOOK

| Task | Location | Notes |
| --- | --- | --- |
| Server construction and tool registration | `marabrain/server.py:create_server` | Factory accepts typed settings; tests use it directly |
| Transport startup | `marabrain/server.py:main` | Both console script and module delegate here |
| Environment parsing | `marabrain/config.py:Settings` | Frozen Pydantic settings with `MARABRAIN_` prefix |
| Remote embedding startup | `marabrain/embeddings.py` | Validate provider and dimensions, then merge persisted Crystalline configuration |
| Transport variants | `marabrain/config.py:Transport` | `stdio` and `http`; startup uses exhaustive matching |
| Application version | `marabrain/__init__.py` | Health model and tests also contain version literals |
| Runtime deployment | `Dockerfile`, `start-from-docker.sh` | Image security and launcher lifecycle are coupled |
| Automated behavior contracts | `tests/test_server.py` | Includes stdio subprocess and HTTP security boundaries |
| Live endpoint check | `test_health.py` | Requires an existing server and bearer credential |

## CODE MAP

LSP symbols and references, cross-checked against AST imports at discovery.
Reference counts exclude declarations and include imports and type references.

| Symbol | Type | Location | References | Role |
| --- | --- | --- | --- | --- |
| `Settings` | Class | `marabrain/config.py` | 11 | Parse and validate environment configuration |
| `create_server` | Function | `marabrain/server.py` | 4 | Construct server for startup and protocol tests |

## CONVENTIONS

- Follow [the Python docstring standard](docs/python-docstrings.md) for all Python
  modules, classes, functions, and methods, including tests and MCP tools. Prefer
  one-sentence PEP 257 docstrings; add Google-style sections only for useful details.
- Use `uv`; project metadata requires Python >=3.14.4, `.python-version` and
  Docker pin 3.15.2, and lint/type-check targets are 3.14.
- Hatchling packages `marabrain`; the console entry point is
  `marabrain = "marabrain.server:main"`. `python -m marabrain` calls the same function.
- Ruff enables `E`, `F`, `W`, and `Q`, enforces double quotes, and permits 160 columns.
- Keep configuration at the `Settings` boundary. HTTP requires a `SecretStr`
  with at least 32 characters; configuration errors redact input values.
- Preserve stateless HTTP, masked tool errors, strict input validation, disabled
  background tasks, and explicit host/origin protection when changing startup.
- HTTP accepts clients without an Origin header. Browser origins require an
  explicit allowlist entry; allowlists are JSON arrays in environment variables.

## TESTS AND COMMANDS

```sh
uv sync --locked
uv run --no-sync marabrain
uv run --no-sync python -m marabrain
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync ty check
uv build
```

- Pytest collects `tests/`. Run it without exported `MARABRAIN_*` settings:
  some tests depend on missing credentials or the default stdio transport.
- The suite checks enum parsing, invalid settings, secret redaction, exact tool
  inventory, stdio startup, and HTTP responses: invalid credentials 401,
  disallowed Host 421, and disallowed Origin 403.
- `uv run --no-sync python test_health.py` checks a running HTTP endpoint;
  it is separate from pytest. It validates structured health content and fails
  on connection, authentication, tool, or response validation errors.
- Installed Pyright reports existing configuration notices for
  `reportMissingReturnType`, `reportExplicitAny`, and `overrides`.

## DEPLOYMENT GOTCHAS

- `Settings` reads environment variables; it does not load `.env` itself.
- The Docker launcher sources `.env` beside the script as shell code. File
  assignments override exported values. The smoke client also loads adjacent
  `.env`, but exported values take precedence there.
- Docker always runs HTTP on container port 9110, bound to `0.0.0.0` internally.
  The launcher publishes only `127.0.0.1`, with configurable host port.
- Retain `127.0.0.1` in allowed hosts: the authenticated container healthcheck
  calls that address. Use a TLS reverse proxy for remote access.
- The launcher runs `exec docker run` in the foreground. Ctrl+C stops the server;
  the stopped named container remains and must be removed before reusing its name.
- Runtime uses UID/GID 65532, a read-only filesystem, temporary `/tmp`, dropped
  capabilities, and no-new-privileges. Secrets are passed at runtime.
- Crystalline runs with its HTTP listener disabled. Its writable configuration,
  state, and cache live under `/data` in the `MARABRAIN_DATA_VOLUME` named volume
  (default `marabrain-data`). Preserve this volume when replacing containers.
- Local health checks require a running Crystalline daemon on the same user/XDG
  paths. A responsive engine does not imply its embedding model is ready.
- Docker forwards `EMBEDDING_MODEL`, `EMBEDDING_PROVIDER_URL`, `EMBEDDING_DIMS`, and
  optional `EMBEDDING_API_KEY`. Remote settings are validated before daemon startup;
  dimensions are checked against a probe response, not sent as a request option.
  Keep API key values out of persisted YAML. Empty embedding settings preserve
  existing Crystalline configuration; changing models does not migrate vectors.
- The smoke client chooses `MARABRAIN_HOST_PORT`, then `MARABRAIN_PORT`, then 9110.

## ANTI-PATTERNS

- Do not print diagnostics to stdout in stdio mode; stdout carries MCP messages.
- Do not commit `.env` or include it in the Docker build context. Git permits
  `.env.example`; Docker excludes all `.env` variants, including the example.
- Do not replace foreground execution with a detached container plus log follower:
  that leaves the server running when the user presses Ctrl+C.
- Do not invoke the HTTP smoke script as part of the self-contained pytest suite.
- Do not assume legacy `Client.initialize()` works with modern FastMCP negotiation;
  the existing protocol test reads `client.server_info` after connecting.
