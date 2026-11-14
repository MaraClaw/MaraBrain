"""Evaluate and stress MaraBrain's graph-aware memory tools against a real, isolated Crystalline engine.

Run from the repository root with Crystalline 0.32.2 on PATH:

    uv run --no-sync python benchmarks/graph_eval.py --output benchmarks/results

The script starts MaraBrain over MCP stdio in managed mode under a private temporary root,
writes synthetic corpora, and records recall, currentness, integrity, and load results in
report.json and report.md. It never touches the user's own Crystalline state.
"""

import argparse
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from pydantic import BaseModel, ConfigDict

from marabrain.engine_context import EngineContext
from marabrain.workspace_storage import prepare_workspace

WORKSPACE = "graph-eval"
REPOSITORY = Path(__file__).resolve().parents[1]
TEAMS = ["Falcon", "Heron", "Osprey", "Kestrel", "Merlin", "Condor", "Harrier", "Kite", "Plover", "Swift"]
DATASTORES = ["Aurora", "Basalt", "Cobalt", "Dolomite", "Ember", "Flint", "Garnet", "Helix"]
SERVICES = [
    "ledger", "invoice", "refund", "payout", "checkout", "catalog", "pricing", "inventory", "shipping", "returns",
    "loyalty", "coupon", "wallet", "fraud", "identity", "session", "profile", "consent", "audit", "billing",
    "tax", "currency", "settlement", "dispute", "chargeback", "subscription", "metering", "quota", "notification", "email",
    "sms", "webhook", "export", "report", "forecast", "warehouse", "courier", "tracking", "review", "recommendation",
]  # fmt: skip
COMPONENTS = [
    "gateway", "scheduler", "indexer", "crawler", "renderer", "uploader", "resizer", "transcoder", "mailer", "archiver",
    "tokenizer", "router", "balancer", "replicator", "compactor", "sweeper", "notifier", "collector", "aggregator", "exporter",
]  # fmt: skip


class Frozen(BaseModel):
    """Base for parsed native responses; unknown native fields are ignored."""

    model_config = ConfigDict(frozen=True)


class Receipt(Frozen):
    """Parse a create receipt's permalink."""

    permalink: str


class Hit(Frozen):
    """Parse one search hit."""

    permalink: str
    status: str | None = None


class Search(Frozen):
    """Parse search hits and the retrieval mode the engine used."""

    hits: list[Hit]
    mode: str | None = None


class Node(Frozen):
    """Parse one context node."""

    permalink: str


class Context(Frozen):
    """Parse context nodes."""

    nodes: list[Node]


class Target(Frozen):
    """Parse a relation target."""

    target: str


class Relation(Frozen):
    """Parse one relation on a read."""

    resolved: bool
    target: Target


class Read(Frozen):
    """Parse the relations returned by a read."""

    relations: list[Relation]


class Finding(Frozen):
    """Parse one review finding."""

    rule: str
    permalink: str | None = None


class Review(Frozen):
    """Parse one review page."""

    queue: list[Finding]
    total: int


class Embeddings(Frozen):
    """Parse embedding readiness."""

    hybrid_available: bool
    embedded_chunks: int
    total_chunks: int


class Activity(Frozen):
    """Parse the daemon backlog."""

    embedding_backlog: int


class Status(Frozen):
    """Parse daemon status."""

    embeddings: Embeddings
    activity: Activity


@dataclass
class Recorder:
    """Collect per-tool latencies and errors."""

    latencies: dict[str, list[float]] = field(default_factory=dict[str, list[float]])
    errors: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def summary(self, wall: float | None = None) -> dict[str, Any]:
        """Summarize latency percentiles and error counts per tool."""
        tools: dict[str, Any] = {}
        for name in sorted(set(self.latencies) | set(self.errors)):
            values = sorted(self.latencies.get(name, []))
            tools[name] = {
                "ok": len(values),
                "errors": len(self.errors.get(name, [])),
                "error_samples": sorted(set(self.errors.get(name, [])))[:3],
                "p50_ms": percentile(values, 50),
                "p95_ms": percentile(values, 95),
                "p99_ms": percentile(values, 99),
                "mean_ms": round(statistics.fmean(values), 1) if values else None,
            }
        result: dict[str, Any] = {"tools": tools}
        if wall is not None:
            total = sum(len(values) for values in self.latencies.values())
            result["wall_s"] = round(wall, 2)
            result["ok_per_s"] = round(total / wall, 2) if wall else None
        return result


