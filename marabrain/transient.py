"""Application-owned immediate subprocess admission and bounded raw stdout storage."""

import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
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


@dataclass(frozen=True, slots=True)
class TransientRuntime:
    """Keep independent data/control slots shareable across threads and event loops."""

    capacity: int | None = None
    max_capture_bytes: int | None = None
    _data: threading.BoundedSemaphore | None = field(init=False, repr=False)
    _control: threading.BoundedSemaphore | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Create immediate admission counters without an async waiter queue."""
        object.__setattr__(self, "_data", None if self.capacity is None else threading.BoundedSemaphore(self.capacity - 1))
        object.__setattr__(self, "_control", None if self.capacity is None else threading.BoundedSemaphore(1))

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

    Bounded mode reserves S payload bytes plus at most 64 allocator/terminator bytes;
    writes use memoryviews, and finish transfers that same array without a copy.
    Each owner reads at most READ_BYTES at once, releasing that chunk before the
    next read. Python/OS pipe buffers are additional fixed transport overhead
    (the async backend also buffers its fixed stream window). Parsed JSON and
    caller-owned stdin are outside this raw capture budget.
    """

    def __init__(self, limit: int | None) -> None:
        """Reserve bounded storage once, avoiding geometric growth or a final raw copy."""
        self.limit = limit
        self.output = bytearray(0 if limit is None else limit)
        self.retained_bytes = 0
        self.peak_read_bytes = 0

    def append(self, chunk: bytes) -> None:
        """Check the raw byte count before retaining the next read chunk."""
        self.peak_read_bytes = max(self.peak_read_bytes, len(chunk))
        end = self.retained_bytes + len(chunk)
        if self.limit is not None:
            if end > self.limit:
                raise CaptureLimitError()
            memoryview(self.output)[self.retained_bytes : end] = chunk
        else:
            self.output += chunk
        self.retained_bytes = end

    def finish(self) -> bytearray:
        """Transfer the one raw buffer after EOF and successful child completion."""
        del self.output[self.retained_bytes :]
        return self.output
