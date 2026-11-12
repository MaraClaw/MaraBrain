"""Application-owned immediate subprocess admission, bounded raw stdout storage, and in-flight stdin reservations."""

import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import TracebackType
from typing import Final, Protocol

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


class InputLimitError(TransientLimitError):
    """A write's stdin was refused before dispatch, so nothing reached the engine."""


class InputSizeError(InputLimitError):
    """One write's UTF-8 stdin exceeded the per-command ceiling."""

    def __str__(self) -> str:
        """Return a fixed error without the rejected payload or any size."""
        return "Crystalline write payload exceeds the input size limit; the write was not dispatched."


class InputBudgetError(InputLimitError):
    """The application-wide in-flight stdin budget could not hold one more payload."""

    def __str__(self) -> str:
        """Return a fixed error without the rejected payload or any size."""
        return "Crystalline input capacity is exhausted; the write was not dispatched."


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


class ByteBudget(Protocol):
    """Reserve and release retained capture bytes for one capture owner."""

    def reserve(self, size: int) -> None:
        """Refuse excess bytes immediately before the caller retains the payload."""
        ...

    def release(self, size: int) -> None:
        """Release bytes only after their owning storage has been cleared."""
        ...


class WorkspaceLedger:
    """Count each workspace's held data slots and retained bytes within one application.

    Counters are keyed by workspace ID, so every view derived for the same workspace
    shares one charge. Entries whose charge returns to zero are dropped.
    """

    def __init__(self, max_processes: int | None, max_capture_bytes: int | None) -> None:
        """Start without charges; a disabled limit neither counts nor refuses."""
        self.max_processes = max_processes
        self.max_capture_bytes = max_capture_bytes
        self._processes: dict[str, int] = {}
        self._capture_bytes: dict[str, int] = {}
        self._lock = threading.Lock()

    def reserve_process(self, workspace_id: str) -> None:
        """Charge one data slot to the workspace or raise the fixed admission error."""
        self._reserve(self._processes, workspace_id, 1, self.max_processes, ProcessAdmissionError)

    def release_process(self, workspace_id: str) -> None:
        """Return one data slot charged by a successful reservation."""
        self._release(self._processes, workspace_id, 1, self.max_processes)

    def reserve_bytes(self, workspace_id: str, size: int) -> None:
        """Charge retained bytes to the workspace or raise the fixed capture-limit error."""
        self._reserve(self._capture_bytes, workspace_id, size, self.max_capture_bytes, CaptureLimitError)

    def release_bytes(self, workspace_id: str, size: int) -> None:
        """Return retained bytes charged by a successful reservation."""
        self._release(self._capture_bytes, workspace_id, size, self.max_capture_bytes)

    def held(self, workspace_id: str) -> tuple[int, int]:
        """Return the workspace's currently charged slots and bytes."""
        with self._lock:
            return self._processes.get(workspace_id, 0), self._capture_bytes.get(workspace_id, 0)

    def _reserve(self, held: dict[str, int], workspace_id: str, amount: int, limit: int | None, error: type[TransientLimitError]) -> None:
        """Add a charge atomically unless it would exceed an enabled limit."""
        if limit is None or not amount:
            return
        with self._lock:
            charged = held.get(workspace_id, 0) + amount
            if charged > limit:
                raise error()
            held[workspace_id] = charged

    def _release(self, held: dict[str, int], workspace_id: str, amount: int, limit: int | None) -> None:
        """Subtract a charge atomically and forget the workspace once it holds nothing."""
        if limit is None or not amount:
            return
        with self._lock:
            remaining = held[workspace_id] - amount
            if remaining:
                held[workspace_id] = remaining
            else:
                del held[workspace_id]


class InputBudget:
    """Mutate one application-wide counter of stdin bytes held by in-flight commands atomically."""

    def __init__(self, limit: int) -> None:
        """Start with no reserved stdin bytes."""
        self.limit = limit
        self._reserved = 0
        self._lock = threading.Lock()

    def reserve(self, size: int) -> None:
        """Refuse a payload that does not fit now, without waiting or reserving part of it."""
        with self._lock:
            if self._reserved + size > self.limit:
                raise InputBudgetError()
            self._reserved += size

    def release(self, size: int) -> None:
        """Return the bytes of exactly one earlier successful reservation."""
        with self._lock:
            self._reserved -= size


