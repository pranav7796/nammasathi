"""Per-turn stage timings, so a slow turn can be attributed without guessing.

A timer lives in a context variable, which `asyncio.to_thread` copies, so extraction running in
a worker thread still reports to the turn that started it. Only numbers are recorded.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

STAGES = ("stt", "understanding", "provider", "speech")


@dataclass
class TurnTimer:
    started: float = field(default_factory=time.perf_counter)
    spent: dict[str, float] = field(default_factory=dict[str, float])
    understood_by: str = ""

    def report(self) -> dict[str, int]:
        """Milliseconds per stage. Orchestration is whatever the named stages do not explain."""
        total = time.perf_counter() - self.started
        result = {name: round(self.spent.get(name, 0.0) * 1000) for name in STAGES}
        explained = sum(self.spent.get(name, 0.0) for name in STAGES)
        result["orchestration"] = round(max(total - explained, 0.0) * 1000)
        result["total"] = round(total * 1000)
        return result


_current: ContextVar[TurnTimer | None] = ContextVar("turn_timer", default=None)


def begin_turn() -> TurnTimer:
    timer = TurnTimer()
    _current.set(timer)
    return timer


@contextmanager
def stage(name: str) -> Generator[None]:
    timer = _current.get()
    started = time.perf_counter()
    try:
        yield
    finally:
        if timer is not None:
            timer.spent[name] = timer.spent.get(name, 0.0) + time.perf_counter() - started


def note_understood_by(source: str) -> None:
    timer = _current.get()
    if timer is not None:
        timer.understood_by = source
