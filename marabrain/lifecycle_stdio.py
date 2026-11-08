"""Cancellable standard-input reads for the application's stdio lifecycle."""

import os
import stat
from collections import deque
from typing import TextIO

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream


class CancellableInput(anyio.AsyncFile[str]):
    """Accumulate protocol lines without a blocking worker that prevents shutdown."""

    def __init__(self, stream: TextIO) -> None:
        """Wrap the sole protocol input and retain its partial line buffer."""
        super().__init__(stream)
        self._pending = b""

    async def readline(self) -> str:
        """Return the next UTF-8 protocol line or EOF while allowing cancellation."""
        while b"\n" not in self._pending:
            descriptor = self.wrapped.fileno()
            mode = os.fstat(descriptor).st_mode
            if stat.S_ISREG(mode) or (stat.S_ISCHR(mode) and not os.isatty(descriptor)):
                chunk = await anyio.to_thread.run_sync(os.read, descriptor, 65536, abandon_on_cancel=True)
            else:
                await anyio.wait_readable(descriptor)
                chunk = os.read(descriptor, 65536)
            if not chunk:
                line, self._pending = self._pending, b""
                return line.decode("utf-8", errors="replace")
            self._pending += chunk
        line, self._pending = self._pending.split(b"\n", 1)
        return line.decode("utf-8", errors="replace") + "\n"


class BufferedInput(anyio.AsyncFile[str]):
    """Deliver lines buffered by the startup EOF watcher to the MCP transport."""

    def __init__(self, stream: TextIO, lines: MemoryObjectReceiveStream[str], startup_lines: deque[str]) -> None:
        """Retain the original descriptor and the single-reader protocol channel."""
        super().__init__(stream)
        self._lines = lines
        self._startup_lines = startup_lines

    async def readline(self) -> str:
        """Return a queued protocol line or EOF when the input watcher finishes."""
        if self._startup_lines:
            return self._startup_lines.popleft()
        try:
            return await self._lines.receive()
        except anyio.EndOfStream:
            return ""
