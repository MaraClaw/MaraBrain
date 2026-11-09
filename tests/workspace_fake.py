"""Stateful subprocess boundary that stores every workspace's data in its captured state path."""

import sys

SCRIPT = f'''#!{sys.executable}
"""Emulate native memory commands without sharing workspace state."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time

state = Path(os.environ["XDG_STATE_HOME"])
state.mkdir(parents=True, exist_ok=True)
events = Path(os.environ["WORKSPACE_TEST_EVENTS"])
args = sys.argv[1:]
with events.open("a") as stream:
    stream.write(json.dumps({{"argv": args, "state": str(state), "pid": os.getpid()}}) + "\\n")
if args == ["ctl", "status"]:
    if (state / "daemon-down").exists():
        sys.exit(1)
    time.sleep(float(os.environ.get("WORKSPACE_PROBE_DELAY", "0")))
    print(json.dumps({{"pid": os.getpid()}}))
    sys.exit(0)
args = args[1:]
domains_path = state / "fake-domains.json"
domains = json.loads(domains_path.read_text()) if domains_path.exists() else []
if args == ["domain", "list"]:
    print(json.dumps({{"domains": domains}}))
elif args[:2] == ["domain", "init"]:
    Path(args[2]).mkdir(parents=True, exist_ok=True)
    print('{{"created": true}}')
elif args[:2] == ["domain", "add"]:
    domains.append({{"name": args[2], "path": args[4]}})
    domains_path.write_text(json.dumps(domains))
    print(json.dumps({{"registered": args[2], "path": args[4]}}))
else:
    command = args[0]
    if command == "search":
        names = [arg.split("=", 1)[1] for arg in args if arg.startswith("--domain=")]
        hits = []
        for domain in domains:
            if domain["name"] in names:
                for path in Path(domain["path"]).glob("*.json"):
                    memory = json.loads(path.read_text())
                    if args[-1] in memory["content"]:
                        hits.append(memory)
        print(json.dumps({{"results": hits}}))
        sys.exit(0)
    name = args[1] if command == "write" else (args[2].split("=", 1)[1] if command == "read" else args[2])
    domain = next(row for row in domains if row["name"] == name)
    permalink = args[2] if command == "write" else args[1]
    path = Path(domain["path"]) / (permalink + ".json")
    if command == "write":
        content = sys.stdin.read()
        memory = {{"domain": name, "permalink": permalink, "content": content,
                  "checksum": hashlib.sha256(content.encode()).hexdigest(), "links": ["shared"]}}
        with path.open("x") as stream:
            json.dump(memory, stream)
    elif command == "read":
        memory = json.loads(path.read_text())
    elif command == "edit":
        memory = json.loads(path.read_text())
        memory["content"] += sys.stdin.read()
        path.write_text(json.dumps(memory))
    elif command == "delete":
        path.unlink()
        memory = {{"deleted": True}}
    else:
        sys.exit(9)
    print(json.dumps(memory))
'''
