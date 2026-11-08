"""Apply absolute engine deadlines without extending budgets across commands."""

from collections.abc import Generator
from contextlib import contextmanager

import anyio
from fastmcp.exceptions import ToolError


def deadline_after(timeout: float | None) -> float | None:
    """Return an absolute deadline on the active AnyIO clock when a budget is configured."""
    return None if timeout is None else anyio.current_time() + timeout


def check_deadline(deadline: float | None) -> None:
    """Refuse dispatch when the shared operation budget has already expired."""
    if deadline is not None and anyio.current_time() >= deadline:
        raise TimeoutError


@contextmanager
def deadline_scope(deadline: float | None) -> Generator[None]:
    """Bound engine preparation while allowing resource cleanup to finish after expiry."""
    try:
        check_deadline(deadline)
        with anyio.fail_after(None if deadline is None else deadline - anyio.current_time()):
            yield
            check_deadline(deadline)
    except TimeoutError as error:
        raise ToolError("Crystalline operation deadline expired; a mutation may or may not have been applied.") from error