def percentile(values: list[float], rank: int) -> float | None:
    """Return the nearest-rank percentile in milliseconds."""
    if not values:
        return None
    index = max(0, min(len(values) - 1, round(rank / 100 * len(values) + 0.5) - 1))
    return round(values[index] * 1000, 1)


class Bench:
    """Wrap one MCP client with timing and error capture."""

    def __init__(self, client: Client[StdioTransport]) -> None:
        """Bind the connected client and count the retrieval modes searches report."""
        self.client = client
        self.modes: dict[str, int] = {}

    async def call(self, tool: str, arguments: dict[str, Any], recorder: Recorder | None = None) -> dict[str, Any]:
        """Call one tool, record its latency or error, and return structured content (empty on error)."""
        started = time.perf_counter()
        result = await self.client.call_tool(tool, {"workspace_id": WORKSPACE, **arguments}, raise_on_error=False)
        elapsed = time.perf_counter() - started
        if result.is_error or result.structured_content is None:
            text = " ".join(getattr(part, "text", "") for part in result.content)
            if recorder is None:
                raise RuntimeError(f"{tool} failed: {text}")
            recorder.errors.setdefault(tool, []).append(text)
            return {}
        if recorder is not None:
            recorder.latencies.setdefault(tool, []).append(elapsed)
        return dict(result.structured_content)

    async def create(self, domain: str, title: str, content: str, tags: list[str]) -> str:
        """Create one memory and return its permalink."""
        return Receipt.model_validate(await self.call("create_memory", {"domain": domain, "title": title, "content": content, "tags": tags})).permalink

    async def search(self, domain: str, query: str, limit: int, status: str | None = None) -> list[Hit]:
        """Return search hits in rank order."""
        arguments: dict[str, Any] = {"domains": [domain], "query": query, "limit": limit}
        if status is not None:
            arguments["status"] = status
        result = Search.model_validate(await self.call("search_memories", arguments))
        mode = result.mode or "unknown"
        self.modes[mode] = self.modes.get(mode, 0) + 1
        return result.hits

    async def review(self, domain: str) -> list[Finding]:
        """Return every review finding for a domain across pages."""
        findings: list[Finding] = []
        page = 1
        while True:
            result = Review.model_validate(await self.call("review_memories", {"domains": [domain], "limit": 100, "page": page}))
            findings.extend(result.queue)
            if not result.queue or len(findings) >= result.total:
                return findings
            page += 1


async def run_cli(arguments: list[str], environment: dict[str, str], timeout: float = 60) -> bytes:
    """Run one isolated native command and return stdout."""
    with anyio.fail_after(timeout):
        process = await anyio.run_process(["crystalline", *arguments], env=environment, check=False)
    if process.returncode != 0:
        raise RuntimeError(f"crystalline {arguments[:3]} failed: {process.stderr.decode()[:300]}")
    return process.stdout


async def wait_for_embeddings(domains: list[str], environment: dict[str, str]) -> tuple[float, Embeddings]:
    """Sync and embed the domains, then wait until every chunk is embedded; return the wait and the final counts."""
    started = time.perf_counter()
    for domain in domains:
        await run_cli(["--json", "ctl", "sync", "--domain", domain, "--embed"], environment, timeout=300)
    with anyio.fail_after(600):
        while True:
            status = Status.model_validate_json(await run_cli(["--json", "ctl", "status"], environment))
            embedded = status.embeddings
            if embedded.hybrid_available and embedded.embedded_chunks == embedded.total_chunks > 0 and status.activity.embedding_backlog == 0:
                return time.perf_counter() - started, embedded
            await anyio.sleep(0.25)


