"""Stress a live MaraBrain HTTP server process for performance and reliability against a real, isolated Crystalline engine.

Run from the repository root with Crystalline 0.33.0 on PATH:

    uv run --no-sync python benchmarks/http_stress.py

The script launches ``python -m marabrain`` as a separate process with HTTP transport and managed
engines under a private ``/tmp`` root, then drives it only through ``/mcp``: protocol overhead, write
and mixed load, write contention, large payloads, connection overload, hostile traffic, client aborts,
a soak, SIGTERM under load, a restart with durability checks, and a run with the capacity limits set.
It writes http_stress.json and http_stress.md and never touches the user's own Crystalline state.
"""

import argparse
import itertools
import json
import os
import random
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO, cast

import anyio
import httpx
from fastmcp import Client

from graph_eval import DATASTORES, SERVICES, TEAMS, percentile, run_cli, title_case, wait_for_embeddings
from marabrain.engine_context import EngineContext
from marabrain.workspace_storage import prepare_workspace

REPOSITORY = Path(__file__).resolve().parents[1]
WORKSPACES = ("ws-a", "ws-b", "ws-c")
MIX = {"search": 50, "read": 20, "context": 12, "create": 10, "edit": 5, "review": 3}
BOUNDED = {
    "MARABRAIN_MAX_TRANSIENT_PROCESSES": "16",
    "MARABRAIN_MAX_WORKSPACE_TRANSIENT_PROCESSES": "8",
    "MARABRAIN_DOMAIN_COMMAND_CAPACITY": "4",
    "MARABRAIN_MAX_RESIDENT_WORKSPACES": "2",
}

type Caller = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
type Operation = Callable[[Caller, random.Random], Awaitable[object]]


def describe(error: BaseException) -> str:
    """Name the innermost exception of a possibly grouped failure."""
    text = f"{type(error).__name__}: {error}"[:160]
    if isinstance(error, BaseExceptionGroup):
        children = cast(BaseExceptionGroup[BaseException], error).exceptions
        return describe(children[0]) if children else text
    return text


