"""Process readiness and polling diagnostics, separate from feed freshness."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any


@dataclass
class LoopStatus:
    interval_seconds: float
    registered_at: float
    last_started: float | None = None
    last_finished: float | None = None
    last_success_at: str | None = None
    last_error: str | None = None


class RuntimeMonitor:
    def __init__(self) -> None:
        self.loops: dict[str, LoopStatus] = {}

    def register(self, name: str, interval_seconds: float) -> None:
        self.loops[name] = LoopStatus(interval_seconds, monotonic())

    @contextmanager
    def cycle(self, name: str) -> Iterator[None]:
        status = self.loops[name]
        status.last_started = monotonic()
        try:
            yield
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status.last_error = type(exc).__name__
            raise
        else:
            status.last_success_at = datetime.now(UTC).isoformat()
            status.last_error = None
        finally:
            status.last_finished = monotonic()

    def payload(self, state: Any) -> tuple[bool, dict[str, object]]:
        now = monotonic()
        ready = True
        loops: dict[str, object] = {}
        for name, status in self.loops.items():
            task = getattr(state, name, None)
            stopped = task is None or task.done()
            anchor = (
                status.last_finished if status.last_finished is not None else status.registered_at
            )
            # Allow slow provider cycles and startup warming without making
            # external service availability a prerequisite for cached reads.
            overdue = now - anchor > max(180.0, status.interval_seconds * 3 + 60)
            ready = ready and not stopped and not overdue
            loops[name] = {
                "running": not stopped,
                "overdue": overdue,
                "cycle_age_seconds": round(now - anchor, 1),
                "last_cycle_started_seconds_ago": (
                    round(now - status.last_started, 1)
                    if status.last_started is not None else None
                ),
                "last_success_at": status.last_success_at,
                "last_error": status.last_error,
            }
        return ready, loops


@contextmanager
def polling_cycle(state: Any, name: str) -> Iterator[None]:
    """Tests and standalone loops may not install a process monitor."""
    monitor = getattr(state, "runtime_monitor", None)
    if isinstance(monitor, RuntimeMonitor):
        with monitor.cycle(name):
            yield
    else:
        yield


def database_status(path: Path) -> dict[str, object]:
    """Read-only probe: never silently create a missing runtime database."""
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as conn:
            quotes, oldest_quote, quote_count, stale_count = conn.execute(
                "SELECT MAX(timestamp), MIN(timestamp), COUNT(*), "
                "COALESCE(SUM(is_stale), 0) FROM latest_quotes"
            ).fetchone()
            bars = conn.execute(
                "SELECT MAX(timestamp) FROM bars WHERE interval = '1d'"
            ).fetchone()[0]
        now = datetime.now(UTC)

        def age(value: str | None) -> float | None:
            if value is None:
                return None
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            return round(max(0.0, (now - stamp).total_seconds()), 1)

        return {
            "available": True,
            "newest_quote_at": quotes,
            "newest_quote_age_seconds": age(quotes),
            "oldest_quote_at": oldest_quote,
            "oldest_quote_age_seconds": age(oldest_quote),
            "quote_count": quote_count,
            "stale_quote_count": stale_count,
            "newest_daily_bar_at": bars,
            "newest_daily_bar_age_seconds": age(bars),
        }
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {"available": False, "error": type(exc).__name__}
