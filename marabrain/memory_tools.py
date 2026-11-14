"""MCP tools that create, read, edit, delete, search, and traverse memories through Crystalline."""

import json
import logging
import time
from collections.abc import Callable
from functools import partial
from typing import assert_never

import anyio
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError

from marabrain.compression import compress_memory
from marabrain.compression_cache import CompressionCache
from marabrain.crystalline import JsonObject, run_json
from marabrain.deadlines import deadline_after
from marabrain.llm_config import LLMSettings
from marabrain.models import (
    AppendEdit,
    Checksum,
    Content,
    Domain,
    Depth,
    Domains,
    EditOperation,
    Families,
    FindReplaceEdit,
    Folder,
    InsertAfterSectionEdit,
    InsertBeforeSectionEdit,
    Limit,
    MaxRelated,
    Metadata,
    Page,
    Permalink,
    PrependEdit,
    Query,
    ReplaceSectionEdit,
    SetFrontmatterEdit,
    Status,
    Tags,
    Timeframe,
    Title,
    WorkspaceId,
)
from marabrain.transient import InputSizeError
from marabrain.workspaces import WorkspaceManager

_COMPRESSION_PROGRESS_INTERVAL = 5.0
_LOGGER = logging.getLogger(__name__)


class MemoryContent(BaseModel):
    """Identity and full Markdown required before external LLM disclosure."""

    model_config = ConfigDict(frozen=True, strict=True)
    domain: Domain
    permalink: Permalink
    content: StrictStr


def _edit_arguments(operation: EditOperation) -> list[str]:
    """Return the Crystalline operation name followed by its option flags."""
    match operation:
        case AppendEdit():
            return ["append"]
        case PrependEdit():
            return ["prepend"]
        case FindReplaceEdit(find_text=find_text, expected_replacements=expected):
            return ["find_replace", f"--find-text={find_text}", *([] if expected is None else [f"--expected-replacements={expected}"])]
        case ReplaceSectionEdit(section=section, include_subsections=include_subsections):
            return ["replace_section", f"--section={section}", *(["--include-subsections"] if include_subsections else [])]
        case InsertBeforeSectionEdit(section=section):
            return ["insert_before_section", f"--section={section}"]
        case InsertAfterSectionEdit(section=section):
            return ["insert_after_section", f"--section={section}"]
        case SetFrontmatterEdit(key=key, value=value, values=values):
            if key in {"tags", "sources"}:
                return ["set_frontmatter", f"--key={key}", f"--values={','.join(values)}" if values else "--values"]
            return ["set_frontmatter", f"--key={key}", *([] if value is None else [f"--value={value}"])]
    assert_never(operation)


def _check_input_size(operation: str, content: str, limit: int | None) -> None:
    """Refuse content over the per-command UTF-8 ceiling before any workspace is leased."""
    started = time.monotonic()
    if limit is not None and (len(content) > limit or len(content.encode("utf-8")) > limit):
        _LOGGER.error("crystalline.failure operation=%s category=resource_limit elapsed_ms=%.1f", operation, (time.monotonic() - started) * 1000)
        raise ToolError(str(InputSizeError()))


def _purge_on_dispatch(cache: CompressionCache | None, workspace_id: str, domain: str, permalink: str) -> Callable[[], None] | None:
    """Return the callback that drops a mutated memory's cached compressions once its command launches, or None without a cache."""
    return None if cache is None else partial(cache.purge, domain, permalink, workspace_id=workspace_id)