@dataclass
class Stats:
    """Collect timestamped per-tool outcomes for one load phase."""

    started: float = field(default_factory=time.perf_counter)
    samples: list[tuple[float, str, float, str | None]] = field(default_factory=list[tuple[float, str, float, str | None]])

    def add(self, tool: str, started: float, elapsed: float, error: str | None = None) -> None:
        """Record one call outcome."""
        self.samples.append((started - self.started, tool, elapsed, error))

    def summary(self, wall: float | None = None) -> dict[str, Any]:
        """Summarize per-tool latency percentiles, error messages, and throughput."""
        tools: dict[str, Any] = {}
        for name in sorted({tool for _, tool, _, _ in self.samples}):
            rows = [row for row in self.samples if row[1] == name]
            values = sorted(elapsed for _, _, elapsed, error in rows if error is None)
            errors = Counter(error for _, _, _, error in rows if error is not None)
            tools[name] = {
                "ok": len(values),
                "errors": sum(errors.values()),
                "error_samples": {message: count for message, count in errors.most_common(3)},
                "p50_ms": percentile(values, 50),
                "p95_ms": percentile(values, 95),
                "p99_ms": percentile(values, 99),
                "max_ms": round(values[-1] * 1000, 1) if values else None,
            }
        ok = sum(1 for row in self.samples if row[3] is None)
        wall = wall if wall is not None else time.perf_counter() - self.started
        return {
            "wall_s": round(wall, 2),
            "ok": ok,
            "errors": len(self.samples) - ok,
            "ok_per_s": round(ok / wall, 1) if wall else None,
            "tools": tools,
        }

    def windows(self, width: float) -> list[dict[str, Any]]:
        """Return throughput, latency, and errors for consecutive windows of the phase."""
        result: list[dict[str, Any]] = []
        end = max((offset + elapsed for offset, _, elapsed, _ in self.samples), default=0.0)
        for index in range(int(end // width) + 1):
            rows = [row for row in self.samples if index * width <= row[0] + row[2] < (index + 1) * width]
            values = sorted(elapsed for _, _, elapsed, error in rows if error is None)
            result.append(
                {
                    "window_s": f"{int(index * width)}-{int((index + 1) * width)}",
                    "ok_per_s": round(len(values) / width, 1),
                    "errors": len(rows) - len(values),
                    "p50_ms": percentile(values, 50),
                    "p95_ms": percentile(values, 95),
                }
            )
        return result


@dataclass
class World:
    """Track permalinks the server confirmed so later phases can read and verify them."""

    corpus: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    created: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    serial: itertools.count[int] = field(default_factory=itertools.count)


@dataclass(frozen=True)
class Proc:
    """One process in the server's process tree."""

    pid: int
    rss_kb: int
    cpu_s: float
    role: str


class LiveServer:
    """Own one ``python -m marabrain`` HTTP process and observe its process tree."""

    def __init__(self, environment: dict[str, str], log: Path) -> None:
        """Bind the process environment and log file."""
        self.environment = environment
        self.log = log
        self.process: subprocess.Popen[bytes] | None = None
        self.handle: TextIO | None = None

    def start(self) -> None:
        """Launch the server process in its own session."""
        self.handle = self.log.open("a")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "marabrain"],
            cwd=REPOSITORY,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=self.handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    @property
    def pid(self) -> int:
        """Return the running server pid."""
        assert self.process is not None
        return self.process.pid

    def tree(self) -> list[Proc]:
        """Return the server and its descendants with resident memory and role."""
        if self.process is None or self.process.poll() is not None:
            return []
        output = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,time=,command="], capture_output=True, text=True, check=True).stdout
        children: dict[int, list[tuple[int, int, float, str]]] = {}
        own: tuple[int, float] = (0, 0.0)
        for line in output.splitlines():
            parts = line.split(None, 4)
            if len(parts) == 5:
                row = (int(parts[0]), int(parts[2]), cpu_seconds(parts[3]), parts[4])
                children.setdefault(int(parts[1]), []).append(row)
                if row[0] == self.pid:
                    own = (row[1], row[2])
        result = [Proc(self.pid, own[0], own[1], "server")]
        queue = [self.pid]
        while queue:
            for pid, rss, cpu, command in children.get(queue.pop(), []):
                executable = Path(command.split()[0]).name
                role = ("daemon" if " serve" in command else "transient") if executable == "crystalline" else "other"
                result.append(Proc(pid, rss, cpu, role))
                queue.append(pid)
        return result

    def open_files(self) -> int:
        """Count the server's open file descriptors."""
        output = subprocess.run(["lsof", "-n", "-P", "-p", str(self.pid)], capture_output=True, text=True, check=False).stdout
        return max(0, len(output.splitlines()) - 1)

    async def stop(self, grace: float = 20) -> dict[str, Any]:
        """Send SIGTERM, time the exit, and report descendants that outlive the server."""
        assert self.process is not None
        descendants = [proc.pid for proc in self.tree() if proc.role != "server"]
        started = time.perf_counter()
        self.process.send_signal(signal.SIGTERM)
        with anyio.move_on_after(grace):
            while self.process.poll() is None:
                await anyio.sleep(0.05)
        exited = self.process.poll() is not None
        exit_s = round(time.perf_counter() - started, 2)
        if not exited:
            self.process.kill()
            self.process.wait()
        with anyio.move_on_after(10):
            while any(alive(pid) for pid in descendants):
                await anyio.sleep(0.1)
        survivors = [pid for pid in descendants if alive(pid)]
        for pid in survivors:
            os.kill(pid, signal.SIGKILL)
        if self.handle is not None:
            self.handle.close()
        return {
            "exited_within_grace": exited,
            "exit_s": exit_s,
            "exit_code": self.process.returncode,
            "descendants_before": len(descendants),
            "descendants_surviving_10s": len(survivors),
        }


def cpu_seconds(text: str) -> float:
    """Parse a ps cumulative CPU time such as 1:02.50 or 1-02:03:04 into seconds."""
    days, _, clock = text.rpartition("-")
    seconds = 0.0
    for part in clock.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds + int(days or 0) * 86400


def alive(pid: int) -> bool:
    """Return whether a pid still exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
    return bool(state) and not state.startswith("Z")


class Sampler:
    """Sample the server's process tree twice a second and attribute samples to the current phase."""

    def __init__(self, server: LiveServer) -> None:
        """Bind the observed server."""
        self.server = server
        self._phase = "startup"
        self.rows: list[tuple[str, int, int, int, int]] = []
        self.cpu: list[tuple[str, float, float, dict[int, float]]] = []

    @property
    def phase(self) -> str:
        """Return the phase new samples belong to."""
        return self._phase

    @phase.setter
    def phase(self, name: str) -> None:
        """Switch phases and report progress on stderr."""
        self._phase = name
        print(f"{time.strftime('%H:%M:%S')} phase {name}", file=sys.stderr, flush=True)

    async def run(self) -> None:
        """Sample until cancelled."""
        while True:
            tree = await anyio.to_thread.run_sync(self.server.tree)
            daemons = [proc.rss_kb for proc in tree if proc.role == "daemon"]
            server = sum(proc.rss_kb for proc in tree if proc.role == "server")
            self.rows.append((self.phase, server, sum(daemons), len(daemons), sum(proc.role == "transient" for proc in tree)))
            if tree:
                engines = {proc.pid: proc.cpu_s for proc in tree if proc.role == "daemon"}
                self.cpu.append((self.phase, time.perf_counter(), sum(proc.cpu_s for proc in tree if proc.role == "server"), engines))
            await anyio.sleep(0.5)

    def summary(self, phase: str) -> dict[str, Any]:
        """Return peak memory and process counts observed during one phase."""
        rows = [row for row in self.rows if row[0] == phase]
        cpu = [row for row in self.cpu if row[0] == phase]
        usage: dict[str, Any] = {}
        if len(cpu) > 1:
            wall = cpu[-1][1] - cpu[0][1]
            daemon = sum(max(0.0, cpu[-1][3][pid] - cpu[0][3][pid]) for pid in cpu[-1][3] if pid in cpu[0][3])
            usage = {"server_cpu_cores": round((cpu[-1][2] - cpu[0][2]) / wall, 2), "daemons_cpu_cores": round(daemon / wall, 2)}
        return {
            **usage,
            "server_rss_peak_mb": round(max((row[1] for row in rows), default=0) / 1024, 1),
            "daemons_rss_peak_mb": round(max((row[2] for row in rows), default=0) / 1024, 1),
            "daemons_peak": max((row[3] for row in rows), default=0),
            "transient_cli_peak": max((row[4] for row in rows), default=0),
        }


def caller(client: Client[Any], stats: Stats) -> Caller:
    """Bind a timed, error-recording tool caller to one MCP client."""

    async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """Call one tool and record its outcome; return structured content or None on failure."""
        started = time.perf_counter()
        try:
            if tool == "tools/list":
                await client.list_tools()
                stats.add(tool, started, time.perf_counter() - started)
                return {}
            result = await client.call_tool(tool, arguments, raise_on_error=False)
        except Exception as error:
            stats.add(tool, started, time.perf_counter() - started, describe(error))
            # Back off like a real client; a dead server fails without suspending and would otherwise starve the event loop.
            await anyio.sleep(0.05)
            return None
        elapsed = time.perf_counter() - started
        if result.is_error or result.structured_content is None:
            stats.add(tool, started, elapsed, " ".join(getattr(part, "text", "") for part in result.content)[:160] or "tool error")
            return None
        stats.add(tool, started, elapsed)
        return dict(result.structured_content)

    return call


async def agents(
    url: str,
    token: str,
    workers: int,
    stats: Stats,
    operation: Operation,
    *,
    duration: float | None = None,
    operations: int | None = None,
    stop: anyio.Event | None = None,
) -> float:
    """Run independent MCP clients, one per worker, until a duration, an operation count, or a stop event; return wall time."""
    issued = itertools.count()
    started = time.perf_counter()
    deadline = None if duration is None else started + duration

    async def agent(index: int) -> None:
        """Connect one client and issue operations until the phase ends."""
        generator = random.Random(index * 7919 + workers)
        try:
            async with Client(url, auth=token, timeout=60) as client:
                call = caller(client, stats)
                while not (stop is not None and stop.is_set()) and (deadline is None or time.perf_counter() < deadline):
                    if operations is not None and next(issued) >= operations:
                        return
                    await operation(call, generator)
                    await anyio.lowlevel.checkpoint()
        except Exception as error:
            stats.add("session", time.perf_counter(), 0.0, describe(error))

    async with anyio.create_task_group() as group:
        for index in range(workers):
            group.start_soon(agent, index)
    return time.perf_counter() - started


def mixed(world: World, workspaces: tuple[str, ...]) -> Operation:
    """Build the weighted agent mix of search, read, context, create, edit, and review."""
    kinds = [kind for kind, weight in MIX.items() for _ in range(weight)]

    async def run(call: Caller, generator: random.Random) -> None:
        """Issue one operation of the mix."""
        workspace = generator.choice(workspaces)
        base: dict[str, Any] = {"workspace_id": workspace}
        corpus, created = world.corpus[workspace], world.created.setdefault(workspace, [])
        match generator.choice(kinds):
            case "search":
                query = f"{generator.choice(SERVICES)} service owner datastore"
                await call("search_memories", {**base, "domains": ["org"], "query": query, "limit": 10})
            case "read":
                await call("read_memory", {**base, "domain": "org", "permalink": generator.choice(corpus)})
            case "context":
                await call("build_memory_context", {**base, "domain": "org", "permalink": generator.choice(corpus), "depth": 2})
            case "review":
                await call("review_memories", {**base, "domains": ["org"], "limit": 20})
            case "edit" if created:
                operation = {"type": "append", "content": f"- [fact] Edit {next(world.serial)} appended under load #load"}
                await call("edit_memory", {**base, "domain": "mixed", "permalink": generator.choice(created), "operation": operation})
            case _:
                number = next(world.serial)
                content = f"Load note {number}.\n\n- [fact] Load note {number} was written under load #load\n- [fact] It is synthetic #load\n"
                receipt = await call("create_memory", {**base, "domain": "mixed", "title": f"Load Note {number}", "content": content, "tags": ["load"]})
                if receipt is not None:
                    created.append(receipt["permalink"])

    return run


async def seed(call: Caller, workspace: str) -> list[str]:
    """Write the service, team, and datastore corpus with relations into one workspace's org domain."""
    permalinks: list[str] = []

    async def create(title: str, content: str, tag: str) -> None:
        """Create one corpus memory or fail the run."""
        receipt = await call("create_memory", {"workspace_id": workspace, "domain": "org", "title": title, "content": content, "tags": [tag]})
        if receipt is None:
            raise RuntimeError(f"seeding {workspace} failed at {title}")
        permalinks.append(receipt["permalink"])

    for index, team in enumerate(TEAMS):
        await create(f"Team {team}", f"Team {team} is an engineering team.\n\n- [fact] Team {team} uses pager rotation PR-{100 + index} #oncall\n", "team")
    for index, store in enumerate(DATASTORES):
        await create(f"{store} Datastore", f"The {store} datastore stores records.\n\n- [fact] It runs version {12 + index}.{index} #datastore\n", "datastore")
    for index, noun in enumerate(SERVICES):
        team, store = TEAMS[index % len(TEAMS)], DATASTORES[(index * 3) % len(DATASTORES)]
        content = (
            f"The {noun} service processes {noun} requests.\n\n- [fact] The {noun} service exposes a gRPC API #service\n"
            f"- owned_by [[Team {team}]]\n- depends_on [[{store} Datastore]]\n"
        )
        await create(f"{title_case(noun)} Service", content, "service")
    return permalinks


async def raw_call(http: httpx.AsyncClient, url: str, token: str, tool: str, arguments: dict[str, Any], host: str | None = None) -> tuple[int, bool]:
    """POST one stateless tools/call without a client session; return the HTTP status and whether the tool succeeded."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if host is not None:
        headers["Host"] = host
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    response = await http.post(url, json=body, headers=headers)
    if response.status_code != 200:
        return response.status_code, False
    lines = [line.removeprefix("data:").strip() for line in response.text.splitlines() if line.startswith("data:")]
    payload = json.loads(lines[-1] if lines else response.text)
    result: dict[str, Any] = payload.get("result") or {}
    return 200, not result.get("isError", True)


async def raw_burst(url: str, token: str, count: int, tool: str, arguments: dict[str, Any], *, host: str | None = None, timeout: float = 60) -> dict[str, Any]:
    """Fire requests concurrently, each on its own connection; return the outcome histogram and latency of successes."""
    outcomes: Counter[str] = Counter()
    latencies: list[float] = []
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as http:

        async def one() -> None:
            """Send one request and classify its outcome."""
            started = time.perf_counter()
            try:
                status, ok = await raw_call(http, url, token, tool, arguments, host)
            except httpx.HTTPError as error:
                outcomes[type(error).__name__] += 1
                return
            outcomes[f"{status} tool_ok" if ok else f"{status}"] += 1
            if ok:
                latencies.append(time.perf_counter() - started)

        started = time.perf_counter()
        async with anyio.create_task_group() as group:
            for _ in range(count):
                group.start_soon(one)
    latencies.sort()
    return {
        "requests": count,
        "outcomes": dict(sorted(outcomes.items())),
        "wall_s": round(time.perf_counter() - started, 2),
        "ok_p50_ms": percentile(latencies, 50),
        "ok_p95_ms": percentile(latencies, 95),
    }


async def wait_ready(url: str, token: str, process: LiveServer, timeout: float = 120) -> float:
    """Poll the health tool until it succeeds and return seconds since launch."""
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=5) as http:
        with anyio.fail_after(timeout):
            while True:
                if process.process is not None and process.process.poll() is not None:
                    raise RuntimeError(f"server exited with {process.process.returncode}; see {process.log}")
                try:
                    if await raw_call(http, url, token, "health", {}) == (200, True):
                        return round(time.perf_counter() - started, 2)
                except httpx.HTTPError:
                    pass
                await anyio.sleep(0.05)


async def drain_transients(server: LiveServer, timeout: float = 40) -> tuple[int, float]:
    """Wait until no transient CLI child remains; return the count at start and seconds to drain."""
    started = time.perf_counter()
    initial = sum(proc.role == "transient" for proc in await anyio.to_thread.run_sync(server.tree))
    with anyio.move_on_after(timeout):
        while any(proc.role == "transient" for proc in await anyio.to_thread.run_sync(server.tree)):
            await anyio.sleep(0.1)
    return initial, round(time.perf_counter() - started, 2)


def log_findings(log: Path) -> dict[str, int]:
    """Count tracebacks and error lines the server wrote."""
    text = log.read_text(errors="replace")
    return {"tracebacks": text.count("Traceback"), "error_lines": sum(1 for line in text.splitlines() if line.startswith(("ERROR", "CRITICAL")))}


def free_port() -> int:
    """Return an unused local TCP port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def first_run(server: LiveServer, sampler: Sampler, url: str, token: str, world: World, environment: dict[str, str], quick: bool) -> dict[str, Any]:
    """Drive every default-profile phase against the first server lifetime."""
    report: dict[str, Any] = {}
    scale = 0.25 if quick else 1.0

    def seconds(value: float) -> float:
        """Scale a phase duration, keeping at least five seconds."""
        return max(5.0, value * scale)

    report["startup"] = {"launch_to_health_s": await wait_ready(url, token, server)}

    sampler.phase = "seed"
    stats = Stats()
    async with Client(url, auth=token, timeout=180) as client:
        call = caller(client, stats)
        for workspace in WORKSPACES:
            started = time.perf_counter()
            await call("read_memory", {"workspace_id": workspace, "domain": "org", "permalink": "absent"})
            report["startup"][f"cold_activation_{workspace}_s"] = round(time.perf_counter() - started, 2)
        for workspace in WORKSPACES:
            world.corpus[workspace] = await seed(call, workspace)
    started = time.perf_counter()
    for workspace in WORKSPACES:
        context = EngineContext(environment, Path(environment["XDG_STATE_HOME"]), "x").for_workspace(workspace, Path(environment["MARABRAIN_WORKSPACE_ROOT"]))
        await wait_for_embeddings(["org"], dict(context.environment))
    report["seed"] = {"memories_per_workspace": len(world.corpus["ws-a"]), "embedding_wait_s": round(time.perf_counter() - started, 1)}

    sampler.phase = "protocol"
    report["protocol"] = {}
    for workers in (1, 16, 64):
        stats = Stats()

        async def overhead(call: Caller, generator: random.Random) -> None:
            """Alternate the engine-free listing with the engine-probing health tool."""
            await call("tools/list" if generator.random() < 0.5 else "health", {})

        wall = await agents(url, token, workers, stats, overhead, operations=int(400 * scale) or 40)
        report["protocol"][f"workers_{workers}"] = stats.summary(wall)

    sampler.phase = "writes"
    report["writes"] = {}
    for workers, count in ((1, 60), (8, 240), (32, 480)):
        stats = Stats()
        receipts: list[str] = []
        serial = itertools.count()
        domain = f"write-c{workers}"

        async def write(
            call: Caller, generator: random.Random, domain: str = domain, receipts: list[str] = receipts, serial: itertools.count[int] = serial
        ) -> None:
            """Create one linked memory."""
            index = next(serial)
            link = f"- depends_on [[Write Item {index - 1}]]\n" if index else ""
            content = f"Write item {index}.\n\n- [fact] Item {index} was written concurrently #write\n{link}"
            arguments = {"workspace_id": "ws-a", "domain": domain, "title": f"Write Item {index}", "content": content, "tags": ["write"]}
            receipt = await call("create_memory", arguments)
            if receipt is not None:
                receipts.append(receipt["permalink"])

        wall = await agents(url, token, workers, stats, write, operations=max(20, int(count * scale)))
        summary = stats.summary(wall)
        verify = Stats()
        unique = sorted(set(receipts))
        await agents(url, token, 16, verify, verify_reads("ws-a", domain, list(unique)), operations=len(unique))
        summary["receipts"], summary["unique_permalinks"] = len(receipts), len(unique)
        summary["readable_after"] = verify.summary()["ok"]
        world.created.setdefault(f"ws-a/{domain}", []).extend(unique)
        report["writes"][f"workers_{workers}"] = summary

    sampler.phase = "mixed"
    report["mixed"] = {}
    for workers in (8, 32, 64):
        stats = Stats()
        wall = await agents(url, token, workers, stats, mixed(world, ("ws-a", "ws-b")), duration=seconds(20))
        report["mixed"][f"workers_{workers}"] = {**stats.summary(wall), **sampler.summary("mixed")}

    sampler.phase = "contention"
    report["contention"] = await contention(url, token)

    sampler.phase = "payloads"
    report["payloads"] = await payloads(url, token)

    sampler.phase = "overload"
    arguments = {"workspace_id": "ws-a", "domains": ["org"], "query": "ledger service owner", "limit": 10}
    burst = await raw_burst(url, token, 300, "search_memories", arguments)
    recovery = Stats()
    await agents(url, token, 1, recovery, lambda call, generator: call("search_memories", arguments), operations=20)
    alive_after = server.process is not None and server.process.poll() is None
    report["overload"] = {"burst_300_concurrent_connections": burst, "after": recovery.summary(), "server_alive": alive_after}

    sampler.phase = "hostile"
    stats = Stats()
    bad: dict[str, Any] = {}

    async def attack() -> None:
        """Send wrong-token and wrong-Host traffic while good agents run."""
        bad["wrong_token_x2000"] = await raw_burst_batches(url, "x" * 48, 2000, 50, "health", {})
        bad["wrong_host_x200"] = await raw_burst_batches(url, token, 200, 50, "health", {}, host="evil.example")

    async with anyio.create_task_group() as group:
        group.start_soon(attack)
        wall = await agents(url, token, 16, stats, mixed(world, ("ws-a", "ws-b")), duration=seconds(15))
    report["hostile"] = {"good_traffic_16_workers": stats.summary(wall), **bad}

    sampler.phase = "aborts"
    aborted: Counter[str] = Counter()
    heavy = {"workspace_id": "ws-a", "domain": "org", "permalink": world.corpus["ws-a"][-1], "depth": 3}
    for _ in range(5):
        result = await raw_burst(url, token, 64, "build_memory_context", heavy, timeout=0.05)
        aborted.update(result["outcomes"])
    transients, drain_s = await drain_transients(server)
    after = Stats()
    await agents(url, token, 1, after, lambda call, generator: call("health", {}), operations=5)
    report["aborts"] = {
        "requests_aborted_by_client_timeout_50ms": dict(aborted),
        "transient_cli_right_after": transients,
        "drain_s": drain_s,
        "health_after": after.summary(),
    }

    sampler.phase = "soak"
    files_before = server.open_files()
    rss_before = {proc.role: proc.rss_kb for proc in server.tree() if proc.role == "server"}
    stats = Stats()
    wall = await agents(url, token, 16, stats, mixed(world, ("ws-a", "ws-b")), duration=seconds(120))
    report["soak"] = {
        **stats.summary(wall),
        "windows": stats.windows(15 if not quick else 5),
        "server_rss_start_mb": round(rss_before.get("server", 0) / 1024, 1),
        "server_rss_end_mb": round(sum(proc.rss_kb for proc in server.tree() if proc.role == "server") / 1024, 1),
        "open_files_start": files_before,
        "open_files_end": server.open_files(),
        **sampler.summary("soak"),
    }

    sampler.phase = "sigterm"
    stats = Stats()
    stop = anyio.Event()
    shutdown: dict[str, Any] = {}

    async def terminate() -> None:
        """Send SIGTERM three seconds into sustained load."""
        await anyio.sleep(3)
        shutdown.update(await server.stop())
        stop.set()

    async with anyio.create_task_group() as group:
        group.start_soon(terminate)
        await agents(url, token, 16, stats, mixed(world, ("ws-a", "ws-b")), stop=stop)
    report["sigterm_under_load"] = {**shutdown, "client_outcomes": {"ok": stats.summary()["ok"], "errors": stats.summary()["errors"]}}
    return report


def verify_reads(workspace: str, domain: str, permalinks: list[str]) -> Operation:
    """Build an operation that reads each listed permalink once."""
    pending = list(permalinks)

    async def run(call: Caller, generator: random.Random) -> None:
        """Read the next permalink."""
        if pending:
            await call("read_memory", {"workspace_id": workspace, "domain": domain, "permalink": pending.pop()})

    return run


async def raw_burst_batches(
    url: str, token: str, total: int, concurrency: int, tool: str, arguments: dict[str, Any], *, host: str | None = None
) -> dict[str, Any]:
    """Send raw requests in fixed-size concurrent batches and merge the outcome histograms."""
    outcomes: Counter[str] = Counter()
    started = time.perf_counter()
    for _ in range(total // concurrency):
        outcomes.update((await raw_burst(url, token, concurrency, tool, arguments, host=host))["outcomes"])
    return {"outcomes": dict(outcomes), "wall_s": round(time.perf_counter() - started, 2)}


async def contention(url: str, token: str) -> dict[str, Any]:
    """Race appends to one memory and creates of one title, then check for lost or duplicated writes."""
    base = {"workspace_id": "ws-a", "domain": "contend"}
    stats = Stats()
    async with Client(url, auth=token, timeout=60) as client:
        call = caller(client, stats)
        receipt = await call("create_memory", {**base, "title": "Contended Note", "content": "Contended.\n\n- [fact] Base line #contend\n", "tags": ["c"]})
        assert receipt is not None
        permalink = receipt["permalink"]
    markers = [f"marker-{index:03d}" for index in range(48)]
    appends = Stats()
    queue = list(markers)

    async def append(call: Caller, generator: random.Random) -> None:
        """Append one unique marker."""
        if queue:
            marker = queue.pop()
            await call("edit_memory", {**base, "permalink": permalink, "operation": {"type": "append", "content": f"- [fact] {marker} #contend"}})

    await agents(url, token, 48, appends, append, operations=48)
    creates = Stats()
    titles: list[str] = []

    async def create(call: Caller, generator: random.Random) -> None:
        """Create the same title concurrently."""
        content = "Duplicate race.\n\n- [fact] Same title race #contend\n"
        result = await call("create_memory", {**base, "title": "Same Title", "content": content, "tags": ["c"]})
        if result is not None:
            titles.append(result["permalink"])

    await agents(url, token, 24, creates, create, operations=24)
    async with Client(url, auth=token, timeout=60) as client:
        final = await caller(client, stats)("read_memory", {**base, "permalink": permalink})
    text = json.dumps(final)
    return {
        "concurrent_appends": appends.summary(),
        "markers_present_after": sum(marker in text for marker in markers),
        "markers_written": 48,
        "same_title_creates": creates.summary(),
        "same_title_distinct_permalinks": len(set(titles)),
    }


async def payloads(url: str, token: str) -> dict[str, Any]:
    """Create and read back memories of increasing size."""
    result: dict[str, Any] = {}
    async with Client(url, auth=token, timeout=120) as client:
        for size in (64 * 1024, 1024 * 1024, 4 * 1024 * 1024):
            stats = Stats()
            call = caller(client, stats)
            line = "- [fact] Payload line carrying filler text for size testing #payload\n"
            content = "Payload note.\n\n" + line * (size // len(line))
            started = time.perf_counter()
            receipt = await call("create_memory", {"workspace_id": "ws-a", "domain": "payload", "title": f"Payload {size}", "content": content, "tags": ["p"]})
            created = time.perf_counter() - started
            read = None if receipt is None else await call("read_memory", {"workspace_id": "ws-a", "domain": "payload", "permalink": receipt["permalink"]})
            result[f"{size // 1024}KiB"] = {
                "create_ok": receipt is not None,
                "create_ms": round(created * 1000, 1),
                "read_ok": read is not None,
                "read_bytes": len(json.dumps(read)) if read is not None else 0,
                "errors": [row[3] for row in stats.samples if row[3]],
            }
    return result


async def second_run(server: LiveServer, sampler: Sampler, url: str, token: str, world: World, quick: bool) -> dict[str, Any]:
    """Restart with capacity limits, verify durability, then overload the limits."""
    report: dict[str, Any] = {"startup": {"launch_to_health_s": await wait_ready(url, token, server)}}
    sampler.phase = "durability"
    targets = [(key.split("/")[0], key.split("/")[1], permalink) for key, permalinks in world.created.items() if "/" in key for permalink in permalinks]
    targets += [(workspace, "mixed", permalink) for workspace in ("ws-a", "ws-b") for permalink in world.created.get(workspace, [])]
    stats = Stats()
    pending = list(targets)

    async def read(call: Caller, generator: random.Random) -> None:
        """Read one confirmed memory from before the restart."""
        if pending:
            workspace, domain, permalink = pending.pop()
            await call("read_memory", {"workspace_id": workspace, "domain": domain, "permalink": permalink})

    wall = await agents(url, token, 4, stats, read, operations=len(targets))
    summary = stats.summary(wall)
    report["durability"] = {"confirmed_before_restart": len(targets), "readable_after_restart": summary["ok"], "errors": summary["tools"]}

    sampler.phase = "bounded"
    stats = Stats()
    wall = await agents(url, token, 64, stats, mixed(world, WORKSPACES), duration=max(5.0, 20 * (0.25 if quick else 1.0)))
    health = Stats()
    await agents(url, token, 1, health, lambda call, generator: call("health", {}), operations=5)
    refusals = [row for row in stats.samples if row[3] is not None and "capacity" in row[3]]
    refusal_latency = sorted(row[2] for row in refusals)
    report["bounded"] = {
        "limits": BOUNDED,
        **stats.summary(wall),
        "refusals": len(refusals),
        "refusal_p95_ms": percentile(refusal_latency, 95),
        "health_after": health.summary(),
        **sampler.summary("bounded"),
    }
    sampler.phase = "final_stop"
    report["sigterm_idle"] = await server.stop()
    return report


def markdown(report: dict[str, Any]) -> str:
    """Render the report as Markdown."""
    lines = [
        "# MaraBrain live HTTP stress test",
        "",
        f"Crystalline {report['engine']}, `python -m marabrain` over streamable HTTP (stateless, bearer auth), managed engines, "
        f"{report['started']}, {report['machine']}. Each load worker is an independent fastmcp client; overload, hostile, and abort "
        "phases use raw HTTP so status codes stay visible.",
    ]

    def table(title: str, levels: dict[str, Any]) -> None:
        """Append one per-tool latency table."""
        header = "| Level | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | max ms | OK/s (level) | Error samples |"
        lines.extend(["", f"## {title}", "", header, "|" + " --- |" * 10])
        for level, data in levels.items():
            for tool, stats in data["tools"].items():
                samples = "; ".join(f"{message} x{count}" for message, count in stats["error_samples"].items())
                lines.append(
                    f"| {level} | {tool} | {stats['ok']} | {stats['errors']} | {stats['p50_ms']} | {stats['p95_ms']} | {stats['p99_ms']} | "
                    f"{stats['max_ms']} | {data['ok_per_s']} | {samples} |"
                )
            extras = {key: value for key, value in data.items() if key not in {"tools", "windows", "errors_by_tool"} and not isinstance(value, dict)}
            lines.append(f"| {level} | *totals* | | | | | | | | {', '.join(f'{key}={value}' for key, value in extras.items())} |")

    def facts(title: str, data: dict[str, Any]) -> None:
        """Append a section of scalar or nested facts."""
        lines.extend(["", f"## {title}", ""])
        for key, value in data.items():
            if isinstance(value, dict) and "tools" in value:
                table(f"{title}: {key}", {key: value})
            else:
                lines.append(f"- {key}: `{json.dumps(value)}`")

    first, second = report["first_run"], report["second_run"]
    facts("Startup and seeding (default limits)", {**first["startup"], **first["seed"]})
    table("Protocol overhead: tools/list and health", first["protocol"])
    table("Concurrent writes (linked creates, then read-back)", first["writes"])
    table("Mixed agent workload on two workspaces", first["mixed"])
    facts("Write contention", first["contention"])
    facts("Large payloads", first["payloads"])
    facts("Connection overload (limit_concurrency=100)", first["overload"])
    facts("Hostile traffic during load", first["hostile"])
    facts("Client aborts", first["aborts"])
    soak = first["soak"]
    table("Soak", {"16 workers": soak})
    lines.extend(["", "| Window s | OK/s | Errors | p50 ms | p95 ms |", "| --- | --- | --- | --- | --- |"])
    lines.extend(f"| {row['window_s']} | {row['ok_per_s']} | {row['errors']} | {row['p50_ms']} | {row['p95_ms']} |" for row in soak["windows"])
    facts("SIGTERM under load", first["sigterm_under_load"])
    facts("Restart with capacity limits", {**second["startup"], **second["durability"]})
    table("Capacity-limited overload: 64 workers on three workspaces", {"64 workers": second["bounded"]})
    facts("SIGTERM idle", second["sigterm_idle"])
    facts("Server logs", report["logs"])
    return "\n".join(lines) + "\n"


async def main(output: Path, keep: bool, quick: bool) -> None:
    """Prepare an isolated engine root, run both server lifetimes, and write the reports."""
    executable = shutil.which("crystalline")
    if executable is None:
        raise SystemExit("Install Crystalline 0.33.0 on PATH first.")
    root = Path(tempfile.mkdtemp(prefix="mbh", dir="/tmp")).resolve()
    port, token = free_port(), secrets.token_urlsafe(48)
    url = f"http://127.0.0.1:{port}/mcp"
    environment = {
        "PATH": os.pathsep.join((str(Path(executable).parent), os.defpath)),
        "HOME": str(root / "h"),
        "XDG_CONFIG_HOME": str(root / "c"),
        "XDG_STATE_HOME": str(root),
        "XDG_CACHE_HOME": str(root / "k"),
        "XDG_DATA_HOME": str(root / "d"),
        "XDG_RUNTIME_DIR": str(root / "r"),
        "TMPDIR": str(root / "t"),
        "MARABRAIN_TRANSPORT": "http",
        "MARABRAIN_PORT": str(port),
        "MARABRAIN_BEARER_SECRET": token,
        "MARABRAIN_ENGINE_MODE": "managed",
        "MARABRAIN_ENGINE_STARTUP_TIMEOUT_SECONDS": "180",
        "MARABRAIN_WORKSPACE_ROOT": str(root / "w"),
        "MARABRAIN_LOG_LEVEL": "warning",
    }
    for directory in ("h", "c", "k", "d", "r", "t"):
        (root / directory).mkdir(mode=0o700)
    servers: list[LiveServer] = []
    try:
        engine = (await run_cli(["--version"], environment)).decode().splitlines()[0].removeprefix("crystalline ")
        await run_cli(["--json", "model", "download"], environment, timeout=600)
        for workspace in WORKSPACES:
            prepare_workspace(workspace, root / "w")
            context = EngineContext(environment, root, "x").for_workspace(workspace, root / "w")
            shutil.copytree(root / "k", context.environment["XDG_CACHE_HOME"], dirs_exist_ok=True)
        report: dict[str, Any] = {"engine": engine, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "machine": f"{os.uname().sysname} {os.uname().machine}"}
        world = World()
        for name, extra in (("first_run", {}), ("second_run", BOUNDED)):
            server = LiveServer({**environment, **extra}, root / f"{name}.log")
            servers.append(server)
            sampler = Sampler(server)
            server.start()
            async with anyio.create_task_group() as group:
                group.start_soon(sampler.run)
                if name == "first_run":
                    report[name] = await first_run(server, sampler, url, token, world, environment, quick)
                else:
                    report[name] = await second_run(server, sampler, url, token, world, quick)
                group.cancel_scope.cancel()
            print(f"{name} done", file=sys.stderr)
        report["logs"] = {server.log.name: log_findings(server.log) for server in servers}
        output.mkdir(parents=True, exist_ok=True)
        (output / "http_stress.json").write_text(json.dumps(report, indent=2) + "\n")
        (output / "http_stress.md").write_text(markdown(report))
        print(markdown(report))
    finally:
        for server in servers:
            if server.process is not None and server.process.poll() is None:
                await server.stop()
        if keep:
            print(f"kept {root}", file=sys.stderr)
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--output", type=Path, default=REPOSITORY / "benchmarks/results")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary engine root and server logs for inspection.")
    parser.add_argument("--quick", action="store_true", help="Shorten durations and counts to about a quarter for a smoke run.")
    options = parser.parse_args()
    anyio.run(main, options.output.resolve(), options.keep, options.quick)