def title_case(word: str) -> str:
    """Capitalize a corpus word for a title."""
    return word[:1].upper() + word[1:]


async def build_org(bench: Bench, domain: str) -> tuple[dict[str, str], list[tuple[str, str, str, str]]]:
    """Write the multi-hop corpus; return title-to-permalink and (kind, query, gold permalink, service permalink) rows."""
    permalinks: dict[str, str] = {}
    for index, team in enumerate(TEAMS):
        content = (
            f"Team {team} is an engineering team.\n\n- [fact] Team {team} handles on-call through pager rotation PR-{100 + index} #oncall\n"
            f"- [fact] Team {team} escalates incidents to its engineering manager #oncall\n"
        )
        permalinks[f"Team {team}"] = await bench.create(domain, f"Team {team}", content, ["team"])
    for index, store in enumerate(DATASTORES):
        content = (
            f"The {store} datastore stores durable records.\n\n- [fact] The {store} datastore runs engine version {12 + index}.{index} #datastore\n"
            f"- [fact] The {store} datastore is backed up nightly #datastore\n"
        )
        permalinks[f"{store} Datastore"] = await bench.create(domain, f"{store} Datastore", content, ["datastore"])
    questions: list[tuple[str, str, str, str]] = []
    for index, noun in enumerate(SERVICES):
        team, store = TEAMS[index % len(TEAMS)], DATASTORES[(index * 3) % len(DATASTORES)]
        title = f"{title_case(noun)} Service"
        content = (
            f"The {noun} service processes {noun} requests for customers.\n\n- [fact] The {noun} service exposes a gRPC API #service\n"
            f"- [fact] The {noun} service scales horizontally #service\n- owned_by [[Team {team}]]\n- depends_on [[{store} Datastore]]\n"
        )
        permalinks[title] = await bench.create(domain, title, content, ["service"])
        questions.append(("oncall", f"{noun} service on-call pager rotation", permalinks[f"Team {team}"], permalinks[title]))
        questions.append(("version", f"{noun} service datastore engine version", permalinks[f"{store} Datastore"], permalinks[title]))
    return permalinks, questions


async def evaluate_recall(bench: Bench, domain: str, questions: list[tuple[str, str, str, str]]) -> dict[str, Any]:
    """E1: compare gold recall at a five-memory budget for search-only, graph, and read-follow strategies."""
    arms: dict[str, dict[str, list[float]]] = {
        name: {"hit": [], "calls": [], "retrieved": []} for name in ("search_top5", "search_top1_context", "search_top1_read_follow")
    }
    by_kind: dict[str, dict[str, list[float]]] = {}
    anchors: list[float] = []

    def record(arm: str, kind: str, gold: str, retrieved: set[str], calls: int) -> None:
        """Store one strategy outcome."""
        hit = 1.0 if gold in retrieved else 0.0
        arms[arm]["hit"].append(hit)
        arms[arm]["calls"].append(calls)
        arms[arm]["retrieved"].append(len(retrieved))
        by_kind.setdefault(f"{arm}/{kind}", {"hit": []})["hit"].append(hit)

    for kind, query, gold, service in questions:
        top5 = await bench.search(domain, query, 5)
        record("search_top5", kind, gold, {hit.permalink for hit in top5}, 1)
        top1 = await bench.search(domain, query, 1)
        if not top1:
            record("search_top1_context", kind, gold, set(), 1)
            record("search_top1_read_follow", kind, gold, set(), 1)
            continue
        anchor = top1[0].permalink
        anchors.append(1.0 if anchor == service else 0.0)
        context = Context.model_validate(await bench.call("build_memory_context", {"domain": domain, "permalink": anchor, "depth": 1, "max_related": 4}))
        record("search_top1_context", kind, gold, {node.permalink for node in context.nodes}, 2)
        read = Read.model_validate(await bench.call("read_memory", {"domain": domain, "permalink": anchor}))
        followed = {anchor}
        calls = 2
        for relation in read.relations[:4]:
            if relation.resolved:
                calls += 1
                followed.update(hit.permalink for hit in await bench.search(domain, relation.target.target, 1))
        record("search_top1_read_follow", kind, gold, followed, calls)
    return {
        "questions": len(questions),
        "search_top1_is_the_named_service": round(sum(anchors) / len(questions), 3),
        "arms": {
            name: {
                "recall": round(statistics.fmean(values["hit"]), 3),
                "mean_calls": round(statistics.fmean(values["calls"]), 2),
                "mean_retrieved": round(statistics.fmean(values["retrieved"]), 2),
            }
            for name, values in arms.items()
        },
        "recall_by_kind": {key: round(statistics.fmean(values["hit"]), 3) for key, values in sorted(by_kind.items())},
    }


