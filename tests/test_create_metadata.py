"""MCP boundary coverage for create_memory metadata forwarding."""

import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError, ValidationError
from memory_fake import call, calls
from pydantic import TypeAdapter

from marabrain.config import Settings
from marabrain.server import create_server


@pytest.mark.parametrize("metadata", [{}, {"label": "Grüße 🌍", "nested": {"items": [1, True, None, "a\x00b"]}}])
async def test_create_forwards_metadata_as_one_json_object(cli: Path, metadata: dict[str, object]) -> None:
    """Preserve empty and nested objects, including Unicode and escaped NUL, in one CLI argument."""
    await call("create_memory", {"domain": "notes", "title": "Title", "content": "Body", "metadata": metadata})
    (record,) = calls(cli)
    argv = TypeAdapter(list[str]).validate_python(record["argv"])
    assert argv[:5] == ["--json", "write", "notes", "Title", "--metadata"]
    assert len(argv) == 6
    assert json.loads(argv[5]) == metadata


@pytest.mark.parametrize("metadata", ["{}", [], 1])
async def test_create_rejects_invalid_metadata_without_executing(cli: Path, metadata: object) -> None:
    """Reject non-objects and non-JSON numbers at the MCP tool boundary."""
    with pytest.raises(ToolError):
        await call("create_memory", {"domain": "notes", "title": "Title", "content": "Body", "metadata": metadata})
    assert calls(cli) == []


@pytest.mark.parametrize("metadata", [{"nested": float("nan")}, {"nested": float("inf")}, {"nested": "\ud800"}])
async def test_create_rejects_non_json_python_values(cli: Path, metadata: dict[str, object]) -> None:
    """Reject invalid numbers and Unicode even when an in-process caller bypasses JSON transport."""
    tool = await create_server(Settings()).get_tool("create_memory")
    assert tool is not None
    with pytest.raises(ValidationError):
        await tool.run({"domain": "notes", "title": "Title", "content": "Body", "metadata": metadata})
    assert calls(cli) == []
