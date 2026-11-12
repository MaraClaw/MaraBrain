"""Behavior contracts for immutable scalar cache limits and bounded string reuse."""

from fractions import Fraction

import pytest
from pydantic import JsonValue, ValidationError

from marabrain.compression_cache import CompressionCache
from marabrain.llm_config import LLMSettings


@pytest.mark.parametrize("entries", [None, ""])
@pytest.mark.parametrize("payload_bytes", [None, ""])
def test_empty_cache_environment_disables_limits(monkeypatch: pytest.MonkeyPatch, entries: str | None, payload_bytes: str | None) -> None:
    """Disable cache limits when both environment values are unset or empty."""
    for name, value in (("MAX_ENTRIES", entries), ("MAX_BYTES", payload_bytes)):
        monkeypatch.delenv(f"LLM_COMPRESSION_CACHE_{name}", raising=False)
        if value is not None:
            monkeypatch.setenv(f"LLM_COMPRESSION_CACHE_{name}", value)
    settings = LLMSettings()
    assert settings.compression_cache_max_entries is None and settings.compression_cache_max_bytes is None


@pytest.mark.parametrize("raw", ["1", "24", "+12"])
def test_cache_environment_parses_integer_strings(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Load ordinary integer strings into the strict scalar settings."""
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", raw)
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "1024")
    settings = LLMSettings()
    assert settings.compression_cache_max_entries == int(raw) and settings.compression_cache_max_bytes == 1024


@pytest.mark.parametrize("field", ["compression_cache_max_entries", "compression_cache_max_bytes", "compression_cache_total_max_bytes"])
def test_cache_limits_are_an_immutable_snapshot(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """Capture scalar limits independently of later environment changes and assignment."""
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "2")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "8")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_TOTAL_MAX_BYTES", "16")
    settings = LLMSettings()
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "99")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "999")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_TOTAL_MAX_BYTES", "9999")
    with pytest.raises(ValidationError, match="frozen_instance"):
        setattr(settings, field, 3)
    assert settings.compression_cache_max_entries == 2 and settings.compression_cache_max_bytes == 8
    assert settings.compression_cache_total_max_bytes == 16


@pytest.mark.parametrize("entries,payload_bytes", [("1", None), (None, "2"), ("1", ""), ("", "2")])
def test_incomplete_cache_environment_is_redacted(monkeypatch: pytest.MonkeyPatch, entries: str | None, payload_bytes: str | None) -> None:
    """Reject a lone enabled limit without disclosing the settings input."""
    for name, value in (("MAX_ENTRIES", entries), ("MAX_BYTES", payload_bytes)):
        monkeypatch.delenv(f"LLM_COMPRESSION_CACHE_{name}", raising=False)
        if value is not None:
            monkeypatch.setenv(f"LLM_COMPRESSION_CACHE_{name}", value)
    with pytest.raises(ValidationError) as error:
        LLMSettings()
    assert "input_value" not in str(error.value) and "input_type" not in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        True,
        False,
        0,
        -1,
        1.0,
        1.5,
        Fraction(2, 1),
        Fraction(3, 2),
        "0",
        "-1",
        "1.0",
        "1.5",
        "1/2",
        "1e2",
        "true",
        "invalid-secret",
        "{}",
        "[]",
        "1_000",
        " ",
    ],
)
@pytest.mark.parametrize("field", ["compression_cache_max_entries", "compression_cache_max_bytes", "compression_cache_total_max_bytes"])
def test_invalid_cache_limit_is_redacted(field: str, value: JsonValue | Fraction) -> None:
    """Reject booleans, floats, fractions, malformed text, and nonpositive scalar limits."""
    with pytest.raises(ValidationError) as error:
        LLMSettings.model_validate({"compression_cache_max_entries": 2, "compression_cache_max_bytes": 8, field: value})
    assert "secret" not in str(error.value) and "input_value" not in str(error.value) and "input_type" not in str(error.value)


@pytest.mark.parametrize("raw", ["true", "1.0", "1.5", "1/2", "invalid-secret", "0", "-1", "{}"])
@pytest.mark.parametrize("name", ["MAX_ENTRIES", "MAX_BYTES", "TOTAL_MAX_BYTES"])
def test_invalid_cache_environment_is_redacted(monkeypatch: pytest.MonkeyPatch, name: str, raw: str) -> None:
    """Reject invalid scalar environment values through the settings loader."""
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "2")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "8")
    monkeypatch.setenv(f"LLM_COMPRESSION_CACHE_{name}", raw)
    with pytest.raises(ValidationError) as error:
        LLMSettings()
    assert "secret" not in str(error.value) and "input_value" not in str(error.value) and "input_type" not in str(error.value)


@pytest.mark.parametrize("total", [None, ""])
def test_empty_total_environment_leaves_the_ceiling_disabled(monkeypatch: pytest.MonkeyPatch, total: str | None) -> None:
    """Treat an unset or empty application-wide ceiling as disabled beside a configured pair."""
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "2")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "8")
    monkeypatch.delenv("LLM_COMPRESSION_CACHE_TOTAL_MAX_BYTES", raising=False)
    if total is not None:
        monkeypatch.setenv("LLM_COMPRESSION_CACHE_TOTAL_MAX_BYTES", total)
    assert LLMSettings().compression_cache_total_max_bytes is None


@pytest.mark.parametrize("raw", ["1", "4096", "+12"])
def test_total_environment_parses_integer_strings(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Load ordinary integer strings into the strict application-wide ceiling."""
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "2")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "8")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_TOTAL_MAX_BYTES", raw)
    assert LLMSettings().compression_cache_total_max_bytes == int(raw)


