"""Immediate atomic admission for resolved domains of final memory commands."""

import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import Final

from fastmcp.exceptions import ToolError


class DomainCommandAdmission:
    """Track mutable command occupancy under one application-owned synchronous lock."""

    def __init__(self, capacity: int | None = None) -> None:
        """Capture one uniform capacity and initialize independent domain occupancy counters."""
        self._capacity: Final[int | None] = capacity
        self._active: dict[str, int] = {}
        self._lock = threading.Lock()

    @contextmanager
    def admit(self, resolved: Sequence[str]) -> Generator[None]:
        """Reserve every distinct resolved domain or immediately refuse without partial ownership."""
        if self._capacity is None:
            yield
            return
        domains = tuple(dict.fromkeys(resolved))
        with self._lock:
            if any(self._active.get(domain, 0) >= self._capacity for domain in domains):
                raise ToolError("Crystalline domain command capacity is exhausted.")
            for domain in domains:
                self._active[domain] = self._active.get(domain, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                for domain in domains:
                    self._active[domain] -= 1
                    if self._active[domain] == 0:
                        del self._active[domain]
