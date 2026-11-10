"""Application-owned immediate subprocess admission and bounded raw stdout storage."""

import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import TracebackType
from typing import Final

READ_BYTES: Final = 65536


class TransientLimitError(RuntimeError):
    """Fixed, payload-free failure of an application subprocess resource limit."""


class ProcessAdmissionError(TransientLimitError):
    """Immediate refusal before creating a transient child or CLI worker."""

    def __str__(self) -> str:
        """Return a fixed error without command arguments or captured output."""
        return "Crystalline transient process capacity is exhausted."


class CaptureLimitError(TransientLimitError):
    """Raw stdout exceeded its ceiling; an interrupted mutation may have applied."""

    def __str__(self) -> str:
        """Return a fixed error without the rejected payload."""
        return "Crystalline output exceeded the capture limit; a mutation may or may not have been applied."


class CaptureBudget:
    """Mutate one application-wide retained-payload counter atomically across capture owners."""

    def __init__(self, limit: int) -> None:
        """Start an empty logical byte budget without reserving payload storage."""
        self.limit = limit
        self._retained = 0
        self._lock = threading.Lock()

    def reserve(self, size: int) -> None:
        """Refuse excess bytes immediately before the caller retains the payload."""
        with self._lock:
            if self._retained + size > self.limit:
                raise CaptureLimitError()
            self._retained += size

    def release(self, size: int) -> None:
        """Release bytes only after their owning storage has been cleared."""
        with self._lock:
            self._retained -= size


@dataclass(frozen=True, slots=True)
class TransientRuntime:
    """Keep independent data/control slots shareable across threads and event loops."""

    capacity: int | None = None
    max_capture_bytes: int | None = None
    max_total_capture_bytes: int | None = None
    capture_budget: CaptureBudget | None = field(init=False, repr=False)
    _data: threading.BoundedSemaphore | None = field(init=False, repr=False)
    _control: threading.BoundedSemaphore | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Create immediate admission counters without an async waiter queue."""
        object.__setattr__(self, "_data", None if self.capacity is None else threading.BoundedSemaphore(self.capacity - 1))
        object.__setattr__(self, "_control", None if self.capacity is None else threading.BoundedSemaphore(1))
        object.__setattr__(self, "capture_budget", None if self.max_total_capture_bytes is None else CaptureBudget(self.max_total_capture_bytes))

    @contextmanager
    def admit(self, *, control: bool = False) -> Generator[None]:
        """Retain a slot until the caller has reaped its child and joined its workers."""
        slots = self._control if control else self._data
        if slots is not None and not slots.acquire(blocking=False):
            raise ProcessAdmissionError()
        try:
            yield
        finally:
            if slots is not None:
                slots.release()


class StdoutCapture:
    """Accumulate raw bytes without retaining any byte beyond the configured ceiling.

    Aggregate-disabled bounded mode reserves S bytes plus at most 64 allocator/terminator bytes;
    writes use memoryviews, and finish transfers that same array without a copy.
    Each owner reads at most READ_BYTES at once, releasing that chunk before the
    next read. Python/OS pipe buffers are additional fixed transport overhead
    (the async backend also buffers its fixed stream window). Parsed JSON and
    caller-owned stdin are outside this raw capture budget. Aggregate mode grows
    incrementally and charges logical payload until cleanup and consumption finish;
    allocator slack and parser objects are outside that bound.
    """

    def __init__(self, limit: int | None, budget: CaptureBudget | None = None) -> None:
        """Allocate incrementally for aggregate accounting, preserving legacy bounded storage."""
        self.limit = limit
        self.budget = budget
        self.output = bytearray(0 if limit is None or budget is not None else limit)
        self.retained_bytes = 0
        self.peak_read_bytes = 0

    def append(self, chunk: bytes) -> None:
        """Check the raw byte count before retaining the next read chunk."""
        self.peak_read_bytes = max(self.peak_read_bytes, len(chunk))
        end = self.retained_bytes + len(chunk)
        if self.limit is not None and end > self.limit:
            raise CaptureLimitError()
        if self.budget is not None:
            self.budget.reserve(len(chunk))
            self.retained_bytes = end
            self.output += chunk
        elif self.limit is not None:
            memoryview(self.output)[self.retained_bytes : end] = chunk
        else:
            self.output += chunk
        self.retained_bytes = end

    def finish(self) -> bytearray:
        """Transfer the one raw buffer after EOF and successful child completion."""
        del self.output[self.retained_bytes :]
        return self.output

    def close(self) -> None:
        """Clear the shared raw storage before releasing its charge, once even with aliases."""
        self.output.clear()
        if self.budget is not None and self.retained_bytes:
            self.budget.release(self.retained_bytes)
        self.retained_bytes = 0

    def __enter__(self) -> bytearray:
        """Expose the same finished buffer under an explicit consumption lifetime."""
        return self.finish()

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        """Discard raw output after successful parsing or any failed consumption."""
        self.close()
