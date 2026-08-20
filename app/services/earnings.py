"""Weekly earnings calendar from Nasdaq's public API.

Nasdaq serves a keyless JSON calendar (the data behind nasdaq.com/market-
activity/earnings): every reporting company for a date with EPS consensus,
report session (pre/after market), market cap, and analyst coverage. A
batched TradingView Scanner request supplies exact scheduled/estimated UTC
release timestamps; a second Nasdaq endpoint returns the last quarters'
EPS surprises for the beat/miss history chips.

The service composes a Monday-Friday week view: per day the top rows by
rank (board-held symbols first, then market cap, then analyst coverage)
are enriched with exact times and surprise history; the rest are counted.
The caches are independent, so watchlist edits re-rank instantly without
refetching.

Implied move (ATM IV scaled to the first expiration after the report) is
computed only for held symbols through the existing MarketData.app options
service — the free tier's request budget cannot cover a whole week of
tickers.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

CALENDAR_URL = "https://api.nasdaq.com/api/calendar/earnings"
SURPRISE_URL = "https://api.nasdaq.com/api/company/{symbol}/earnings-surprise"
RELEASE_TIME_URL = "https://scanner.tradingview.com/america/scan"
REQUEST_TIMEOUT_SECONDS = 15.0
# api.nasdaq.com rejects requests without a browser-looking UA outright.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# The calendar shifts slowly (companies confirm dates days ahead); surprise
# history only changes when a company reports.
DAY_CACHE_SECONDS = 6 * 3600.0
SURPRISE_CACHE_SECONDS = 24 * 3600.0
RELEASE_TIME_CACHE_SECONDS = 6 * 3600.0
# The composed week payload: a warm copy must answer every read instantly;
# a cold compose costs ~35 surprise fetches (~5s).
PAYLOAD_CACHE_SECONDS = 30 * 60.0
PAYLOAD_CACHE_MAX = 8
FAILURE_RETRY_SECONDS = 300.0
SURPRISE_CACHE_MAX = 512
RELEASE_TIME_CACHE_MAX = 512
DETAILED_PER_DAY = 7
LAST_QUARTERS = 4
# Bounded fan-out for surprise fetches: a week enriches at most ~35 symbols.
_SURPRISE_CONCURRENCY = 8
_DISPLAY_TZ = ZoneInfo("Europe/Berlin")
_TRADINGVIEW_EXCHANGES = ("NASDAQ", "NYSE", "AMEX", "OTC")
_TRADINGVIEW_EXCHANGE_PRIORITY = {
    exchange: priority for priority, exchange in enumerate(_TRADINGVIEW_EXCHANGES)
}

_MONEY = re.compile(r"[^0-9.\-]")


class OptionsSnapshotService(Protocol):
    async def get_snapshot(self, symbol: str, expiration: str | None = None) -> dict[str, object]:
        """MarketDataOptionsService.get_snapshot contract."""
        ...


def week_start(today: date) -> date:
    """Monday of the week to show: current week, or next from the weekend."""
    monday = today - timedelta(days=today.weekday())
    if today.weekday() >= 5:
        monday += timedelta(days=7)
    return monday


def _parse_money(value: Any) -> float | None:
    """Nasdaq money strings: "$3.69", "($0.24)" (negative), "" (missing)."""
    if not isinstance(value, str) or not value.strip():
        return None
    negative = "(" in value
    cleaned = _MONEY.sub("", value)
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative and number > 0 else number


def _parse_int(value: Any) -> int | None:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


_SESSIONS = {"time-pre-market": "bmo", "time-after-hours": "amc"}


def _parse_calendar_rows(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    raw_rows = data.get("rows") if isinstance(data, dict) else None
    rows: list[dict[str, Any]] = []
    for raw in raw_rows or []:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": str(raw.get("name") or symbol).strip(),
                "session": _SESSIONS.get(str(raw.get("time") or ""), "tns"),
                "eps_estimate": _parse_money(raw.get("epsForecast")),
                "market_cap": _parse_money(raw.get("marketCap")),
                "estimates": _parse_int(raw.get("noOfEsts")),
                "fiscal_quarter": str(raw.get("fiscalQuarterEnding") or "") or None,
            }
        )
    return rows


def _parse_surprises(payload: Any) -> list[bool | None]:
    """Beat/miss for the last quarters, oldest first; unknown rows are None."""
    data = payload.get("data") if isinstance(payload, dict) else None
    table = data.get("earningsSurpriseTable") if isinstance(data, dict) else None
    raw_rows = table.get("rows") if isinstance(table, dict) else None
    marks: list[bool | None] = []
    for raw in (raw_rows or [])[:LAST_QUARTERS]:
        if not isinstance(raw, dict):
            marks.append(None)
            continue
        try:
            surprise = float(str(raw.get("percentageSurprise")))
        except (TypeError, ValueError):
            marks.append(None)
            continue
        marks.append(surprise >= 0)
    marks.reverse()  # Nasdaq returns newest first; chips read oldest -> newest.
    return marks


def _parse_release_candidates(payload: Any) -> dict[str, list[tuple[int, int, int]]]:
    """TradingView symbol -> (UTC epoch, session code, exchange priority)."""
    raw_rows = payload.get("data") if isinstance(payload, dict) else None
    candidates: dict[str, list[tuple[int, int, int]]] = {}
    for raw in raw_rows or []:
        if not isinstance(raw, dict):
            continue
        listing = str(raw.get("s") or "")
        values = raw.get("d")
        if not isinstance(values, list) or len(values) < 3:
            continue
        symbol = str(values[0] or "").strip().upper()
        timestamp, timing = values[1:3]
        if (
            not symbol
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, int | float)
            or isinstance(timing, bool)
            or not isinstance(timing, int | float)
        ):
            continue
        exchange = listing.partition(":")[0]
        priority = _TRADINGVIEW_EXCHANGE_PRIORITY.get(exchange, len(_TRADINGVIEW_EXCHANGES))
        candidates.setdefault(symbol, []).append((int(timestamp), int(timing), priority))
    return candidates


def _select_release_at(
    candidates: list[tuple[int, int, int]],
    report_date: date,
    session: str,
) -> str | None:
    """Choose an exact scheduled timestamp consistent with the Nasdaq row."""
    expected_timing = {"bmo": -1, "amc": 1}.get(session)
    valid: list[tuple[int, int, datetime]] = []
    for timestamp, timing, priority in candidates:
        # TradingView's zero code means no exact pre/post-market timing.
        if timing not in {-1, 1} or (expected_timing is not None and timing != expected_timing):
            continue
        try:
            moment = datetime.fromtimestamp(timestamp, UTC)
        except (OSError, OverflowError, ValueError):
            continue
        day_distance = abs((moment.astimezone(_DISPLAY_TZ).date() - report_date).days)
        if day_distance <= 1:
            valid.append((day_distance, priority, moment))
    if not valid:
        return None
    moment = min(valid, key=lambda item: (item[0], item[1]))[2]
    return moment.isoformat().replace("+00:00", "Z")


def _rank_key(row: dict[str, Any], held: set[str]) -> tuple[int, float, int]:
    return (
        0 if row["symbol"] in held else 1,
        -(row["market_cap"] or 0.0),
        -(row["estimates"] or 0),
    )


class EarningsCalendarService:
    """Cached weekly earnings calendar with beat/miss and implied-move data."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._day_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._day_failed_at: dict[str, float] = {}
        self._surprise_cache: dict[str, tuple[float, list[bool | None]]] = {}
        self._release_time_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
        self._release_time_failed_at: float | None = None
        self._payload_cache: dict[str, tuple[float, dict[str, object]]] = {}
        self._lock = asyncio.Lock()

    async def get_week_cached(
        self,
        start: date,
        held: set[str],
        options_service: OptionsSnapshotService | None = None,
    ) -> dict[str, object]:
        """Composed week from the payload cache; recomputes only past the TTL.

        The scheduler's warm loop refreshes ahead of expiry, so user requests
        never pay the cold-compose cost of a fresh week.
        """
        key = start.isoformat()
        cached = self._payload_cache.get(key)
        if cached is not None and monotonic() - cached[0] < PAYLOAD_CACHE_SECONDS:
            return cached[1]
        payload = await self.get_week(start, held, options_service)
        self._payload_cache[key] = (monotonic(), payload)
        while len(self._payload_cache) > PAYLOAD_CACHE_MAX:
            del self._payload_cache[min(self._payload_cache)]
        return payload

    async def get_week(
        self,
        start: date,
        held: set[str],
        options_service: OptionsSnapshotService | None = None,
    ) -> dict[str, object]:
        days = [start + timedelta(days=offset) for offset in range(5)]
        async with self._lock:
            day_rows = await asyncio.gather(*(self._day_rows(day) for day in days))

        detailed: list[tuple[date, dict[str, Any]]] = []
        payload_days: list[dict[str, object]] = []
        ranking_fallback = False
        for day, rows in zip(days, day_rows, strict=True):
            ordered = sorted(rows, key=lambda row: _rank_key(row, held))
            top = ordered[:DETAILED_PER_DAY]
            ranking_fallback = ranking_fallback or any(row["market_cap"] is None for row in top)
            detailed.extend((day, row) for row in top)
            payload_days.append(
                {
                    "date": day.isoformat(),
                    "weekday": day.strftime("%a").upper(),
                    "reports": top,  # enriched in place below
                    "more": max(len(ordered) - len(top), 0),
                    "total": len(ordered),
                }
            )

        async with self._lock:
            release_times = await self._release_times(
                [(day, row["symbol"], row["session"]) for day, row in detailed]
            )

        surprises = await self._surprise_histories([row["symbol"] for _, row in detailed])
        implied = await self._implied_moves(
            [(day, row["symbol"]) for day, row in detailed if row["symbol"] in held],
            options_service,
        )
        for report_day, row in detailed:
            row["held"] = row["symbol"] in held
            row["last4q"] = surprises.get(row["symbol"], [])
            row["implied_move_pct"] = implied.get(row["symbol"])
            row["release_at"] = release_times.get((report_day.isoformat(), row["symbol"]))
        return {
            "as_of": datetime.now(UTC).isoformat(),
            "week_start": start.isoformat(),
            "week_end": days[-1].isoformat(),
            "days": payload_days,
            "ranking_fallback": ranking_fallback,
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _day_rows(self, day: date) -> list[dict[str, Any]]:
        key = day.isoformat()
        now = monotonic()
        cached = self._day_cache.get(key)
        if cached is not None and now - cached[0] < DAY_CACHE_SECONDS:
            return cached[1]
        failed_at = self._day_failed_at.get(key)
        if failed_at is not None and now - failed_at < FAILURE_RETRY_SECONDS:
            return cached[1] if cached is not None else []
        try:
            response = await self._http().get(CALENDAR_URL, params={"date": key})
            response.raise_for_status()
            rows = _parse_calendar_rows(response.json())
        except Exception:
            # Stale-on-error: a Nasdaq hiccup must not blank the calendar.
            logger.warning("earnings calendar fetch failed for %s", key, exc_info=True)
            self._day_failed_at[key] = now
            return cached[1] if cached is not None else []
        self._day_cache[key] = (now, rows)
        self._day_failed_at.pop(key, None)
        # The board only ever shows a handful of weeks; drop the oldest keys.
        while len(self._day_cache) > 30:
            del self._day_cache[min(self._day_cache)]
        return rows

    async def _surprise_histories(self, symbols: list[str]) -> dict[str, list[bool | None]]:
        now = monotonic()
        wanted = {symbol for symbol in symbols}
        results: dict[str, list[bool | None]] = {}
        missing: list[str] = []
        for symbol in sorted(wanted):
            cached = self._surprise_cache.get(symbol)
            if cached is not None and now - cached[0] < SURPRISE_CACHE_SECONDS:
                results[symbol] = cached[1]
            else:
                missing.append(symbol)

        semaphore = asyncio.Semaphore(_SURPRISE_CONCURRENCY)

        async def fetch(symbol: str) -> tuple[str, list[bool | None] | None]:
            async with semaphore:
                try:
                    response = await self._http().get(SURPRISE_URL.format(symbol=symbol))
                    response.raise_for_status()
                    return symbol, _parse_surprises(response.json())
                except Exception:
                    return symbol, None

        for symbol, marks in await asyncio.gather(*(fetch(symbol) for symbol in missing)):
            if marks is None:
                stale = self._surprise_cache.get(symbol)
                results[symbol] = stale[1] if stale is not None else []
                continue
            results[symbol] = marks
            self._surprise_cache[symbol] = (now, marks)
        while len(self._surprise_cache) > SURPRISE_CACHE_MAX:
            del self._surprise_cache[
                min(self._surprise_cache, key=lambda key: self._surprise_cache[key][0])
            ]
        return results

    async def _release_times(
        self,
        reports: list[tuple[date, str, str]],
    ) -> dict[tuple[str, str], str | None]:
        now = monotonic()
        wanted = {
            (report_date.isoformat(), symbol): (report_date, session)
            for report_date, symbol, session in reports
        }
        results: dict[tuple[str, str], str | None] = {}
        stale: dict[tuple[str, str], str | None] = {}
        missing: list[tuple[str, str]] = []
        for key in wanted:
            cached = self._release_time_cache.get(key)
            if cached is not None:
                stale[key] = cached[1]
            if cached is not None and now - cached[0] < RELEASE_TIME_CACHE_SECONDS:
                results[key] = cached[1]
            else:
                missing.append(key)
        if not missing:
            return results
        if (
            self._release_time_failed_at is not None
            and now - self._release_time_failed_at < FAILURE_RETRY_SECONDS
        ):
            return stale | results

        symbols = sorted({symbol for _, symbol in missing})
        tickers = [
            f"{exchange}:{symbol}" for symbol in symbols for exchange in _TRADINGVIEW_EXCHANGES
        ]
        try:
            response = await self._http().post(
                RELEASE_TIME_URL,
                json={
                    "symbols": {"tickers": tickers, "query": {"types": []}},
                    "columns": [
                        "name",
                        "earnings_release_next_date",
                        "earnings_release_next_time",
                    ],
                },
            )
            response.raise_for_status()
            candidates = _parse_release_candidates(response.json())
        except Exception:
            logger.warning("earnings release-time fetch failed", exc_info=True)
            self._release_time_failed_at = now
            return stale | results

        for key in missing:
            report_date, session = wanted[key]
            release_at = _select_release_at(candidates.get(key[1], []), report_date, session)
            results[key] = release_at
            self._release_time_cache[key] = (now, release_at)
        self._release_time_failed_at = None
        while len(self._release_time_cache) > RELEASE_TIME_CACHE_MAX:
            del self._release_time_cache[
                min(self._release_time_cache, key=lambda key: self._release_time_cache[key][0])
            ]
        return results

    async def _implied_moves(
        self,
        reports: list[tuple[date, str]],
        options_service: OptionsSnapshotService | None,
    ) -> dict[str, float]:
        if options_service is None or not reports:
            return {}
        moves: dict[str, float] = {}
        for report_date, symbol in reports:
            try:
                move = await self._implied_move(options_service, symbol, report_date)
            except Exception:
                move = None  # unsupported venue / options outage: show a dash
            if move is not None:
                moves[symbol] = move
        return moves

    async def _implied_move(
        self,
        options_service: OptionsSnapshotService,
        symbol: str,
        report_date: date,
    ) -> float | None:
        snapshot = await options_service.get_snapshot(symbol)
        raw_expirations = snapshot.get("expirations")
        expirations = (
            [value for value in raw_expirations if isinstance(value, str)]
            if isinstance(raw_expirations, list)
            else []
        )
        target = next((exp for exp in sorted(expirations) if exp >= report_date.isoformat()), None)
        if target is not None and target != snapshot.get("expiration"):
            snapshot = await options_service.get_snapshot(symbol, target)
        metrics = snapshot.get("metrics")
        atm_iv = metrics.get("atm_iv") if isinstance(metrics, dict) else None
        expiration = str(snapshot.get("expiration") or "")
        if not isinstance(atm_iv, int | float) or atm_iv <= 0 or not expiration:
            return None
        try:
            days_to_exp = (date.fromisoformat(expiration) - report_date).days
        except ValueError:
            return None
        # ATM IV annualizes the distribution; scale it down to the horizon
        # between the report and the expiration that prices it.
        return round(float(atm_iv) * math.sqrt(max(days_to_exp, 1) / 365.0) * 100, 1)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, headers=_HEADERS)
        return self._client