async def build_currentness(bench: Bench, plain: str, lifecycle: str) -> dict[str, tuple[str, str, str]]:
    """E2 corpus: every component fact is written twice; only the lifecycle domain retires the old version."""
    rows: dict[str, tuple[str, str, str]] = {}
    for index, component in enumerate(COMPONENTS):
        old_value, new_value = 10 + index, 40 + index
        old_title, new_title = f"{title_case(component)} timeout setting", f"{title_case(component)} timeout setting revised"

        def body(value: int, extra: str = "", component: str = component) -> str:
            """Render one version of the component fact."""
            return (
                f"The {component} component request timeout setting.\n\n- [fact] The {component} timeout is {value} seconds #config\n"
                f"- [fact] The timeout applies to every {component} request #config\n{extra}"
            )

        await bench.create(plain, old_title, body(old_value), ["config"])
        plain_new = await bench.create(plain, new_title, body(new_value), ["config"])
        old = await bench.create(lifecycle, old_title, body(old_value), ["config"])
        new = await bench.create(lifecycle, new_title, body(new_value, f"- supersedes [[{old_title}]]\n"), ["config"])
        target = {"domain": lifecycle, "permalink": old}
        await bench.call("edit_memory", {**target, "operation": {"type": "set_frontmatter", "key": "status", "value": "superseded"}})
        await bench.call("edit_memory", {**target, "operation": {"type": "append", "content": f"- superseded_by [[{new_title}]]"}})
        assert plain_new == new
        rows[component] = (f"current {component} timeout seconds", new, old)
    return rows


async def evaluate_currentness(bench: Bench, plain: str, lifecycle: str, rows: dict[str, tuple[str, str, str]]) -> dict[str, Any]:
    """E2: top-1 current rate and stale-in-top-3 rate with and without the supersede lifecycle."""
    arms: dict[str, tuple[str, str | None]] = {
        "no_lifecycle": (plain, None),
        "superseded_ranking_only": (lifecycle, None),
        "superseded_status_stable": (lifecycle, "stable"),
    }
    result: dict[str, Any] = {"facts": len(rows), "arms": {}}
    for arm, (domain, status) in arms.items():
        top1: list[float] = []
        stale: list[float] = []
        for query, new, old in rows.values():
            hits = [hit.permalink for hit in await bench.search(domain, query, 3, status)]
            top1.append(1.0 if hits[:1] == [new] else 0.0)
            stale.append(1.0 if old in hits else 0.0)
        result["arms"][arm] = {"top1_current_rate": round(statistics.fmean(top1), 3), "stale_in_top3_rate": round(statistics.fmean(stale), 3)}
    return result


