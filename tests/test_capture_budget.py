"""Protect logical byte accounting and incremental storage independently of process admission."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from marabrain.config import Settings
from marabrain.engine_context import EngineContext
from marabrain.transient import CaptureBudget, CaptureLimitError, StdoutCapture


@pytest.mark.parametrize("child,total", [(None, 4), (4, 9), (4, 4)])
def test_limits_count_bytes_before_retaining_rejected_chunk(child: int | None, total: int) -> None:
    """Accept the exact UTF-8 payload and preserve it unchanged when another chunk exceeds a limit."""
    capture = StdoutCapture(child, CaptureBudget(total))
    with capture as raw:
        capture.append("\u00fc\u00fc".encode())
        with pytest.raises(CaptureLimitError):
            capture.append(b"x")
        assert raw == "\u00fc\u00fc".encode()
        assert capture.retained_bytes == 4
    assert raw == b""


def test_aliases_charge_once_and_clear_before_exactly_one_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep one raw alias charged through consumption and make repeated disposal harmless."""
    budget = CaptureBudget(4)
    capture = StdoutCapture(None, budget)
    capture.append(b"abcd")
    alias = capture.finish()
    releases: list[int] = []
    original = budget.release

    def release(size: int) -> None:
        """Observe the buffer state at the actual accounting release boundary."""
        assert alias == b""
        releases.append(size)
        original(size)

    monkeypatch.setattr(budget, "release", release)
    with capture as raw:
        assert raw is alias
        with pytest.raises(CaptureLimitError):
            budget.reserve(1)
    capture.close()
    assert releases == [4]
    budget.reserve(4)


def test_tiny_aggregate_capture_does_not_allocate_either_ceiling() -> None:
    """Allocate only small accepted output even when both configured ceilings are enormous."""
    capture = StdoutCapture(1 << 24, CaptureBudget(1 << 24))
    assert capture.output.__alloc__() < 64
    with capture as raw:
        assert raw.__alloc__() < 64
        capture.append(b"{}")
        assert raw.__alloc__() < 128
        assert capture.finish() is raw


def test_aggregate_disabled_capture_preserves_preallocation_and_transfer() -> None:
    """Keep the existing fixed allocation and same-buffer finish under a per-child limit alone."""
    capture = StdoutCapture(100000)
    original = capture.output
    capture.append(b"{}")
    assert len(original) == 100000
    assert original.__alloc__() <= 100064
    with capture as raw:
        assert raw is original
        assert raw == b"{}"


def test_atomic_reservation_refuses_concurrent_owners_without_waiting() -> None:
    """Allow exactly one thread to retain the entire shared budget while both stay live."""
    budget = CaptureBudget(4)
    ready = threading.Barrier(2)
    retained = threading.Barrier(2)

    def compete() -> bool:
        """Race distinct captures for the same bytes and hold the winner until both attempted."""
        capture = StdoutCapture(None, budget)
        try:
            ready.wait(timeout=5)
            try:
                capture.append(b"abcd")
                accepted = True
            except CaptureLimitError:
                accepted = False
            retained.wait(timeout=5)
            return accepted
        finally:
            capture.close()

    with ThreadPoolExecutor(2) as workers:
        results = [workers.submit(compete) for _ in range(2)]
        assert sorted(result.result(timeout=5) for result in results) == [False, True]
    budget.reserve(4)


def test_application_and_standalone_contexts_keep_independent_capture_owners(tmp_path: Path) -> None:
    """Bind one owner to each app and share it with workspace derivatives only."""
    app = EngineContext.for_application(Settings(max_total_capture_bytes=2))
    other = EngineContext.for_application(Settings(max_total_capture_bytes=2))
    standalone = EngineContext.capture(max_total_capture_bytes=2)
    assert app.transients.capture_budget is not None
    assert app.for_workspace("one", tmp_path).transients is app.transients
    assert app.for_workspace("two", tmp_path).transients is app.transients
    assert other.transients.capture_budget is not app.transients.capture_budget
    assert standalone.transients.capture_budget is not app.transients.capture_budget
