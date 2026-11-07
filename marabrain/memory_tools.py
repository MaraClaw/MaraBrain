"""MCP tools that create, read, edit, delete, and search memories through Crystalline."""

from typing import assert_never

from fastmcp import FastMCP

from marabrain.crystalline import JsonObject, run_json
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
    Page,
    Permalink,
    PrependEdit,
    Query,
    ReplaceSectionEdit,
    Tags,
    Title,
)


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


def register_memory_tools(server: FastMCP) -> None:
    """Register the five Crystalline-backed memory tools on the server."""

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
    async def create_memory(domain: Domain, title: Title, content: Content, tags: Tags | None = None, folder: Folder | None = None) -> JsonObject:
        """Create a new Markdown memory in a domain; an existing memory is never overwritten. Returns Crystalline's receipt, including its permalink."""
        options = [*([] if tags is None else [f"--tags={','.join(tags)}"]), *([] if folder is None else [f"--folder={folder}"])]
        return await run_json(["write", domain, title, *options], content)

    @server.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
    async def read_memory(domain: Domain, permalink: Permalink) -> JsonObject:
        """Read one memory by domain and permalink: full Markdown with frontmatter, checksum, links, observations, and relations."""
        return await run_json(["read", permalink, f"--domain={domain}"])

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False})
    async def edit_memory(domain: Domain, permalink: Permalink, operation: EditOperation, expected_checksum: Checksum | None = None) -> JsonObject:
        """Edit an existing memory with one operation; expected_checksum from read_memory refuses the edit if the memory changed since."""
        checksum = [] if expected_checksum is None else [f"--expected-checksum={expected_checksum}"]
        name, *flags = _edit_arguments(operation)
        return await run_json(["edit", permalink, domain, name, *flags, *checksum], operation.content)

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False})
    async def delete_memory(domain: Domain, permalink: Permalink) -> JsonObject:
        """Permanently delete one memory without confirmation or checksum protection."""
        return await run_json(["delete", permalink, domain, "--force"])

    @server.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
    async def search_memories(query: Query, domains: Domains, tags: Tags | None = None, limit: Limit = 10, page: Page = 1) -> JsonObject:
        """Search memories in one or more domains for nonempty query text, optionally narrowed by tags. Hits give the domain and permalink for read_memory."""
        filters = [*(f"--domain={domain}" for domain in domains), *(f"--tag={tag}" for tag in tags or [])]
        return await run_json(["search", *filters, f"--limit={limit}", f"--page={page}", query])