async def evaluate_integrity(bench: Bench, domain: str, count: int) -> dict[str, Any]:
    """E3: inject dangling links and references to retired memories, then measure review detection."""
    dangling: set[str] = set()
    consumers: set[str] = set()
    clean: set[str] = set()
    for index in range(count):
        lead = f"Integrity fixture {index}.\n\n- [fact] Fixture {index} exists for the review evaluation #fixture\n- [fact] It is synthetic #fixture\n"
        old = f"Old Module {index}"
        old_permalink = await bench.create(domain, old, lead, ["module"])
        await bench.create(domain, f"New Module {index}", lead + f"- supersedes [[{old}]]\n", ["module"])
        await bench.call(
            "edit_memory", {"domain": domain, "permalink": old_permalink, "operation": {"type": "set_frontmatter", "key": "status", "value": "superseded"}}
        )
        await bench.call(
            "edit_memory", {"domain": domain, "permalink": old_permalink, "operation": {"type": "append", "content": f"- superseded_by [[New Module {index}]]"}}
        )
        dangling.add(await bench.create(domain, f"Dangling Note {index}", lead + f"- depends_on [[Ghost Component {index}]]\n", ["note"]))
        consumers.add(await bench.create(domain, f"Consumer {index}", lead + f"- depends_on [[{old}]]\n", ["note"]))
        clean.add(await bench.create(domain, f"Clean Note {index}", lead + f"- depends_on [[New Module {index}]]\n", ["note"]))
    findings = await bench.review(domain)
    flagged: dict[str, set[str]] = {}
    for finding in findings:
        if finding.permalink is not None:
            flagged.setdefault(finding.rule, set()).add(finding.permalink)
    rules = {rule: len(permalinks) for rule, permalinks in sorted(flagged.items())}
    return {
        "injected_each": count,
        "dangling_link_recall_V102": round(len(dangling & flagged.get("V102", set())) / count, 3),
        "retired_reference_recall_V101": round(len(consumers & flagged.get("V101", set())) / count, 3),
        "clean_false_positives_V101_V102": len(clean & (flagged.get("V101", set()) | flagged.get("V102", set()))),
        "findings_total": len(findings),
        "flagged_memories_by_rule": rules,
    }


async def run_pool(workers: int, jobs: list[Callable[[], Awaitable[None]]]) -> float:
    """Run jobs with a fixed number of concurrent workers and return wall time."""
    pending = list(reversed(jobs))

    async def worker() -> None:
        """Drain jobs until none remain."""
        while pending:
            await pending.pop()()

    started = time.perf_counter()
    async with anyio.create_task_group() as group:
        for _ in range(workers):
            group.start_soon(worker)
    return time.perf_counter() - started


async def stress_store(bench: Bench, levels: list[int], operations: int) -> dict[str, Any]:
    """S1: concurrent creates carrying relations, then a review of graph consistency."""
    result: dict[str, Any] = {}
    for workers in levels:
        domain = f"stress-w{workers}"
        recorder = Recorder()

        def job(index: int, domain: str = domain, recorder: Recorder = recorder) -> Callable[[], Awaitable[None]]:
            """Build one create that links to the previous item."""

            async def run() -> None:
                """Create the item."""
                link = f"- depends_on [[Stress Item {index - 1}]]\n" if index else ""
                content = (
                    f"Stress item {index} for load testing.\n\n- [fact] Item {index} carries a payload #stress\n- [fact] Written concurrently #stress\n{link}"
                )
                await bench.call("create_memory", {"domain": domain, "title": f"Stress Item {index}", "content": content, "tags": ["stress"]}, recorder)

            return run

        await bench.call("create_memory", {"domain": domain, "title": "Stress seed", "content": "Seed.\n\n- [fact] Seed #stress\n- [fact] Seed two #stress\n"})
        wall = await run_pool(workers, [job(index) for index in range(operations)])
        unresolved = sum(1 for finding in await bench.review(domain) if finding.rule == "V102")
        result[f"workers_{workers}"] = {**recorder.summary(wall), "operations": operations, "unresolved_links_after": unresolved}
    return result


