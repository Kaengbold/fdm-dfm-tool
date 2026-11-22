"""Progress reports from long engine calls, for the UI's progress bar.

The web worker installs a sink that posts messages to the page; natively nothing listens.
``stage`` maps a sub-step onto part of the parent's range, so nested work (a check run
inside the orientation search) still moves one bar from 0 to 1. ``partial`` hands the page a usable result
before the call ends (the optimizer's best options so far, T-552): the worker can't answer a second call
while one runs, so "Stop & show best so far" keeps the latest one and cancels.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable

_sink: Callable[[float, str], None] | None = None
_partial_sink: Callable[[dict], None] | None = None
_range = [0.0, 1.0]


def set_sink(fn: Callable[[float, str], None] | None) -> None:
    global _sink
    _sink = fn


def set_partial_sink(fn: Callable[[dict], None] | None) -> None:
    global _partial_sink
    _partial_sink = fn


def wants_partial() -> bool:
    """Whether anyone listens for partial results (building one isn't free)."""
    return _partial_sink is not None


def partial(result: dict) -> None:
    """A result that is usable as it is, sent before the call ends (made JSON-friendly like api.dispatch's)."""
    if _partial_sink is not None:
        from .api import _plain  # lazily: api imports this module

        _partial_sink(_plain(result))


def report(fraction: float, text: str = "") -> None:
    if _sink is not None:
        lo, hi = _range
        _sink(lo + (hi - lo) * min(max(float(fraction), 0.0), 1.0), text)


@contextmanager
def stage(lo: float, hi: float):
    """Inside the block, report(0..1) covers [lo, hi] of the current range."""
    old = list(_range)
    a, b = old
    _range[:] = [a + (b - a) * lo, a + (b - a) * hi]
    try:
        yield
    finally:
        _range[:] = old
