"""Progress reports from long engine calls, for the UI's progress bar.

The web worker installs a sink that posts messages to the page; natively nothing listens.
``stage`` maps a sub-step onto part of the parent's range, so nested work (a check run
inside the orientation search) still moves one bar from 0 to 1.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable

_sink: Callable[[float, str], None] | None = None
_range = [0.0, 1.0]


def set_sink(fn: Callable[[float, str], None] | None) -> None:
    global _sink
    _sink = fn


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