@pytest.mark.parametrize("entries,payload_bytes", [(None, None), ("", ""), ("2", None), (None, "8"), ("2", ""), ("", "8")])
def test_total_requires_the_entry_and_byte_pair_and_is_redacted(monkeypatch: pytest.MonkeyPatch, entries: str | None, payload_bytes: str | None) -> None:
    """Reject a ceiling whose per-pair limits are absent or incomplete without disclosing the input."""
    for name, value in (("MAX_ENTRIES", entries), ("MAX_BYTES", payload_bytes)):
        monkeypatch.delenv(f"LLM_COMPRESSION_CACHE_{name}", raising=False)
        if value is not None:
            monkeypatch.setenv(f"LLM_COMPRESSION_CACHE_{name}", value)
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_TOTAL_MAX_BYTES", "invalid-secret-16")
    with pytest.raises(ValidationError) as rejected:
        LLMSettings()
    assert "secret" not in str(rejected.value) and "input_value" not in str(rejected.value) and "input_type" not in str(rejected.value)
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_TOTAL_MAX_BYTES", "16")
    with pytest.raises(ValidationError) as missing:
        LLMSettings()
    assert "input_value" not in str(missing.value) and "input_type" not in str(missing.value)
    if entries in (None, "") and payload_bytes in (None, ""):
        assert [error["type"] for error in missing.value.errors()] == ["compression_cache_total_without_limits"]


@pytest.mark.parametrize("entries,payload_bytes", [(2, 100), (100, 6)])
async def test_lru_obeys_entry_and_utf8_payload_limits(entries: int, payload_bytes: int) -> None:
    """Refresh hits and evict least-recent strings under independent entry and byte limits."""
    calls: list[str] = []

    async def provider(settings: LLMSettings, query: str, content: str) -> str:
        """Record distinct provider work and return a multibyte payload."""
        calls.append(content)
        return content

    settings = LLMSettings(compression_cache_max_entries=entries, compression_cache_max_bytes=payload_bytes)
    cache = CompressionCache(settings, compressor=provider)
    async with cache.lifespan():
        for content in ("\u00e9\u00e9", "xx", "\u00e9\u00e9", "x", "\u00e9\u00e9", "xx"):
            assert await cache.get("notes", "p", "q", content, workspace_id="test-workspace") == content
    assert calls == ["\u00e9\u00e9", "xx", "x", "xx"]


async def test_oversized_output_is_returned_without_storage_or_eviction() -> None:
    """Leave existing hits intact when a successful string exceeds its domain byte limit."""
    calls: list[str] = []

    async def provider(settings: LLMSettings, query: str, content: str) -> str:
        """Return source-sized results so storage depends on UTF-8 bytes."""
        calls.append(content)
        return content

    settings = LLMSettings(compression_cache_max_entries=2, compression_cache_max_bytes=4)
    cache = CompressionCache(settings, compressor=provider)
    async with cache.lifespan():
        for content in ("fit", "\u00e9\u00e9\u00e9", "fit", "\u00e9\u00e9\u00e9"):
            assert await cache.get("notes", "p", "q", content, workspace_id="test-workspace") == content
    assert calls == ["fit", "\u00e9\u00e9\u00e9", "\u00e9\u00e9\u00e9"]


