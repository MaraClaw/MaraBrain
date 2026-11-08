"""MCP tools that create, read, edit, delete, and search memories through Crystalline."""

import json
from typing import assert_never

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError

from marabrain.compression import compress_memory
from marabrain.crystalline import JsonObject, run_json
from marabrain.domain_registry import DomainRegistry
from marabrain.llm_config import LLMSettings
from marabrain.models import (
    AppendEdit,
    Checksum,
    Content,
    Domain,
    Domains,
    EditOperation,
    FindReplaceEdit,
    Folder,
    InsertAfterSectionEdit,
    InsertBeforeSectionEdit,
    Limit,
    Metadata,
    Page,
    Permalink,
    PrependEdit,
    Query,
    ReplaceSectionEdit,
    Tags,
    Title,
)


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
    assert_never(operation)


def register_memory_tools(
    server: FastMCP, allowed_domains: frozenset[str] | None = None, command_timeout_seconds: float = 30.0, *, llm_settings: LLMSettings | None = None
) -> None:
    """Register the five Crystalline-backed memory tools on the server."""

    registry = DomainRegistry.from_environment(command_timeout_seconds, allowed_domains)

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
    async def create_memory(
        domain: Domain, title: Title, content: Content, tags: Tags | None = None, folder: Folder | None = None, metadata: Metadata | None = None
    ) -> JsonObject:
        """Create a new Markdown memory in a domain; an existing memory is never overwritten. Returns Crystalline's receipt, including its permalink."""
        (resolved,) = await registry.resolve([domain], create=True)
        options = [
            *([] if tags is None else [f"--tags={','.join(tags)}"]),
            *([] if folder is None else [f"--folder={folder}"]),
            *([] if metadata is None else ["--metadata", json.dumps(metadata, allow_nan=False)]),
        ]
        return await run_json(["write", resolved, title, *options], content, command_timeout_seconds)

    @server.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True})
    async def read_memory(domain: Domain, permalink: Permalink, compress: bool = False, query: Query | None = None) -> JsonObject:
        """Read one memory's full Markdown and metadata, or distill its content against a query.

        With compress=true, query is required and full content is sent to the configured LLM.
        The returned content is generated context marked compressed=true; checksum still identifies the original document.
        Compression errors fail the tool. Default reads return native JSON without contacting an LLM.
        """
        if compress and query is None:
            raise ToolError("A query is required when read compression is enabled.")
        (resolved,) = await registry.resolve([domain])
        result = await run_json(["read", permalink, f"--domain={resolved}"], timeout_seconds=command_timeout_seconds)
        if compress and query is not None:
            settings = llm_settings if llm_settings is not None else LLMSettings()
            settings.require_configured()
            try:
                memory = MemoryContent.model_validate(result)
            except ValidationError:
                raise ToolError("Crystalline returned invalid memory content for compression.") from None
            if memory.domain != resolved or memory.permalink != permalink:
                raise ToolError("Crystalline returned a different memory for compression.")
            context = await compress_memory(settings, query, memory.content)
            return {**result, "content": context, "compressed": True}
        return result

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False})
    async def edit_memory(domain: Domain, permalink: Permalink, operation: EditOperation, expected_checksum: Checksum | None = None) -> JsonObject:
        """Edit an existing memory with one operation; expected_checksum from read_memory refuses the edit if the memory changed since."""
        (resolved,) = await registry.resolve([domain])
        checksum = [] if expected_checksum is None else [f"--expected-checksum={expected_checksum}"]
        name, *flags = _edit_arguments(operation)
        return await run_json(["edit", permalink, resolved, name, *flags, *checksum], operation.content, command_timeout_seconds)

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False})
    async def delete_memory(domain: Domain, permalink: Permalink) -> JsonObject:
        """Permanently delete one memory without confirmation or checksum protection."""
        (resolved,) = await registry.resolve([domain])
        return await run_json(["delete", permalink, resolved, "--force"], timeout_seconds=command_timeout_seconds)

    @server.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
    async def search_memories(query: Query, domains: Domains, tags: Tags | None = None, limit: Limit = 10, page: Page = 1) -> JsonObject:
        """Search memories in one or more domains for nonempty query text, optionally narrowed by tags. Hits give the domain and permalink for read_memory."""
        resolved = await registry.resolve(domains)
        filters = [*(f"--domain={domain}" for domain in resolved), *(f"--tag={tag}" for tag in tags or [])]
        return await run_json(["search", *filters, f"--limit={limit}", f"--page={page}", query], timeout_seconds=command_timeout_seconds)