@dataclass(frozen=True, slots=True)
class TransientRuntime:
    """Keep independent data/control slots shareable across threads and event loops."""

    capacity: int | None = None
    max_capture_bytes: int | None = None
    max_total_capture_bytes: int | None = None
    capture_budget: CaptureBudget | None = field(init=False, repr=False)
    _data: threading.BoundedSemaphore | None = field(init=False, repr=False)
    _control: threading.BoundedSemaphore | None = field(init=False, repr=False)
    max_workspace_transient_processes: int | None = field(default=None, kw_only=True)
    max_workspace_capture_bytes: int | None = field(default=None, kw_only=True)
    workspace_ledger: WorkspaceLedger = field(init=False, repr=False, compare=False)
    max_total_input_bytes: int | None = field(default=None, kw_only=True)
    input_budget: InputBudget | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Create immediate admission counters without an async waiter queue."""
        object.__setattr__(self, "_data", None if self.capacity is None else threading.BoundedSemaphore(self.capacity - 1))
        object.__setattr__(self, "_control", None if self.capacity is None else threading.BoundedSemaphore(1))
        object.__setattr__(self, "capture_budget", None if self.max_total_capture_bytes is None else CaptureBudget(self.max_total_capture_bytes))
        object.__setattr__(self, "workspace_ledger", WorkspaceLedger(self.max_workspace_transient_processes, self.max_workspace_capture_bytes))
        object.__setattr__(self, "input_budget", None if self.max_total_input_bytes is None else InputBudget(self.max_total_input_bytes))

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

    @contextmanager
    def reserve_input(self, size: int) -> Generator[None]:
        """Hold stdin bytes until the caller has reaped its child and joined its workers.

        A command without stdin reserves nothing. A payload that does not fit is refused
        immediately with `InputBudgetError`, and nothing is held. Enter this after `admit()`
        in one `with` statement: a refusal by either leaves the other released, and the
        budget is touched only by commands that will launch a child.
        """
        budget = self.input_budget if size else None
        if budget is not None:
            budget.reserve(size)
        try:
            yield
        finally:
            if budget is not None:
                budget.release(size)

    def for_workspace(self, workspace_id: str) -> WorkspaceTransients | None:
        """Return a data-lane view charging this workspace's share, or None when shares are disabled."""
        if self.max_workspace_transient_processes is None and self.max_workspace_capture_bytes is None:
            return None
        return WorkspaceTransients(self, workspace_id)


@dataclass(frozen=True, slots=True)
class WorkspaceTransients:
    """Layer one workspace's share over the application runtime it references.

    The view holds no counters: charges live in the application's ledger keyed by
    workspace ID, and the application's data slots, control slot, and aggregate byte
    budget stay shared. A refused level rolls back the other, so callers hold both or neither.
    """

    application: TransientRuntime
    workspace_id: str

    @property
    def max_capture_bytes(self) -> int | None:
        """Return the application's per-child stdout ceiling."""
        return self.application.max_capture_bytes

    @property
    def capture_budget(self) -> ByteBudget | None:
        """Return the layered byte budget, or the application budget when bytes have no workspace share."""
        if self.application.workspace_ledger.max_capture_bytes is None:
            return self.application.capture_budget
        return WorkspaceCaptureBudget(self.application.workspace_ledger, self.workspace_id, self.application.capture_budget)

    @contextmanager
    def admit(self) -> Generator[None]:
        """Hold a workspace slot and then an application data slot until the caller's cleanup finishes."""
        ledger = self.application.workspace_ledger
        ledger.reserve_process(self.workspace_id)
        try:
            with self.application.admit():
                yield
        finally:
            ledger.release_process(self.workspace_id)


@dataclass(frozen=True, slots=True)
class WorkspaceCaptureBudget:
    """Reserve retained bytes from a workspace share and then the application budget, or neither."""

    ledger: WorkspaceLedger
    workspace_id: str
    application: CaptureBudget | None

    def reserve(self, size: int) -> None:
        """Charge both levels, rolling back the workspace charge when the application refuses."""
        self.ledger.reserve_bytes(self.workspace_id, size)
        if self.application is not None:
            try:
                self.application.reserve(size)
            except BaseException:
                self.ledger.release_bytes(self.workspace_id, size)
                raise

    def release(self, size: int) -> None:
        """Release the application charge and then the workspace charge."""
        if self.application is not None:
            self.application.release(size)
        self.ledger.release_bytes(self.workspace_id, size)


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

    def __init__(self, limit: int | None, budget: ByteBudget | None = None) -> None:
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