async def test_exact_identity_and_instances_separate_cached_results() -> None:
    """Separate resolved domains, permalinks, exact queries, full contents, and server instances."""
    calls: list[tuple[str | None, str, str]] = []

    async def provider(settings: LLMSettings, query: str, content: str) -> str:
        """Expose repeated work through a monotonically numbered provider result."""
        calls.append((settings.model, query, content))
        return str(len(calls))

    settings = LLMSettings(compression_cache_max_entries=10, compression_cache_max_bytes=100)
    first, second = CompressionCache(settings, compressor=provider), CompressionCache(settings, compressor=provider)
    identities = [
        ("notes", "p", "q", "body"),
        ("Other", "p", "q", "body"),
        ("notes", "other", "q", "body"),
        ("notes", "p", "q ", "body"),
        ("notes", "p", "q", "body\n"),
    ]
    async with first.lifespan(), second.lifespan():
        results = [await first.get(*identity, workspace_id="test-workspace") for identity in identities]
        assert results == ["1", "2", "3", "4", "5"]
        assert await first.get(*identities[0], workspace_id="test-workspace") == "1"
        assert await second.get(*identities[0], workspace_id="test-workspace") == "6"
        assert await first.get("other", "p", "q", "body", workspace_id="test-workspace") == "7"
        assert await first.get("other", "p", "q", "body", workspace_id="test-workspace") == "7"
    assert len(calls) == 7


@pytest.mark.parametrize("entries,payload_bytes", [(1, 100), (10, 3)])
async def test_all_domains_have_independent_eviction_limits(entries: int, payload_bytes: int) -> None:
    """Keep another domain's hits when one domain exhausts its uniform budget."""
    calls: list[str] = []

    async def provider(settings: LLMSettings, query: str, content: str) -> str:
        """Record work so eviction is observable at the provider boundary."""
        calls.append(content)
        return content

    cache = CompressionCache(LLMSettings(compression_cache_max_entries=entries, compression_cache_max_bytes=payload_bytes), compressor=provider)
    async with cache.lifespan():
        for domain, content in (("notes", "one"), ("Exact Name", "two"), ("notes", "one"), ("notes", "tri"), ("notes", "one"), ("Exact Name", "two")):
            assert await cache.get(domain, "p", "q", content, workspace_id="test-workspace") == content
    assert calls == ["one", "two", "tri", "one"]


async def test_new_lifespan_discards_previous_results() -> None:
    """Release retained strings when an application lifespan shuts down."""
    calls = 0

    async def provider(settings: LLMSettings, query: str, content: str) -> str:
        """Number provider requests across consecutive application lifespans."""
        nonlocal calls
        calls += 1
        return str(calls)

    cache = CompressionCache(LLMSettings(compression_cache_max_entries=1, compression_cache_max_bytes=5), compressor=provider)
    async with cache.lifespan():
        assert await cache.get("notes", "p", "q", "body", workspace_id="test-workspace") == "1"
    async with cache.lifespan():
        assert await cache.get("notes", "p", "q", "body", workspace_id="test-workspace") == "2"


def echoing_cache(*, entries: int = 10, payload_bytes: int = 100, total: int | None = None) -> tuple[CompressionCache, list[str]]:
    """Build a cache whose provider echoes the source and records every call, so each eviction shows as repeated work."""
    calls: list[str] = []

    async def provider(settings: LLMSettings, query: str, content: str) -> str:
        """Record provider work and return the source unchanged."""
        calls.append(content)
        return content

    settings = LLMSettings(compression_cache_max_entries=entries, compression_cache_max_bytes=payload_bytes, compression_cache_total_max_bytes=total)
    return CompressionCache(settings, compressor=provider), calls


def assert_accounting(cache: CompressionCache, ceiling: int | None = None) -> None:
    """Verify the recency order, running total, and bucket map describe exactly the retained entries."""
    retained = {key: value for bucket in cache._domains.values() for key, value in bucket.entries.items()}
    order = list(cache._recency)
    assert len(order) == len(retained) and set(order) == set(retained)
    assert all([key for key in order if key in bucket.entries] == list(bucket.entries) for bucket in cache._domains.values())
    assert cache._total == sum(len(value.encode("utf-8")) for value in retained.values()) == sum(bucket.size for bucket in cache._domains.values())
    assert all(bucket.entries for bucket in cache._domains.values())
    assert ceiling is None or cache._total <= ceiling


async def test_total_ceiling_evicts_the_application_lru_across_buckets_and_hits_refresh_recency() -> None:
    """Evict the least recently used entry of any bucket at the ceiling, and let a hit protect its entry."""
    cache, calls = echoing_cache(total=6)
    memories = {"a": ("w1", "notes", "aa"), "b": ("w2", "notes", "bb"), "c": ("w1", "other", "cc"), "d": ("w2", "other", "dd")}

    async def fetch(name: str) -> None:
        """Read one memory and verify every accounting invariant afterwards."""
        workspace, domain, content = memories[name]
        assert await cache.get(domain, "p", "q", content, workspace_id=workspace) == content
        assert_accounting(cache, 6)

    async with cache.lifespan():
        for name in "abc":
            await fetch(name)
        await fetch("a")
        assert calls == ["aa", "bb", "cc"]
        await fetch("d")
        assert set(cache._domains) == {("w1", "notes"), ("w1", "other"), ("w2", "other")}
        await fetch("a")
        await fetch("c")
        assert calls == ["aa", "bb", "cc", "dd"]
        await fetch("b")
        await fetch("d")
        assert calls == ["aa", "bb", "cc", "dd", "bb", "dd"]


