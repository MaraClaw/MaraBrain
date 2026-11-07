#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14.4"
# dependencies = ["fastmcp==4.1.6", "mcp>=2.3.0", "anyio>=4.16.0", "python-dotenv>=1.2.4", "pydantic>=2.14.3"]
# ///
# How to run: uv run --no-sync python test_health.py

"""Smoke check for a running local MaraBrain HTTP MCP server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import anyio
from dotenv import load_dotenv
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp import MCPError
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class HealthResult(BaseModel):
    """Validated health response with an ok status and a nonempty version."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"]
    version: str = Field(min_length=1)


async def main() -> None:
    """Call the local HTTP health tool and print its validated response."""
    load_dotenv(Path(__file__).with_name(".env"))
    secret = os.environ.get("MARABRAIN_BEARER_SECRET")
    if not secret:
        raise SystemExit("Set MARABRAIN_BEARER_SECRET in .env or the environment.")
    port = os.environ.get("MARABRAIN_HOST_PORT", os.environ.get("MARABRAIN_PORT", "9110"))
    url = f"http://127.0.0.1:{port}/mcp"
    async with Client(url, auth=secret, timeout=10) as client:
        result = await client.call_tool("health", {})
        health = HealthResult.model_validate(result.structured_content)
    print(f"PASS {url}")
    print(health.model_dump_json(indent=2))


if __name__ == "__main__":
    try:
        anyio.run(main)
    except (MCPError, RuntimeError, TimeoutError, ToolError, ValidationError) as error:
        raise SystemExit(f"Health check failed: {error}") from None
