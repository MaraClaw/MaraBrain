# MaraBrain

🦞 Second Brain For OpenClaw-backed Agents.

Python MCP server named `marabrain-mcp`, built with FastMCP. The interface contains
six tools and no resources or prompts. `health` takes no arguments and returns
`{"status": "ok", "version": "0.4.6"}` when every resident workspace's
[Crystalline](https://github.com/jordiboehme/crystalline) engine answers
`crystalline ctl status` within one total health probe deadline. With zero resident
workspaces, the service is ready and `health` succeeds without starting an engine.
Missing executables, failed probes, and probes exceeding that shared deadline produce
an MCP tool error. Health does not assure embedding-model readiness.
The other five tools manage memories through the Crystalline CLI; see [Memory tools](#memory-tools).

## Memory tools

The tools run the pinned Crystalline 0.32.0 `crystalline --json` command without a shell,
send content on stdin, and return its JSON object unchanged (native fields, no added keys)
unless `read_memory` is called with `compress=true`, as described below.
All five memory tools require `workspace_id`: a strict, case-sensitive ASCII string
of 1-64 characters matching `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`. There is no default,
normalization, or adoption of legacy data. For example, `Team_A` and `team_a` are
different workspaces; missing IDs, non-string values, whitespace, dots, and Unicode
characters are rejected.

The calling backend must authenticate the tenant and authorize workspace membership
before supplying `workspace_id`. The ID routes storage and engine work; it is not
authorization. The shared HTTP bearer credential is only for a trusted backend that
enforces this contract, not for direct access by tenants.

The `crystalline` executable must be on `PATH`. Every activated workspace owns separate
HOME/XDG paths, configuration, database, control socket, domains, registry, and admission
state. In managed mode, its dedicated daemon starts lazily on the first memory request.
In external mode, a dedicated daemon must already be provisioned on that workspace's
paths; the base daemon is never used. The engine must be ready before registry
synchronization or memory commands in either mode. Each CLI command has a configurable
30-second default deadline and is never retried. Domain synchronization and registration
can add commands before a memory write.

Workspace activation synchronizes its persistent SQLite registry from
`crystalline --json domain list` in both engine modes and fails that activation if the
complete list is invalid or unavailable.
`create_memory` initializes and registers missing file domains before writing, using
`domain init <path> --name <name>` followed by `domain add <name> --path <path>`.
New names are lowercase ASCII, 1-64 characters, start with a letter or digit, and
contain only letters, digits, underscores, hyphens, and dots; `..` is rejected.
Existing exact Crystalline registration names and paths within that workspace are preserved. All memory tools
resolve case variants, canonical names, and effective aliases to the existing exact registration
name; ambiguous normalization collisions fail synchronization instead of merging domains.

`MARABRAIN_WORKSPACE_ROOT` optionally selects an absolute workspace root; its default is
the captured `$XDG_STATE_HOME/marabrain/workspaces`, using `~/.local/state` when
`XDG_STATE_HOME` is unset. Each workspace uses a hashed subdirectory token with an exact
identity record. Before external provisioning, call
`marabrain.workspace_storage.prepare_workspace(workspace_id, root)` as the server user
to bind the identity and create its private directories. Prepopulated directories without
an identity record are rejected. Do not construct token paths yourself: operators can use
`EngineContext.capture().for_workspace(workspace_id, root)` to obtain the captured path environment
for provisioning or administration with the same root as the server. Each workspace's
registry and automatic domain directories live in its own storage tree.

Directories must be private, writable, owned by the server user, and free of symlink
components. Use a local filesystem with POSIX locks. A short absolute workspace root
may be needed to fit the full Unix socket path within 103 bytes on macOS or 107 bytes
on Linux; overlong paths fail without fallback relocation. Docker persists the default
root beneath `/data`. Registry locks and domain admission are scoped to each workspace
and domain, so concurrent workspaces do not serialize each other's memory commands.
Application-wide process limits still apply across workspaces. A process lock serializes
registry and engine registration changes for servers sharing the same workspace paths.
Independent CLI/configuration writers do not participate in this lock and must not run concurrently.
Failed registration never proceeds to a memory write. A validated domain list reconciles
an uncertain add result; an actually registered domain can therefore appear in the registry
even when the original command reports failure. Reactivation after a restart also
reconciles that workspace's complete list.

| Tool | Arguments | Effect |
| --- | --- | --- |
| `create_memory` | `workspace_id`, `domain`, `title`, `content`, optional `tags` (list), `folder`, `metadata` (JSON object) | Creates a Markdown memory; an existing one is never overwritten |
| `read_memory` | `workspace_id`, `domain`, `permalink`; optional `compress` (boolean, default false), `query` (default null) | Returns full Markdown with frontmatter, `checksum`, links, observations, and relations; compression replaces returned `content` with distilled context and adds `compressed: true` |
| `edit_memory` | `workspace_id`, `domain`, `permalink`, `operation`, optional `expected_checksum` | Applies one edit operation |
| `delete_memory` | `workspace_id`, `domain`, `permalink` | Permanently deletes without confirmation; no checksum guard |
| `search_memories` | required `workspace_id`, nonempty `query` and nonempty `domains` (list); optional `tags` (list), `limit` (1-100, default 10), `page` (1 or more, default 1) | Searches the named domains in that workspace and returns native JSON unchanged |

`search_memories` requires both query text and at least one domain; a missing, null, empty,
or whitespace-only value is rejected before anything runs. Each hit includes the `domain` and
`permalink` to pass to `read_memory` with the same `workspace_id`.

`read_memory(workspace_id, domain, permalink, compress=false, query=None)` leaves normal reads unchanged:
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
`search_memories` does not accept compression arguments or call an LLM.

For example, pass these MCP arguments to `read_memory`:

```json
{"workspace_id": "team_a", "domain": "notes", "permalink": "team/plan", "compress": true, "query": "What is the current rollout plan?"}
```

Edit `operation` is an object whose `type` selects the fields:

| `type` | Fields besides `content` |
| --- | --- |
| `append`, `prepend` | none |
| `find_replace` | `find_text`; optional `expected_replacements` (0 or more) |
| `replace_section` | `section`; optional `include_subsections` (default false) |
| `insert_before_section`, `insert_after_section` | `section` |

```json
{"workspace_id": "team_a", "domain": "notes", "permalink": "team/plan", "expected_checksum": "<checksum from read_memory>",
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

### Breaking migration from shared storage

Existing clients must add a backend-authorized `workspace_id` to every memory-tool call.
Existing base HOME/XDG data, configuration, domain registrations, and databases remain
unassigned and unreachable through workspace tools. There is no default workspace,
legacy adoption, or automatic migration, and base configuration is not copied into
workspace runtimes.

Preserve and back up the old data. Determine tenant ownership before explicitly
migrating each tenant's memories, domains, and required configuration into that tenant's
workspace. Provision its dedicated engine using the workspace path environment and
verify its registrations and data before switching client traffic. Never attach the
shared base daemon or database to multiple workspaces. Shutdown reaps owned daemons
and leaves workspace data in place.

## Install and run locally

Use [uv](https://docs.astral.sh/uv/). The project requires Python 3.14.4 or newer;
`.python-version` pins the development interpreter.

Install Crystalline using its [upstream installation instructions](https://github.com/jordiboehme/crystalline#get-started),
then choose an engine mode. For local managed mode set `MARABRAIN_ENGINE_MODE=managed`;
MaraBrain lazily starts a dedicated daemon for each workspace's first memory request.
Docker uses managed mode by default. Managed mode requires exclusive control of each
workspace's HOME/XDG paths; do not share them with another engine or managed server.
The local default is external mode: preprovision and run a separate
`crystalline serve --http off` for every workspace after preparing its identity record,
with the environment from `EngineContext.capture().for_workspace(workspace_id, root)`,
using the same user as MaraBrain.
Starting a daemon with only the base HOME/XDG environment does not provision a workspace.

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
hosts to the hostname it forwards. Keep the shared bearer credential in the trusted
backend, which authenticates tenants and authorizes membership before choosing each
`workspace_id`. MaraBrain does not perform per-ID access control.
Rotate the credential by updating both the server and backend and restarting the server.

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
| `MARABRAIN_ALLOWED_DOMAINS` | unset | JSON array of permitted exact domain names, applied separately within every workspace; unset allows all, `[]` denies all memory operations |
| `MARABRAIN_WORKSPACE_ROOT` | captured `$XDG_STATE_HOME/marabrain/workspaces` | Optional absolute root for separate workspace storage trees |
| `MARABRAIN_HEALTH_TIMEOUT_SECONDS` | `2` | Positive finite total probe deadline in seconds across all resident workspace engines |
| `MARABRAIN_COMMAND_TIMEOUT_SECONDS` | `30` | Positive finite memory command deadline in seconds |
| `MARABRAIN_STARTUP_TIMEOUT_SECONDS` | unset (disabled) | Optional positive finite total budget in seconds for each workspace activation; empty also disables it |
| `MARABRAIN_OPERATION_TIMEOUT_SECONDS` | unset (disabled) | Optional positive finite total budget for each memory operation's activation wait and Crystalline phase in seconds; empty also disables it |
| `MARABRAIN_MAX_TRANSIENT_PROCESSES` | unset (disabled) | Optional application-wide integer capacity of at least 2 for transient engine commands across workspaces; empty also disables it |
| `MARABRAIN_MAX_CAPTURE_BYTES` | unset (disabled) | Optional positive integer ceiling on raw stdout bytes per captured child; empty also disables it |
| `MARABRAIN_MAX_TOTAL_CAPTURE_BYTES` | unset (disabled) | Optional strict positive integer application-wide budget for retained raw stdout payload across workspaces and data/control commands; empty also disables it |
| `MARABRAIN_MAX_WORKSPACE_TRANSIENT_PROCESSES` | unset (disabled) | Optional strict positive integer share of transient memory and registry processes one workspace may hold at once; empty also disables it |
| `MARABRAIN_MAX_WORKSPACE_CAPTURE_BYTES` | unset (disabled) | Optional strict positive integer share of retained raw stdout payload one workspace's memory and registry commands may hold at once; empty also disables it |
| `MARABRAIN_MAX_INPUT_BYTES` | unset (disabled) | Optional strict positive integer ceiling on one `create_memory` or `edit_memory` content, in UTF-8 bytes; empty also disables it |
| `MARABRAIN_MAX_TOTAL_INPUT_BYTES` | unset (disabled) | Optional strict positive integer application-wide budget of stdin bytes held by in-flight writes across workspaces; empty also disables it |
| `MARABRAIN_DOMAIN_COMMAND_CAPACITY` | unset (disabled) | Optional positive integer final-command capacity applied independently to every workspace and resolved domain pair; empty also disables it |
| `MARABRAIN_MAX_RESIDENT_WORKSPACES` | unset (disabled) | Optional strict positive integer cap on owned workspace runtimes in managed mode; empty also disables it; configured external mode is rejected |
| `MARABRAIN_ENGINE_MODE` | `external` | `external` or `managed`; Docker's default command selects `managed` |
| `MARABRAIN_ENGINE_STARTUP_TIMEOUT_SECONDS` | `30` | Positive finite managed engine readiness budget in seconds after daemon launch |

Within each workspace, domain admission checks both the supplied and resolved exact top-level names before mutations;
normalization never widens an allowlist. For a new mixed-case input, permit both that spelling
and its lowercase name, or use the lowercase spelling directly. Admission does not isolate files,
resolve aliases, redact linked metadata, or authorize workspace membership. A mixed allowed and denied search is
rejected in full. Docker's independent healthcheck has a three-second HTTP timeout
and five-second outer timeout regardless of the health probe deadline; client
timeouts and HTTP shutdown grace are separate limits too.

The two optional total budgets are independent. The startup total now applies to each
workspace activation and covers preparation, the workspace's existing-engine check,
embedding initialization, managed daemon launch and readiness, and initial domain
synchronization as applicable. It never limits the server's serving lifetime. Each
memory operation's total includes waiting for workspace activation, then domain
resolution, lock acquisition, domain synchronization and registration, and the final memory command. Compressed
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
or workspace reactivation reconciles uncertain domain registration before trusting cached routes.

Each application captures an immutable snapshot of its engine execution environment
at `run_application()` entry or standalone `create_server()` construction. Each workspace
derives its own HOME/XDG paths, configuration identity, registry, health checks,
initialization, daemon supervision, and memory tools from that snapshot and the captured
workspace root. Base captured `CRYSTALLINE_*` overrides, including `CRYSTALLINE_CONFIG`,
are removed from workspace runtimes; base configuration files are not copied. Later
process environment changes do not redirect an existing application. Initializer
credential overlays apply only to that workspace's owned daemon. External mode requires
the daemon provisioned for those workspace paths; a successful health probe does not
identify the external daemon's configuration.

The optional resource limits are independent and default to disabled. With
`MARABRAIN_MAX_TRANSIENT_PROCESSES=C`, each application shares C-1 slots for memory
and domain-registry commands and one reserved control slot for health, readiness,
or embedding initialization across all workspace activations and serving. Saturated lanes fail
immediately before launching a child or CLI worker; commands do not queue. Capacity
remains occupied until the child is reaped and its worker finishes. Separate
applications have independent capacity; persistent daemons and LLM calls are excluded.
Managed daemon count is independent of this transient limit. Without a resident cap,
it grows with activated workspaces. Account for each resident workspace's daemon memory
and embedding-model copies.

In managed mode, `MARABRAIN_MAX_RESIDENT_WORKSPACES=4` caps resident runtimes at four.
Starting, ready, and retiring runtimes occupy their slots until owned daemon and helper
cleanup finishes. A new workspace can retire the least recently used idle runtime;
if all slots are starting, leased, or retiring, the request fails immediately with
`Resident workspace capacity is busy.` There is no queue waiting for busy capacity.
Concurrent requests for the same activation share it, and canceling one caller does
not cancel startup for the others. All five memory tools lease the runtime through
registry work, native commands, and their cleanup. Recency records memory operations;
health pins only the resident snapshot and does not refresh recency or reactivate
retired workspaces. Unexpected daemon death remains fatal to the application.

Retirement stops only the owned runtime. Workspace identity, configuration, databases,
domain files, registry, and model cache remain on disk. A later request performs a
cold activation and complete domain synchronization before serving memory commands.
This can repeat embedding readiness work; the cap promises neither a memory ceiling
nor a latency bound. Compression releases the runtime before its LLM phase, and
application-owned compression flights and cached results survive retirement. Unset
or empty residency preserves existing unbounded behavior in either engine mode;
a configured cap requires `MARABRAIN_ENGINE_MODE=managed`.

`MARABRAIN_DOMAIN_COMMAND_CAPACITY` adds admission per workspace and resolved domain for the final create,
read, edit, delete, and search commands. For example, export
`MARABRAIN_DOMAIN_COMMAND_CAPACITY=2` to give every resolved domain in each workspace
its own capacity of two commands. Aliases and case variants within a workspace share
their resolved domain's capacity. The same domain name in another workspace has
independent counters.
A search reserves one permit per distinct resolved domain atomically; if any domain
is full, the whole search fails immediately without launching its final command.

Domain capacities do not enlarge or replace the global data and reserved control
lanes. Registry synchronization and registration remain outside domain quotas;
a refused final create can therefore leave a newly registered domain in place.
Permits remain occupied through child reaping and worker cleanup, including
cancellation and timeout. A compressed read releases its command permit after
source retrieval, before LLM work. Each application captures its own immutable
policy and independent counters; restart to change the policy. These quotas do
not authorize tenant membership. Unset or empty disables them;
malformed values, booleans, fractional values, and
nonpositive capacities fail configuration validation.

`MARABRAIN_MAX_CAPTURE_BYTES=S` limits raw stdout retained per captured child for
memory, registry, status and embedding initialization commands. Exactly S bytes can
succeed only after EOF, successful exit and response validation. The first excess
byte causes a fixed, redacted failure and owned-child cleanup; truncated JSON is
never returned and mutations are never retried. Retained raw stdout uses at most
S bytes plus fixed read overhead; parsed-object memory, daemon diagnostics and LLM
responses are excluded. Existing deadlines still apply. Leave either limit empty or
unset to preserve existing behavior; malformed, fractional, zero and negative
values are rejected, as is capacity below 2.

`MARABRAIN_MAX_TOTAL_CAPTURE_BYTES=T` independently limits logical retained raw
stdout payload across all captures in one application, including workspace memory
and registry commands, status/readiness probes, and embedding initialization.
Storage grows incrementally under this policy, so small responses do not reserve
the per-child ceiling. Each chunk reserves bytes atomically before retention;
the first chunk exceeding either configured byte limit fails immediately with the
same fixed capture-limit error. There is no waiting for bytes, truncation, or retry.
The reserved control process slot reserves no stdout bytes and can fail under
data-capture pressure. Separate applications have independent byte budgets.

Aliased views of one raw buffer count once. Its charge remains until child reaping,
worker joining or stream closure, and JSON parsing or discard have all finished.
Raw storage is cleared before its charge is released; initializer output is
released before daemon launch and serving. This is a retained-payload bound,
not an RSS or allocator guarantee: parsed objects, allocator slack, fixed read and
transport buffers, stdin, stderr, daemon diagnostics, and LLM responses are excluded.
Unset or empty preserves existing capture allocation behavior. Boolean, fractional,
malformed, zero, and negative aggregate budgets are rejected without exposing input.

`MARABRAIN_MAX_WORKSPACE_TRANSIENT_PROCESSES=P` and `MARABRAIN_MAX_WORKSPACE_CAPTURE_BYTES=B`
add independent per-workspace shares on top of the application limits, which keep applying.
For example, with `MARABRAIN_MAX_TRANSIENT_PROCESSES=9` and
`MARABRAIN_MAX_WORKSPACE_TRANSIENT_PROCESSES=3`, one workspace's memory and registry
commands hold at most three of the eight shared data slots, so a flooding tenant is
refused while other workspaces keep running. Likewise, one workspace's retained raw
stdout payload never exceeds B bytes, and it also counts against
`MARABRAIN_MAX_TOTAL_CAPTURE_BYTES` when that budget is set. Either share works
without the corresponding application limit.

Each command acquires its workspace share and the application limit together or not
at all: a refusal at either level leaves no charge at the other. Refusal is immediate
with the same fixed process-capacity or capture-limit error; there is no queue, and
the error names no workspace. Charges last until the child is reaped, its worker
has joined, and its output is parsed or discarded, including on timeout, cancellation,
and launch failure. Health, readiness, and embedding initialization keep using the
reserved control slot and are never charged to a workspace share. Shares are counted
per workspace ID within one application, so reactivating a retired workspace cannot
enlarge its share while earlier commands finish; separate applications keep independent
counters. Unset or empty disables each share and preserves existing behavior;
malformed, boolean, fractional, zero, and negative values are rejected without
exposing input.

`MARABRAIN_MAX_INPUT_BYTES=S` limits the UTF-8 size of the `content` sent on stdin by
`create_memory` and by every `edit_memory` operation. Exactly S bytes pass and S plus
one byte is refused; multibyte text is counted in encoded bytes, not characters. The
check runs before the workspace is leased, so a refused call runs no Crystalline command,
activates no workspace, and retires no other tenant's idle runtime.

`MARABRAIN_MAX_TOTAL_INPUT_BYTES=T` independently limits the stdin bytes held by
in-flight commands across all workspaces in one application. The encoded size is
reserved before the child or CLI worker is created and refused immediately, without
queuing, when it does not fit. It is released once, after the child is reaped and its
worker has joined, on success, nonzero exit, timeout, cancellation (including before the
child exists), and launch failure. Commands without stdin, such as reads, deletes,
searches, registry commands, and health or readiness probes, reserve nothing. Separate
applications have independent budgets. Both input limits are independent of the transient
process slots and the capture limits.

A write refused by either input limit is never dispatched, so the fixed error says so and
does not claim a mutation may have been applied; it carries no payload or size. Refusals are
logged with the `resource_limit` category, without arguments or content. Counts cover
stdin content only, not the request's other arguments, parsed objects, or allocator
overhead. Unset or empty preserves existing behavior; boolean, fractional, malformed,
zero, and negative values are rejected without exposing input.

Environment variables are read at startup. An originless MCP client is accepted;
browser clients must use an explicitly allowed origin. Avoid wildcard host or
origin entries. HTTP limits concurrent connections to 100, uses a 5-second
keep-alive timeout, and allows 5 seconds for graceful shutdown. Give your process
supervisor a longer termination grace period. Server errors are masked in MCP
responses; credentials are redacted from configuration errors.

## Docker

The multi-stage image uses `python:3.15.2-slim`, installs locked production
dependencies, and copies the Crystalline v0.32.0 binary from its official
multi-architecture image pinned by digest. It runs HTTP and lazily supervised
workspace Crystalline daemons as a non-root user. Copy `.env.example` to `.env`
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

Crystalline's own HTTP listener is disabled. Each workspace's configuration, database,
control socket, domains, registry, and model cache live in a separate storage tree.
The default root is `/data/state/marabrain/workspaces`, beneath `/data`, mounted from
the named Docker volume `marabrain-data`. Keep any custom container workspace root
under the persistent mount if its data must survive replacement. This volume survives
container replacement; back it up before removing it. Existing volumes must be writable
by UID/GID 65532. A managed server requires exclusive workspace HOME/XDG paths; do not
share the volume with another running engine or managed server. On first activation,
each workspace's Crystalline daemon may download its embedding model; subsequent
activations reuse that workspace's cache. Model storage grows with persisted workspaces;
daemon memory grows with resident workspaces. The optional resident cap retires idle
daemons without removing their data. Shutdown reaps owned daemons and preserves data. Health checks
all resident engines within one total deadline, not embedding-model readiness or
semantic-search quality. See the upstream [deployment guide](https://github.com/jordiboehme/crystalline/blob/v0.32.0/docs/deployment.md)
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
the managed Python process supplies the embedding key to each workspace's daemon environment.

During each workspace activation, before starting its managed Crystalline daemon,
bootstrap sends a small embedding request and checks its response and vector length.
Provider configuration and bootstrap errors that previously stopped engine startup
now surface on the first workspace activation, including authentication failures,
unreachable providers, and mismatched dimensions. Crystalline does
not support a `dimensions` request field, so choose a provider or model that
already returns the expected vector length.

After the check succeeds, activation merges provider settings into that workspace's
own Crystalline configuration, preserving its unrelated settings and domains.
It never rewrites the base `/data/config/crystalline/config.yaml` or copies base configuration.
The file stores the API key's environment variable name, never its value. The
remote provider avoids the default Granite embedding-model download. Restart the
container after changing these variables; the activation check does not continuously
monitor the provider, and MCP health checks only resident Crystalline engines.

Leaving all embedding variables unset or empty preserves each workspace's existing
Crystalline configuration and skips the remote-provider check. A fresh workspace uses the
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
| `LLM_COMPRESSION_CACHE_MAX_ENTRIES` | unset (disabled) | Positive integer retained entry limit applied independently to every workspace and resolved domain pair; requires the byte limit |
| `LLM_COMPRESSION_CACHE_MAX_BYTES` | unset (disabled) | Positive integer retained compressed UTF-8 payload limit applied independently to every workspace and resolved domain pair; requires the entry limit |

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

To reuse compressed output, set, for example,
`LLM_COMPRESSION_CACHE_MAX_ENTRIES=64` and `LLM_COMPRESSION_CACHE_MAX_BYTES=1048576`.
Every workspace and resolved domain pair independently uses these same limits to cache
output and coalesce matching concurrent provider requests. Cache keys, shared in-flight
requests, and quota buckets include both workspace and resolved domain. Every caller
still resolves its domain, passes domain admission, reads the source,
and validates its identity before reuse. A hit requires the same workspace, resolved domain,
permalink, exact query, and full source content under the application's captured
LLM settings. Each response uses that caller's fresh metadata and source checksum.
Aliases share only their resolved domain's entries within that workspace; plain reads and search remain
unchanged. Leaving both limits unset or empty disables caching and coalescing.
Otherwise both must be positive integers; a missing partner, malformed values,
and nonpositive, boolean, or fractional limits fail validation.

Each workspace and domain pair evicts least recently used entries to satisfy both limits. `LLM_COMPRESSION_CACHE_MAX_BYTES`
bounds only retained compressed UTF-8 payload, excluding cache keys, object
overhead, and in-flight provider data. Oversized results are returned without being
cached; failures and cancelled work are never cached. Cancelling one caller leaves
shared work available to remaining callers. When the last caller leaves or the
server shuts down, owned work is cancelled and drained; cleanup can outlast the
provider timeout and does not guarantee cancellation at the remote provider.
Late callers do not reset that timeout, and failures never fall back to source content.

Cached output and shared requests stay private to one application, workspace, and resolved
domain. Cached data is held in process memory, cleared on shutdown, and never
persisted. Restart to apply policy changes. A cache miss still sends the full source
content and query to the configured LLM provider; enabling caching does not change
that disclosure or replace backend tenant authorization.

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

`MARABRAIN_MAX_RESIDENT_WORKSPACES` is exported and forwarded identically to preflight
and runtime, including sourced `.env` overrides. Both launcher containers explicitly
use `MARABRAIN_ENGINE_MODE=managed`, including the alternate preflight command, so a
local `external` setting does not reject a valid Docker cap. Invalid caps fail preflight;
unset or empty caps preserve unbounded residency.

`MARABRAIN_MAX_TRANSIENT_PROCESSES`, `MARABRAIN_MAX_CAPTURE_BYTES`,
`MARABRAIN_MAX_TOTAL_CAPTURE_BYTES`, `MARABRAIN_MAX_WORKSPACE_TRANSIENT_PROCESSES`, and
`MARABRAIN_MAX_WORKSPACE_CAPTURE_BYTES` are also
exported and forwarded identically to both containers, including sourced `.env`
overrides. Both containers parse the same optional integer limits; empty or unset
values disable them, and invalid values fail configuration preflight.

`MARABRAIN_MAX_INPUT_BYTES` and `MARABRAIN_MAX_TOTAL_INPUT_BYTES` are exported and
forwarded identically to both containers, including sourced `.env` overrides; empty or
unset values disable them, and invalid values fail configuration preflight.

`MARABRAIN_DOMAIN_COMMAND_CAPACITY`, `LLM_COMPRESSION_CACHE_MAX_ENTRIES`, and
`LLM_COMPRESSION_CACHE_MAX_BYTES` are
exported and forwarded identically to configuration preflight and runtime,
including sourced `.env` overrides. Unset or empty capacity disables domain quotas;
both cache limits unset or empty disables caching. Invalid scalar values or an
incomplete cache pair fail preflight before runtime starts.

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
