"""Consume CLI stdout while delivering stdin without extra I/O worker threads."""

import os
import select
import selectors
import subprocess

from marabrain.transient import READ_BYTES, StdoutCapture


def communicate(child: subprocess.Popen[bytes], data: bytes, capture: StdoutCapture) -> bytearray:
    """Multiplex POSIX pipes, accepting early stdin closure like Popen.communicate."""
    assert child.stdout is not None
    offset = 0
    with memoryview(data) as content, selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        if child.stdin is not None:
            selector.register(child.stdin, selectors.EVENT_WRITE)
        while selector.get_map():
            for key, events in selector.select():
                if events & selectors.EVENT_READ:
                    chunk = os.read(key.fd, READ_BYTES)
                    if chunk:
                        capture.append(chunk)
                        del chunk
                    else:
                        selector.unregister(child.stdout)
                        child.stdout.close()
                else:
                    assert child.stdin is not None
                    try:
                        offset += os.write(key.fd, content[offset : offset + select.PIPE_BUF])
                    except BrokenPipeError:
                        offset = len(content)
                    if offset == len(content):
                        selector.unregister(child.stdin)
                        child.stdin.close()
    child.wait()
    return capture.finish()
