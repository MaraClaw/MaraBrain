"""Stateful executable boundary for registry and multi-process registration tests."""

import sys
from pathlib import Path

import pytest

SCRIPT = f'''#!{sys.executable}
"""Retain an engine configuration and file-backed memories across invocations."""
import json, os, pathlib, sys
root = pathlib.Path(__file__).parent
args = sys.argv[2:]
content = sys.stdin.read()
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "stdin": content}}) + "\\n")
domains = json.loads((root / "domains.json").read_text())
mode = (root / "failure").read_text() if (root / "failure").exists() else ""
if args == ["domain", "list"]:
    if mode == "list-exit":
        sys.stderr.write("secret engine payload")
        sys.exit(7)
    if mode == "list-malformed":
        print('{{"domains":[{{"name":"valid"}},{{"broken":true}}]}}')
    else:
        print(json.dumps({{"domains": domains}}))
elif args[:2] == ["domain", "init"]:
    path = pathlib.Path(args[2])
    path.mkdir(parents=True, exist_ok=True)
    (path / "MANIFEST.md").write_text("domain manifest")
    print('{{"created":true}}')
elif args[:2] == ["domain", "add"]:
    name, path = args[2], pathlib.Path(args[4])
    if not (path / "MANIFEST.md").is_file() or mode == "add-exit":
        sys.stderr.write("secret engine payload")
        sys.exit(8)
    if any(row["name"].lower() == name.lower() for row in domains):
        sys.exit(9)
    domains.append({{"name": name, "path": str(path)}})
    (root / "domains.json").write_text(json.dumps(domains))
    if mode == "add-uncertain":
        sys.exit(8)
    print(json.dumps({{"registered":name,"path":str(path)}}))
elif args[0] == "write":
    domain = next(row for row in domains if row["name"] == args[1])
    path = pathlib.Path(domain["path"]) / (args[2] + ".md")
    path.write_text(content)
    print(json.dumps({{"domain":args[1],"permalink":args[2]}}))
elif args[0] == "read":
    name = args[2].split("=",1)[1]
    domain = next(row for row in domains if row["name"] == name)
    content = (pathlib.Path(domain["path"]) / (args[1] + ".md")).read_text()
    print(json.dumps({{"domain":name,"content":content}}))
else:
    print(json.dumps({{"arguments":args}}))
'''


@pytest.fixture
def domain_cli(cli: Path) -> Path:
    """Replace the recording CLI with a persistent engine fake without touching live paths."""
    (cli / "crystalline").write_text(SCRIPT)
    return cli
