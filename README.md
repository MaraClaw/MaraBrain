# MaraBrain

🦞 Second Brain For OpenClaw-backed Agents.

Python MCP server named `marabrain-mcp`, built with FastMCP. The interface contains
six tools and no resources or prompts. `health` returns `{"status": "ok", "version": "0.4.0"}`
only after the [Crystalline](https://github.com/jordiboehme/crystalline) background engine
answers `crystalline ctl status`. Missing executables, failed probes, and probes taking
longer than the configured health deadline produce an MCP tool error instead of a successful response.
The other five tools manage memories through the Crystalline CLI; see [Memory tools](#memory-tools).

## Memory tools

The tools run the pinned Crystalline 0.31.0 `crystalline --json` command without a shell,
send content on stdin, and return its JSON object unchanged (native fields, no added keys)
unless `read_memory` is called with `compress=true`, as described below.
The `crystalline` executable must be on `PATH`, using the same HOME/XDG configuration
as MaraBrain. `health` also requires its daemon to be running; the Docker image manages
both processes. Each CLI command has a configurable 30-second default deadline and
is never retried. Domain synchronization and registration can add commands before a memory write.

Startup synchronizes a persistent SQLite registry from `crystalline --json domain list`
in both engine modes and fails before serving if the complete list is invalid or unavailable.
`create_memory` initializes and registers missing file domains before writing, using
`domain init <path> --name <name>` followed by `domain add <name> --path <path>`.
New names are lowercase ASCII, 1-64 characters, start with a letter or digit, and
contain only letters, digits, underscores, hyphens, and dots; `..` is rejected.
Existing exact Crystalline registration names and paths are preserved. All memory tools
resolve case variants, canonical names, and effective aliases to the existing exact registration
name; ambiguous normalization collisions fail synchronization instead of merging domains.

The registry is `$XDG_STATE_HOME/marabrain/domains.sqlite3`, and automatic domains live
in sibling `domains/<name>` directories. Without `XDG_STATE_HOME`, local storage defaults
to `~/.local/state/marabrain`. Storage must be writable, owned by the server user, and
free of symlink components. Docker already persists this directory under `/data/state`.
A process lock serializes registry and engine registration changes across MaraBrain servers
sharing these paths; use the same HOME/XDG paths and a local filesystem with POSIX locks.
Independent CLI/configuration writers do not participate in this lock and must not run concurrently.
Failed registration never proceeds to a memory write. A validated domain list reconciles
an uncertain add result; an actually registered domain can therefore appear in the registry
even when the original command reports failure. Restart also reconciles the complete list.

| Tool | Arguments | Effect |
| --- | --- | --- |
| `create_memory` | `domain`, `title`, `content`, optional `tags` (list), `folder`, `metadata` (JSON object) | Creates a Markdown memory; an existing one is never overwritten |
| `read_memory` | `domain`, `permalink`; optional `compress` (boolean, default false), `query` (default null) | Returns full Markdown with frontmatter, `checksum`, links, observations, and relations; compression replaces returned `content` with distilled context and adds `compressed: true` |
| `edit_memory` | `domain`, `permalink`, `operation`, optional `expected_checksum` | Applies one edit operation |
| `delete_memory` | `domain`, `permalink` | Permanently deletes without confirmation; no checksum guard |
| `search_memories` | required nonempty `query` and nonempty `domains` (list); optional `tags` (list), `limit` (1-100, default 10), `page` (1 or more, default 1) | Searches the named domains and returns native JSON unchanged |

`search_memories` requires both query text and at least one domain; a missing, null, empty,
or whitespace-only value is rejected before anything runs. Each hit includes the `domain` and
`permalink` to pass to `read_memory`.

`read_memory(domain, permalink, compress=false, query=None)` leaves normal reads unchanged:
with `compress=false`, it returns native JSON and full content without calling an LLM.
With `compress=true`, a nonempty, non-whitespace `query` is required. The
[configured LLM provider](#optional-read-compression) distills the full memory against
that query; the response replaces `content` with the distilled context and adds
`compressed: true`, preserving the other fields. The original `checksum` remains the
source memory's checksum, not a checksum of the distilled context, and can still be
used as `expected_checksum` for an edit.

Only `compress=true` discloses the full memory content and query to the chosen LLM
provider. Compression never writes or modifies the source memory. It can reduce context
passed to the calling agent, but does not reduce storage read I/O. Read or provider
errors surface as tool errors; there is no fallback to uncompressed content.
`search_memories` is unchanged and does not accept compression arguments or call an LLM.

For example, pass these MCP arguments to `read_memory`:

```json
{"domain": "notes", "permalink": "team/plan", "compress": true, "query": "What is the current rollout plan?"}
```

Edit `operation` is an object whose `type` selects the fields:

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
that read; without it the last write wins. Explicit domain administration, metadata editing, and
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
For local managed mode set `MARABRAIN_ENGINE_MODE=managed`; it requires exclusive
control of its HOME and XDG state paths. Do not share them with another engine or server.

```sh
uv sync --locked
uv run --no-sync marabrain
```

The default transport is stdio. Your MCP client starts the process and owns its
lifecycle. `uv run --no-sync python -m marabrain` is an equivalent entry point.
Protocol messages use stdout; diagnostics use stderr.

Set `MARABRAIN_LOG_LEVEL=debug` to trace command starts, or `info` (the default)
to record successful completions. Failures are logged at `error` with a fixed
category, operation, elapsed time, and exit code when available. Arguments,
memory contents, CLI output, and raw engine error details are suppressed, so
these logs alone may not identify the underlying engine failure. FastMCP debug
logging is not enabled because it can expose tool arguments. For example:

```sh
MARABRAIN_LOG_LEVEL=debug uv run --no-sync marabrain
```

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
| `MARABRAIN_BEARER_SECRET_FILE` | unset | UTF-8 file alternative to the bearer secret |
| `MARABRAIN_ALLOWED_HOSTS` | `["127.0.0.1","localhost","[::1]"]` | JSON array of accepted HTTP hosts |
| `MARABRAIN_ALLOWED_ORIGINS` | `[]` | JSON array of accepted browser origins |
| `MARABRAIN_ALLOWED_DOMAINS` | unset | JSON array of permitted exact domain names; unset allows all, `[]` denies all memory operations |
| `MARABRAIN_HEALTH_TIMEOUT_SECONDS` | `2` | Positive finite health probe deadline in seconds |
| `MARABRAIN_COMMAND_TIMEOUT_SECONDS` | `30` | Positive finite memory command deadline in seconds |
| `MARABRAIN_STARTUP_TIMEOUT_SECONDS` | unset (disabled) | Optional positive finite total startup preparation budget in seconds; empty also disables it |
| `MARABRAIN_OPERATION_TIMEOUT_SECONDS` | unset (disabled) | Optional positive finite total budget for each memory operation's Crystalline phase in seconds; empty also disables it |
| `MARABRAIN_ENGINE_MODE` | `external` | `external` or `managed`; Docker's default command selects `managed` |
| `MARABRAIN_ENGINE_STARTUP_TIMEOUT_SECONDS` | `30` | Positive finite managed engine readiness budget in seconds after daemon launch |

Domain admission checks both the supplied and resolved exact top-level names before mutations;
normalization never widens an allowlist. For a new mixed-case input, permit both that spelling
and its lowercase name, or use the lowercase spelling directly. Admission does not isolate files,
resolve aliases, or redact linked metadata. A mixed allowed and denied search is
rejected in full. Docker's independent healthcheck has a three-second HTTP timeout
and five-second outer timeout regardless of the health probe deadline; client
timeouts and HTTP shutdown grace are separate limits too.

The two optional total budgets are independent. Startup's total begins before
preparation and covers the existing-engine check, embedding initialization, managed
daemon launch and readiness, and initial domain synchronization as applicable. It
ends before serving starts and never limits the server's serving lifetime. Each
memory operation's total begins before domain resolution and spans lock acquisition,
domain synchronization and registration, and the final memory command. Compressed
reads then use their separate `LLM_TIMEOUT_SECONDS` budget for LLM compression.
Existing command, health, and engine readiness caps still apply with their unchanged
defaults; a total budget can shorten a step's available time but never extends its cap.
For example, set `MARABRAIN_STARTUP_TIMEOUT_SECONDS=45` and
`MARABRAIN_OPERATION_TIMEOUT_SECONDS=10` to enable both totals, or leave either empty
or unset to disable it. Zero, negative, nonfinite, and malformed values are rejected.

After expiry is observed, no further Crystalline command is dispatched. Cancellation
and cleanup still reap owned children, release locks, and close SQLite connections,
which may outlast the budget; these settings do not promise an exact response deadline.
Mutations with uncertain outcomes are never retried automatically. The next operation
or startup reconciles uncertain domain registration before trusting cached routes.

Each application captures an immutable snapshot of its engine execution environment
at `run_application()` entry or standalone `create_server()` construction. Registry
paths and configuration identity, health checks, initialization, daemon supervision,
and memory tools use that snapshot, including HOME/XDG defaults and supported
`CRYSTALLINE_CONFIG` selection. Later process environment changes do not redirect an
existing application. Initializer credential overlays apply only to that application's
owned daemon. External mode retains its captured CLI environment; a successful health
probe does not identify the external daemon's configuration.

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
Press `Ctrl+C` to stop the server and remove its container; the named data volume
is preserved. It uses a read-only root
filesystem, a temporary `/tmp`, dropped Linux capabilities, and an authenticated
MCP healthcheck. The secret is passed at runtime and is never baked into the
image. Alternatively set `MARABRAIN_BEARER_SECRET_FILE` to an absolute host file
path. It is mounted read-only at `/run/secrets/marabrain_bearer` in both containers
and must be readable by UID/GID 65532. Host credential paths must not contain commas.
Docker administrators can inspect container environment variables; keep
access to the Docker daemon restricted.

Crystalline's own HTTP listener is disabled. Its configuration, database, control
socket, and model cache live under `/data`, mounted from the named Docker volume
`marabrain-data`. This volume survives container replacement; back it up before
removing it. Existing volumes must be writable by UID/GID 65532. A managed server
requires exclusive HOME/XDG paths; do not share the volume with another running
engine or managed server. On first startup,
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
| `EMBEDDING_API_KEY_FILE` | Absolute host file path alternative, mounted read-only at `/run/secrets/embedding_api_key` |

For each credential, specify either its raw variable or its file variable, not
both nonempty. Files must be readable by UID/GID 65532 and contain a nonempty
UTF-8 value without embedded line breaks or NUL; one terminal LF or CRLF is
removed. Values are resolved at startup, so restart after rotating a file. File
contents are not placed in Docker arguments, configured environment, or YAML;
the managed Python process supplies the embedding key to its daemon environment.

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

Leaving all embedding variables unset or empty preserves the existing Crystalline
configuration and skips the remote-provider check. A fresh volume then uses the
default local model. Changing providers or models does not automatically migrate
existing vectors; follow Crystalline's indexing guidance before reusing an index
with a different embedding space.

### Optional read compression

Configure a text-generation model before calling `read_memory` with `compress=true`.
Set these variables in `.env` for Docker, or export them for local startup. Generation
settings are separate from embedding settings; an embedding model cannot generate context.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `LLM_MODEL` | unset | Text-generation model accepted by the chosen provider |
| `LLM_PROVIDER_URL` | unset | API base URL: include `/v1` for OpenAI; use the gateway root for the Claude Agent SDK |
| `LLM_API_KEY` | unset | Provider credential; required for Anthropic, optional only for keyless OpenAI-compatible hosts |
| `LLM_PROVIDER` | `openai` | `openai` (including compatible hosts) or `anthropic` |
| `LLM_REASONING_EFFORT` | unset | Optional `low`, `medium`, or `high`; requires model support |
| `LLM_TEMPERATURE` | unset | Optional OpenAI sampling temperature from 0 through 2; rejected for Claude |
| `LLM_MAX_INPUT_TOKENS` | `256000` | Claude Agent SDK's assumed context window; OpenAI uses the selected model's context window |
| `LLM_MAX_TOKENS` | `32768` | Maximum generated output tokens |
| `LLM_TIMEOUT_SECONDS` | `60` | Provider request timeout in seconds |

For OpenAI, for example, set `LLM_PROVIDER=openai`, `LLM_MODEL=gpt-4.1-mini`,
and `LLM_PROVIDER_URL=https://api.openai.com/v1`, then supply your `LLM_API_KEY`.
For Anthropic, set `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-sonnet-4-20250514`,
and `LLM_PROVIDER_URL=https://api.anthropic.com`, then supply your Anthropic key.
Use a model available to your account and omit sampling or effort settings it does not
support. OpenAI uses the asynchronous OpenAI SDK with a structured response schema.
Claude uses the Claude Agent SDK with structured output, disabled ordinary tools,
and isolated temporary configuration. Claude reasoning effort uses adaptive thinking
and requires a model that supports it. Leave `LLM_TEMPERATURE` empty for Claude;
the Agent SDK does not expose temperature.
Some models reject sampling or effort even within the configured ranges.

The 256K setting means 256,000 tokens and maps to Claude Code's
`CLAUDE_CODE_MAX_CONTEXT_TOKENS`. It is a context-window hint, not a hard input
token check before dispatch. OpenAI-compatible Chat Completions has no equivalent
request parameter, so the provider's model limit applies. `LLM_MAX_TOKENS` is a
separate output budget; Claude's agent may make multiple requests to produce the
structured result.

The launcher exports and forwards every `LLM_*` variable listed above to both the
configuration preflight and runtime container. Settings are parsed at startup; restart
after changing them. LLM settings and credentials are not persisted in Crystalline
configuration or baked into the image. Configuration validation does not verify live
provider compatibility or credentials.

Override `MARABRAIN_IMAGE`, `MARABRAIN_CONTAINER`, `MARABRAIN_HOST_PORT`, or
`MARABRAIN_DATA_VOLUME` to choose an image tag, container name, local port, or
persistent volume. The host, origin and domain allowlists and timeout settings
from the table above and `MARABRAIN_LOG_LEVEL` are also forwarded. The host port remains bound to loopback;
put a TLS reverse proxy in front for remote access. The script validates the
configuration before starting the service and never replaces an existing
container automatically.

Both optional total-budget variables are exported and forwarded identically to the
configuration preflight and runtime containers. Empty or unset values leave the
corresponding total disabled in both containers.

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

The script reads the bearer secret or bearer secret file path from `.env` beside the script or the exported
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