def register_memory_tools(
    server: FastMCP,
    manager: WorkspaceManager,
    command_timeout_seconds: float = 30.0,
    *,
    llm_settings: LLMSettings | None = None,
    operation_timeout_seconds: float | None = None,
    compression_cache: CompressionCache | None = None,
    max_input_bytes: int | None = None,
) -> None:
    """Register the eight memory tools bound to the application's workspace manager and its lifespan."""

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
    async def create_memory(
        workspace_id: WorkspaceId,
        domain: Domain,
        title: Title,
        content: Content,
        tags: Tags | None = None,
        folder: Folder | None = None,
        metadata: Metadata | None = None,
    ) -> JsonObject:
        """Create a new Markdown memory in a domain; an existing memory is never overwritten. Returns Crystalline's receipt, including its permalink.

        Write the body as a lead-in sentence plus bullets: `- [category] fact #tag` is an observation and
        `- rel_type [[Target Title]]` is a graph relation (quote a multi-word type: `- "relates to" [[Target]]`).
        A bare target resolves by title within the same domain; write `[[other-domain:Target Title]]` to cross domains.
        Relations make the memory reachable from its neighbours through build_memory_context.
        """
        _check_input_size("write", content, max_input_bytes)
        deadline = deadline_after(operation_timeout_seconds)
        manager.admit([domain])
        options = [
            *([] if tags is None else [f"--tags={','.join(tags)}"]),
            *([] if folder is None else [f"--folder={folder}"]),
            *([] if metadata is None else ["--metadata", json.dumps(metadata, allow_nan=False)]),
        ]
        async with manager.lease(workspace_id, deadline=deadline) as runtime:
            (resolved,) = await runtime.registry.resolve([domain], create=True, deadline=deadline)
            with runtime.admission.admit([resolved]):
                return await run_json(["write", resolved, title, *options], content, command_timeout_seconds, context=runtime.context, deadline=deadline)

    @server.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True})
    async def read_memory(
        workspace_id: WorkspaceId, domain: Domain, permalink: Permalink, ctx: Context, compress: bool = False, query: Query | None = None
    ) -> JsonObject:
        """Read one memory's full Markdown and metadata, or distill its content against a query.

        With compress=true, query is required and full content is sent to the configured LLM.
        The returned content is generated context marked compressed=true; checksum still identifies the original document.
        Compression errors fail the tool. Default reads return native JSON without contacting an LLM.
        """
        if compress and query is None:
            raise ToolError("A query is required when read compression is enabled.")
        deadline = deadline_after(operation_timeout_seconds)
        manager.admit([domain])
        async with manager.lease(workspace_id, deadline=deadline) as runtime:
            (resolved,) = await runtime.registry.resolve([domain], deadline=deadline)
            with runtime.admission.admit([resolved]):
                result = await run_json(
                    ["read", permalink, f"--domain={resolved}"], timeout_seconds=command_timeout_seconds, context=runtime.context, deadline=deadline
                )
        if compress and query is not None:
            settings = llm_settings if llm_settings is not None else LLMSettings()
            settings.require_configured()
            try:
                memory = MemoryContent.model_validate(result)
            except ValidationError:
                raise ToolError("Crystalline returned invalid memory content for compression.") from None
            if memory.domain != resolved or memory.permalink != permalink:
                raise ToolError("Crystalline returned a different memory for compression.")

            async def report_wait() -> None:
                """Report elapsed waiting time without claiming model completion progress."""
                started = anyio.current_time()
                while True:
                    await ctx.report_progress(anyio.current_time() - started, message="Waiting for LLM compression.")
                    await anyio.sleep(_COMPRESSION_PROGRESS_INTERVAL)

            compression: str | ToolError | None = None
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(report_wait)
                try:
                    if compression_cache is None:
                        compression = await compress_memory(settings, query, memory.content)
                    else:
                        compression = await compression_cache.get(resolved, permalink, query, memory.content, workspace_id=workspace_id)
                except ToolError as error:
                    compression = error
                finally:
                    tasks.cancel_scope.cancel()
            if isinstance(compression, ToolError):
                raise compression
            assert compression is not None
            return {**result, "content": compression, "compressed": True}
        return result

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False})
    async def edit_memory(
        workspace_id: WorkspaceId, domain: Domain, permalink: Permalink, operation: EditOperation, expected_checksum: Checksum | None = None
    ) -> JsonObject:
        """Edit an existing memory with one operation; expected_checksum from read_memory refuses the edit if the memory changed since.

        set_frontmatter assigns lifecycle fields without touching the body. To retire a fact that a newer memory replaces,
        set status to superseded, append a `- superseded_by [[New Title]]` relation, and optionally set valid_to;
        retired memories fade in search ranking and search_memories(status="stable") excludes them.
        """
        content = None if isinstance(operation, SetFrontmatterEdit) else operation.content
        if content is not None:
            _check_input_size("edit", content, max_input_bytes)
        deadline = deadline_after(operation_timeout_seconds)
        manager.admit([domain])
        checksum = [] if expected_checksum is None else [f"--expected-checksum={expected_checksum}"]
        name, *flags = _edit_arguments(operation)
        async with manager.lease(workspace_id, deadline=deadline) as runtime:
            (resolved,) = await runtime.registry.resolve([domain], deadline=deadline)
            with runtime.admission.admit([resolved]):
                return await run_json(
                    ["edit", permalink, resolved, name, *flags, *checksum],
                    content,
                    command_timeout_seconds,
                    context=runtime.context,
                    deadline=deadline,
                    on_dispatch=_purge_on_dispatch(compression_cache, workspace_id, resolved, permalink),
                )

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False})
    async def delete_memory(workspace_id: WorkspaceId, domain: Domain, permalink: Permalink) -> JsonObject:
        """Permanently delete one memory without confirmation or checksum protection."""
        deadline = deadline_after(operation_timeout_seconds)
        manager.admit([domain])
        async with manager.lease(workspace_id, deadline=deadline) as runtime:
            (resolved,) = await runtime.registry.resolve([domain], deadline=deadline)
            with runtime.admission.admit([resolved]):
                return await run_json(
                    ["delete", permalink, resolved, "--force"],
                    timeout_seconds=command_timeout_seconds,
                    context=runtime.context,
                    deadline=deadline,
                    on_dispatch=_purge_on_dispatch(compression_cache, workspace_id, resolved, permalink),
                )

    @server.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
    async def search_memories(
        workspace_id: WorkspaceId,
        query: Query,
        domains: Domains,
        tags: Tags | None = None,
        limit: Limit = 10,
        page: Page = 1,
        status: Status | None = None,
    ) -> JsonObject:
        """Search memories in one or more domains for nonempty query text, optionally narrowed by tags and one status.

        Hits give the domain and permalink for read_memory. Pass status="stable" for what is true now; it also matches legacy "current".
        """
        deadline = deadline_after(operation_timeout_seconds)
        manager.admit(domains)
        async with manager.lease(workspace_id, deadline=deadline) as runtime:
            resolved = await runtime.registry.resolve(domains, deadline=deadline)
            filters = [
                *(f"--domain={domain}" for domain in resolved),
                *(f"--tag={tag}" for tag in tags or []),
                *([] if status is None else [f"--status={status}"]),
            ]
            with runtime.admission.admit(resolved):
                return await run_json(
                    ["search", *filters, f"--limit={limit}", f"--page={page}", query],
                    timeout_seconds=command_timeout_seconds,
                    context=runtime.context,
                    deadline=deadline,
                )

    @server.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
    async def build_memory_context(
        workspace_id: WorkspaceId,
        domain: Domain,
        permalink: Permalink,
        domains: Domains | None = None,
        depth: Depth | None = None,
        max_related: MaxRelated | None = None,
        timeframe: Timeframe | None = None,
    ) -> JsonObject:
        """Return the graph neighbourhood around one memory: nodes (domain, permalink, title, type) and typed edges, without content.

        Traversal follows relations and links in both directions, ranked by connection strength, and stays within the anchor's
        domain plus any extra domains listed. Read the nodes you need with read_memory. Depth defaults to 1 hop, at most 3.
        """
        deadline = deadline_after(operation_timeout_seconds)
        requested: list[str] = list(dict.fromkeys([domain, *(domains or [])]))
        manager.admit(requested)
        options = [
            *([] if depth is None else [f"--depth={depth}"]),
            *([] if max_related is None else [f"--max-related={max_related}"]),
            *([] if timeframe is None else [f"--timeframe={timeframe}"]),
        ]
        async with manager.lease(workspace_id, deadline=deadline) as runtime:
            resolved = list(dict.fromkeys(await runtime.registry.resolve(requested, deadline=deadline)))
            with runtime.admission.admit(resolved):
                return await run_json(
                    ["context", f"crystalline://{resolved[0]}/{permalink}", *(f"--domain={name}" for name in resolved), *options],
                    timeout_seconds=command_timeout_seconds,
                    context=runtime.context,
                    deadline=deadline,
                )

    @server.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
    async def review_memories(workspace_id: WorkspaceId, domains: Domains, families: Families | None = None, limit: Limit = 10, page: Page = 1) -> JsonObject:
        """Sweep domains read-only for the maintenance their memories need and return Crystalline's ranked queue.

        Findings include unresolved links, current memories that still reference retired ones, retirements without a successor,
        expired validity, orphans, and near-duplicates, each with evidence and a suggested fix. The sweep changes nothing.
        """
        deadline = deadline_after(operation_timeout_seconds)
        manager.admit(domains)
        async with manager.lease(workspace_id, deadline=deadline) as runtime:
            resolved = list(dict.fromkeys(await runtime.registry.resolve(domains, deadline=deadline)))
            filters = [*(f"--domain={name}" for name in resolved), *(f"--family={family}" for family in families or [])]
            with runtime.admission.admit(resolved):
                return await run_json(
                    ["evolve", *filters, f"--limit={limit}", f"--page={page}"],
                    timeout_seconds=command_timeout_seconds,
                    context=runtime.context,
                    deadline=deadline,
                )

    @server.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
    async def get_memory_vocabulary(workspace_id: WorkspaceId, domain: Domain) -> JsonObject:
        """List the tags, observation categories, relation types, types, and statuses a domain uses, with counts; reuse them before coining new terms."""
        deadline = deadline_after(operation_timeout_seconds)
        manager.admit([domain])
        async with manager.lease(workspace_id, deadline=deadline) as runtime:
            (resolved,) = await runtime.registry.resolve([domain], deadline=deadline)
            with runtime.admission.admit([resolved]):
                return await run_json(
                    ["vocabulary", f"--domain={resolved}"], timeout_seconds=command_timeout_seconds, context=runtime.context, deadline=deadline
                )
