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


@pytest.mark.parametrize("field", ["compression_cache_max_entries", "compression_cache_max_bytes"])
def test_cache_limits_are_an_immutable_snapshot(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """Capture scalar limits independently of later environment changes and assignment."""
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "2")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "8")
    settings = LLMSettings()
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "99")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "999")
    with pytest.raises(ValidationError, match="frozen_instance"):
        setattr(settings, field, 3)
    assert settings.compression_cache_max_entries == 2 and settings.compression_cache_max_bytes == 8


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
@pytest.mark.parametrize("field", ["compression_cache_max_entries", "compression_cache_max_bytes"])
def test_invalid_cache_limit_is_redacted(field: str, value: JsonValue | Fraction) -> None:
    """Reject booleans, floats, fractions, malformed text, and nonpositive scalar limits."""
    with pytest.raises(ValidationError) as error:
        LLMSettings.model_validate({"compression_cache_max_entries": 2, "compression_cache_max_bytes": 8, field: value})
    assert "secret" not in str(error.value) and "input_value" not in str(error.value) and "input_type" not in str(error.value)


@pytest.mark.parametrize("raw", ["true", "1.0", "1.5", "1/2", "invalid-secret", "0", "-1", "{}"])
@pytest.mark.parametrize("name", ["MAX_ENTRIES", "MAX_BYTES"])
def test_invalid_cache_environment_is_redacted(monkeypatch: pytest.MonkeyPatch, name: str, raw: str) -> None:
    """Reject invalid scalar environment values through the settings loader."""
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_ENTRIES", "2")
    monkeypatch.setenv("LLM_COMPRESSION_CACHE_MAX_BYTES", "8")
    monkeypatch.setenv(f"LLM_COMPRESSION_CACHE_{name}", raw)
    with pytest.raises(ValidationError) as error:
        LLMSettings()
    assert "secret" not in str(error.value) and "input_value" not in str(error.value) and "input_type" not in str(error.value)


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