@pytest.mark.parametrize(("entries", "payload_bytes", "retained"), [(2, 100, ["bb", "cc"]), (10, 3, ["cc"])])
async def test_pair_limits_still_apply_below_the_total_ceiling(entries: int, payload_bytes: int, retained: list[str]) -> None:
    """Hold each pair to its own entry and byte limits while the application-wide ceiling still has room."""
    cache, _ = echoing_cache(entries=entries, payload_bytes=payload_bytes, total=100)
    async with cache.lifespan():
        for content in ("aa", "bb", "cc"):
            await cache.get("notes", "p", "q", content, workspace_id="w1")
        await cache.get("notes", "p", "q", "zz", workspace_id="w2")
        assert sorted(cache._domains[("w1", "notes")].entries.values()) == retained
        assert list(cache._domains[("w2", "notes")].entries.values()) == ["zz"]
        assert_accounting(cache, 100)


@pytest.mark.parametrize(("payload_bytes", "total"), [(100, 4), (4, 100)])
async def test_value_over_any_applicable_byte_limit_is_returned_without_storage(payload_bytes: int, total: int) -> None:
    """Return an oversized string uncached and leave fitting entries in place, whichever byte limit it exceeds."""
    cache, calls = echoing_cache(payload_bytes=payload_bytes, total=total)
    async with cache.lifespan():
        for content in ("ab", "\u00e9\u00e9\u00e9", "ab", "\u00e9\u00e9\u00e9"):
            assert await cache.get("notes", "p", "q", content, workspace_id="w1") == content
        assert calls == ["ab", "\u00e9\u00e9\u00e9", "\u00e9\u00e9\u00e9"]
        assert_accounting(cache, 4)


async def test_buckets_left_empty_are_removed_and_cleared_at_shutdown() -> None:
    """Drop a bucket when its last entry is evicted, never create one for unstored results, and reset all accounting on shutdown."""
    cache, _ = echoing_cache(payload_bytes=4, total=2)
    async with cache.lifespan():
        await cache.get("notes", "p", "q", "too long", workspace_id="w1")
        assert cache._domains == {}
        await cache.get("notes", "p", "q", "aa", workspace_id="w1")
        await cache.get("notes", "p", "q", "bb", workspace_id="w2")
        assert list(cache._domains) == [("w2", "notes")]
        assert_accounting(cache, 2)
    assert cache._domains == {} and cache._recency == {} and cache._total == 0


async def test_purge_removes_every_query_for_one_memory_only() -> None:
    """Drop all queries and contents cached for one memory while other permalinks, domains, and workspaces keep their entries."""
    cache, calls = echoing_cache(total=100)
    memories = [("w1", "notes", "p", "q1"), ("w1", "notes", "p", "q2"), ("w1", "notes", "other", "q1"), ("w1", "Other", "p", "q1"), ("w2", "notes", "p", "q1")]
    async with cache.lifespan():
        for workspace, domain, permalink, query in memories:
            await cache.get(domain, permalink, query, "body", workspace_id=workspace)
        await cache.get("notes", "p", "q1", "edited body", workspace_id="w1")
        assert len(calls) == 6
        cache.purge("notes", "p", workspace_id="w1")
        assert_accounting(cache, 100)
        assert len(cache._recency) == 3
        cache.purge("Other", "p", workspace_id="w1")
        assert ("w1", "Other") not in cache._domains
        assert_accounting(cache, 100)
        for workspace, domain, permalink, query in memories:
            await cache.get(domain, permalink, query, "body", workspace_id=workspace)
        assert len(calls) == 9


async def test_purge_without_matching_entries_or_with_caching_disabled_does_nothing() -> None:
    """Treat a purge as a no-op for an uncached memory and for a cache whose limits are unset."""
    cache, calls = echoing_cache()
    disabled = CompressionCache(LLMSettings(), compressor=cache._compressor)
    async with cache.lifespan(), disabled.lifespan():
        await cache.get("notes", "p", "q", "body", workspace_id="w1")
        cache.purge("notes", "absent", workspace_id="w1")
        cache.purge("missing", "p", workspace_id="w1")
        await cache.get("notes", "p", "q", "body", workspace_id="w1")
        assert calls == ["body"]
        disabled.purge("notes", "p", workspace_id="w1")
        assert await disabled.get("notes", "p", "q", "body", workspace_id="w1") == "body"
        assert disabled._domains == {} and disabled._recency == {} and disabled._flights == {}
