"""The /api/market-context digest: continuous market memory for agents.

Hermes runs stateless cron jobs on another box; this single endpoint gives
it history instead of a moment — daily board snapshots, 5d/20d watchlist
movers from cached daily bars, accrued ETF flow history, the next week of
key dates, and its own fringe book with P&L. Everything degrades to empty
on failure: an agent brief must never be blocked by one broken piece.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

from app import db
from app.models import Bar, GroupConfig, ProviderName
from app.services.econ_calendar import EconCalendarService, key_dates_payload
from app.services.fringe import FringeService

logger = logging.getLogger(__name__)

T = TypeVar("T")

MIN_DAYS = 7
MAX_DAYS = 90
KEY_DATES_AHEAD_DAYS = 7
KEY_DATES_LIMIT = 50
MOVER_WINDOWS = (5, 20)
MOVER_LIMIT = 10


def clamp_days(days: int) -> int:
    """Silently bound the window: the caller is a bot, not a form."""
    return max(MIN_DAYS, min(MAX_DAYS, days))


async def market_context_payload(
    database_path: Path,
    *,
    groups: list[GroupConfig],
    econ_service: EconCalendarService | None,
    fringe_service: FringeService | None,
    days: int,
) -> dict[str, object]:
    days = clamp_days(days)
    snapshots = await _guarded(
        asyncio.to_thread(db.load_board_snapshots, database_path, days), [], "snapshots"
    )
    movers = await _guarded(_movers(database_path, groups), {}, "movers")
    start = (datetime.now(UTC).date() - timedelta(days=days)).isoformat()
    etf_flows = await _guarded(
        asyncio.to_thread(db.load_etf_flow_history, database_path, start=start),
        {},
        "etf flows",
    )
    key_dates = await _guarded(
        _upcoming_key_dates(database_path, econ_service), [], "key dates"
    )
    fringe_book = await _guarded(_fringe_book(fringe_service), _EMPTY_BOOK.copy(), "fringe book")
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "days": days,
        "snapshots": snapshots,
        "movers": movers,
        "etf_flows": etf_flows,
        "key_dates": key_dates,
        "fringe_book": fringe_book,
    }


_EMPTY_BOOK: dict[str, object] = {"open": [], "recently_closed": []}


async def _guarded(awaitable: Awaitable[T], default: T, label: str) -> T:
    try:
        return await awaitable
    except Exception:
        logger.exception("market-context %s failed", label)
        return default


async def _movers(database_path: Path, groups: list[GroupConfig]) -> dict[str, object]:
    """5d/20d percent moves for watchlist symbols from cached daily bars.

    One windowed SQL loads every series (load_bars_by_symbol); looping
    load_bars per symbol would issue ~100 queries per digest request.
    """
    window = max(MOVER_WINDOWS)
    grouped = await asyncio.to_thread(
        db.load_bars_by_symbol, database_path, "1d", limit_per_series=window + 1
    )
    changes: dict[int, list[dict[str, object]]] = {n: [] for n in MOVER_WINDOWS}
    seen: set[str] = set()
    for group in groups:
        for asset in group.assets:
            if asset.symbol in seen:
                continue
            seen.add(asset.symbol)
            series = _series_for(grouped, asset.symbol, asset.source)
            if series is None:
                continue
            for n in MOVER_WINDOWS:
                pct = _pct_change(series, n)
                if pct is not None:
                    changes[n].append({"symbol": asset.symbol, "pct": pct})
    movers: dict[str, object] = {}
    for n in MOVER_WINDOWS:
        ranked = sorted(changes[n], key=lambda item: float(str(item["pct"])), reverse=True)
        movers[f"{n}d"] = {
            "leaders": ranked[:MOVER_LIMIT],
            "laggards": list(reversed(ranked[-MOVER_LIMIT:])),
        }
    return movers


def _series_for(
    grouped: dict[tuple[str, ProviderName], list[Bar]],
    symbol: str,
    source: ProviderName,
) -> list[Bar] | None:
    """The configured provider's series, else any provider that has one."""
    series = grouped.get((symbol, source))
    if series:
        return series
    for (bar_symbol, _), bars in grouped.items():
        if bar_symbol == symbol and bars:
            return bars
    return None


def _pct_change(series: list[Bar], bars_back: int) -> float | None:
    if len(series) <= bars_back:
        return None
    base = series[-1 - bars_back].close
    if base == 0:
        return None
    return round((series[-1].close - base) / base * 100.0, 2)


async def _upcoming_key_dates(
    database_path: Path, econ_service: EconCalendarService | None
) -> list[dict[str, object]]:
    payload = await key_dates_payload(
        database_path, econ_service, days=KEY_DATES_AHEAD_DAYS, limit=KEY_DATES_LIMIT
    )
    items = payload.get("key_dates")
    return items if isinstance(items, list) else []


async def _fringe_book(fringe_service: FringeService | None) -> dict[str, object]:
    if fringe_service is None:
        return _EMPTY_BOOK.copy()
    payload = await fringe_service.payload()
    # The agent digest keeps a recent tail; the full history lives on the
    # dashboard's Fringe tab (/api/fringe `closed`).
    closed = payload["closed"]
    recent = closed[:10] if isinstance(closed, list) else closed
    return {"open": payload["open"], "recently_closed": recent}
