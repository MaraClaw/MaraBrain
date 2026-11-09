"""Shared fake crystalline executable and helpers for the memory tool tests."""

import json
import sys
from pathlib import Path
from typing import Final

from fastmcp import Client

from marabrain.config import Settings
from marabrain.domain_registry import DomainRegistry
from marabrain.engine_context import EngineContext
from marabrain.server import create_server
from marabrain.workspace_storage import prepare_workspace

WORKSPACE_ID: Final = "test-workspace"
WORKSPACE_ACTIVATION: Final = [["ctl", "status"], ["--json", "domain", "list"]]
NOISE = "stderr-noise"
RECEIPT: dict[str, object] = {"action": "created", "hits": [{"score": 0.5, "tags": ["a", None], "nested": {"ok": True}}], "total": 1}
FAKE_CLI = f"""#!{sys.executable}
import json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
plan = json.loads((root / "plan.json").read_text())
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "pid": os.getpid()}}) + "\\n")
sys.stderr.write("{NOISE}")
args = sys.argv[1:]
if args == ["ctl", "status"]:
    sys.exit(0)
if args == ["--json", "domain", "list"]:
    print(json.dumps({{"domains": json.loads((root / "domains.json").read_text())}}))
    sys.exit(0)
if plan["hang"]:
    time.sleep(60)
sys.stdout.write(plan["stdout"])
sys.exit(plan["exit"])
"""


def respond(cli: Path, stdout: str = json.dumps(RECEIPT), exit_code: int = 0, hang: bool = False) -> None:
    """Configure what the fake crystalline executable prints, returns, and whether it hangs."""
    (cli / "plan.json").write_text(json.dumps({"stdout": stdout, "exit": exit_code, "hang": hang}))


def external_cli(body: str) -> str:
    """Wrap a final-command probe with the external daemon and domain-list fixture contract."""
    return (
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "if sys.argv[1:] == ['ctl', 'status']: sys.exit(0)\n"
        "if sys.argv[1:] == ['--json', 'domain', 'list']:\n"
        "    print(json.dumps({'domains': json.loads((pathlib.Path(__file__).parent / 'domains.json').read_text())}))\n"
        "    sys.exit(0)\n" + body
    )


def calls(cli: Path) -> list[dict[str, object]]:
    """Return the argv, stdin, and pid recorded by each fake crystalline invocation."""
    log = cli / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def domain_list(cli: Path, names: list[str]) -> None:
    """Configure the authoritative domain identities returned by the fake engine."""
    (cli / "domains.json").write_text(json.dumps([{"name": name} for name in names]))


async def call(tool: str, arguments: dict[str, object]) -> dict[str, object] | None:
    """Call a tool on an in-memory server and return its structured content."""
    async with Client(create_server(Settings())) as client:
        return (await client.call_tool(tool, arguments)).structured_content


def workspace_context(context: EngineContext | None = None) -> EngineContext:
    """Select the fixture workspace while retaining its application's captured environment."""
    settings = Settings()
    base = context if context is not None else EngineContext.capture()
    root = settings.workspace_root if settings.workspace_root is not None else base.state_home / "marabrain/workspaces"
    prepare_workspace(WORKSPACE_ID, root)
    return base.for_workspace(WORKSPACE_ID, root)


def workspace_registry(context: EngineContext | None = None) -> DomainRegistry:
    """Open the fixture workspace registry for tool-wiring setup and durable assertions."""
    return DomainRegistry.from_environment(30, context=workspace_context(context))