async def stress_reads(bench: Bench, domain: str, permalinks: list[str], levels: list[int], operations: int) -> dict[str, Any]:
    """S2: mixed search, read, and context load against the multi-hop corpus."""
    result: dict[str, Any] = {}
    generator = random.Random(7)
    for workers in levels:
        recorder = Recorder()

        def job(index: int, recorder: Recorder = recorder) -> Callable[[], Awaitable[None]]:
            """Build one operation of the rotating mix."""
            permalink = generator.choice(permalinks)
            noun = generator.choice(SERVICES)

            async def run() -> None:
                """Run the operation."""
                match index % 3:
                    case 0:
                        await bench.call("search_memories", {"domains": [domain], "query": f"{noun} service owner", "limit": 10}, recorder)
                    case 1:
                        await bench.call("read_memory", {"domain": domain, "permalink": permalink}, recorder)
                    case _:
                        await bench.call("build_memory_context", {"domain": domain, "permalink": permalink, "depth": 2}, recorder)

            return run

        wall = await run_pool(workers, [job(index) for index in range(operations)])
        result[f"workers_{workers}"] = {**recorder.summary(wall), "operations": operations}
    return result


def markdown(report: dict[str, Any]) -> str:
    """Render the report as Markdown tables."""
    lines = [
        "# MaraBrain graph evaluation",
        "",
        f"Crystalline {report['engine']}, MaraBrain over MCP stdio (managed mode), {report['started']}, on {report['machine']}.",
        f"Corpus write time {report['setup']['corpus_write_s']} s; embedding wait {report['setup']['embedding_wait_s']} s "
        f"({report['setup']['embedded_chunks']}/{report['setup']['total_chunks']} chunks embedded); search modes {report['setup']['search_modes']}.",
        "",
        "## E1 multi-hop recall (budget: at most 5 memories)",
        "",
        "| Strategy | Recall | Mean tool calls | Mean memories retrieved |",
        "| --- | --- | --- | --- |",
    ]
    lines.insert(-3, f"Search top-1 is the service the question names in {report['E1_multi_hop']['search_top1_is_the_named_service']} of questions.")
    for name, arm in report["E1_multi_hop"]["arms"].items():
        lines.append(f"| {name} | {arm['recall']} | {arm['mean_calls']} | {arm['mean_retrieved']} |")
    lines += ["", "| Strategy/question | Recall |", "| --- | --- |"]
    lines += [f"| {key} | {value} |" for key, value in report["E1_multi_hop"]["recall_by_kind"].items()]
    lines += ["", "## E2 currentness", "", "| Arm | Top-1 current | Stale in top 3 |", "| --- | --- | --- |"]
    for name, arm in report["E2_currentness"]["arms"].items():
        lines.append(f"| {name} | {arm['top1_current_rate']} | {arm['stale_in_top3_rate']} |")
    integrity = report["E3_integrity"]
    lines += [
        "",
        "## E3 integrity review",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        *(f"| {key} | {value} |" for key, value in integrity.items()),
    ]
    for section, title in (("S1_store", "S1 concurrent store"), ("S2_mixed_read", "S2 mixed read load")):
        lines += [
            "",
            f"## {title}",
            "",
            "| Workers | Tool | OK | Errors | p50 ms | p95 ms | p99 ms | Wall s | OK/s | Notes |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for level, data in report[section].items():
            for tool, stats in data["tools"].items():
                note = f"unresolved links after: {data['unresolved_links_after']}" if "unresolved_links_after" in data else ""
                if stats["error_samples"]:
                    note = (note + "; " if note else "") + "errors: " + " / ".join(stats["error_samples"])
                lines.append(
                    f"| {level.removeprefix('workers_')} | {tool} | {stats['ok']} | {stats['errors']} | {stats['p50_ms']} | {stats['p95_ms']} | "
                    f"{stats['p99_ms']} | {data['wall_s']} | {data['ok_per_s']} | {note} |"
                )
    return "\n".join(lines) + "\n"


async def main(output: Path, keep: bool, store_levels: list[int], read_levels: list[int]) -> None:
    """Prepare an isolated engine, run every evaluation, and write the reports."""
    executable = shutil.which("crystalline")
    if executable is None:
        raise SystemExit("Install Crystalline 0.32.2 on PATH first.")
    root = Path(tempfile.mkdtemp(prefix="mbb", dir="/tmp")).resolve()
    environment = {
        "PATH": os.pathsep.join((str(Path(executable).parent), os.defpath)),
        "HOME": str(root / "h"),
        "XDG_CONFIG_HOME": str(root / "c"),
        "XDG_STATE_HOME": str(root),
        "XDG_CACHE_HOME": str(root / "k"),
        "XDG_DATA_HOME": str(root / "d"),
        "XDG_RUNTIME_DIR": str(root / "r"),
        "TMPDIR": str(root / "t"),
        "MARABRAIN_TRANSPORT": "stdio",
        "MARABRAIN_ENGINE_MODE": "managed",
        "MARABRAIN_ENGINE_STARTUP_TIMEOUT_SECONDS": "180",
        "MARABRAIN_WORKSPACE_ROOT": str(root / "w"),
        "MARABRAIN_LOG_LEVEL": "warning",
    }
    for directory in ("h", "c", "k", "d", "r", "t"):
        (root / directory).mkdir(mode=0o700)
    try:
        engine = (await run_cli(["--version"], environment)).decode().splitlines()[0].removeprefix("crystalline ")
        await run_cli(["--json", "model", "download"], environment, timeout=600)
        prepare_workspace(WORKSPACE, root / "w")
        context = EngineContext(environment, root, "graph-eval").for_workspace(WORKSPACE, root / "w")
        shutil.copytree(root / "k", context.environment["XDG_CACHE_HOME"], dirs_exist_ok=True)
        workspace_environment = dict(context.environment)
        transport = StdioTransport(
            command=sys.executable, args=["-m", "marabrain"], cwd=str(REPOSITORY), env=environment, keep_alive=False, log_file=root / "server.log"
        )
        report: dict[str, Any] = {"engine": engine, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "machine": f"{os.uname().sysname} {os.uname().machine}"}
        async with Client(transport, timeout=120, init_timeout=180) as client:
            bench = Bench(client)
            started = time.perf_counter()
            permalinks, questions = await build_org(bench, "org")
            rows = await build_currentness(bench, "curr-plain", "curr-lifecycle")
            corpus_write = time.perf_counter() - started
            embedding_wait, embedded = await wait_for_embeddings(["org", "curr-plain", "curr-lifecycle"], workspace_environment)
            setup: dict[str, Any] = {
                "corpus_write_s": round(corpus_write, 1),
                "embedding_wait_s": round(embedding_wait, 1),
                "embedded_chunks": embedded.embedded_chunks,
                "total_chunks": embedded.total_chunks,
            }
            report["setup"] = setup
            report["E1_multi_hop"] = await evaluate_recall(bench, "org", questions)
            report["E2_currentness"] = await evaluate_currentness(bench, "curr-plain", "curr-lifecycle", rows)
            report["E3_integrity"] = await evaluate_integrity(bench, "integrity", 10)
            report["S1_store"] = await stress_store(bench, store_levels, 48)
            report["S2_mixed_read"] = await stress_reads(bench, "org", sorted(permalinks.values()), read_levels, 150)
            setup["search_modes"] = bench.modes
        output.mkdir(parents=True, exist_ok=True)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        (output / "report.md").write_text(markdown(report))
        print(markdown(report))
    finally:
        if keep:
            print(f"kept {root}", file=sys.stderr)
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--output", type=Path, default=REPOSITORY / "benchmarks/results")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary engine root for inspection.")
    parser.add_argument("--store-workers", type=int, nargs="+", default=[1, 4, 8])
    parser.add_argument("--read-workers", type=int, nargs="+", default=[4, 8, 16])
    options = parser.parse_args()
    anyio.run(main, options.output.resolve(), options.keep, options.store_workers, options.read_workers)
