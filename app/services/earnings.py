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
refetching; the composed payload is cached per held set.

Implied move (ATM IV scaled to the first expiration after the report's
reaction session) is computed only for held symbols reporting today or
later, through the existing MarketData.app options service. Each report's
move is cached for hours and all lookups share a small daily request budget:
the free tier's 100 credits/day must stay available for the chart modal.
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

from app.services.options import OptionsDataError

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
# Implied moves: a report's IV shifts slowly, so one lookup per report every
# few hours is plenty. MarketData's free tier allows 100 credits per day
# (resetting 09:30 ET); one lookup costs at most an expirations request plus
# one chain request, and the calendar may spend at most this many per day.
IMPLIED_MOVE_CACHE_SECONDS = 12 * 3600.0
IMPLIED_MOVE_RETRY_SECONDS = 3600.0
IMPLIED_MOVE_CACHE_MAX = 256
IMPLIED_MOVE_DAILY_REQUESTS = 20
IMPLIED_MOVE_REQUESTS_PER_LOOKUP = 2
_MARKETDATA_QUOTA_RESET = timedelta(hours=9, minutes=30)
# Failures that hit every symbol alike: stop the pass instead of burning
# the budget on the remaining reports.
_TOKEN_WIDE_OPTIONS_ERRORS = frozenset(
    {
        "options_not_configured",
        "marketdata_auth_failed",
        "marketdata_device_locked",
        "marketdata_entitlement_required",
        "marketdata_rate_limited",
    }
)
# Bounded fan-out for surprise fetches: a week enriches at most ~35 symbols.
_SURPRISE_CONCURRENCY = 8
_DISPLAY_TZ = ZoneInfo("Europe/Berlin")
# "Today" for the calendar is the US Eastern trading date.
_EASTERN = ZoneInfo("America/New_York")
_MARKET_CLOSE_HOUR = 16
_TRADINGVIEW_EXCHANGES = ("NASDAQ", "NYSE", "AMEX", "OTC")
_TRADINGVIEW_EXCHANGE_PRIORITY = {
    exchange: priority for priority, exchange in enumerate(_TRADINGVIEW_EXCHANGES)
}

_MONEY = re.compile(r"[^0-9.\-]")


class OptionsSnapshotService(Protocol):
    async def get_expirations(self, symbol: str) -> list[str]:
        """MarketDataOptionsService.get_expirations contract."""
        ...

    async def get_snapshot(self, symbol: str, expiration: str | None = None) -> dict[str, object]:
        """MarketDataOptionsService.get_snapshot contract."""
        ...


def eastern_today() -> date:
    """The US Eastern trading date; a UTC date skips ahead every evening."""
    return datetime.now(_EASTERN).date()


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
        self._payload_cache: dict[tuple[str, frozenset[str]], tuple[float, dict[str, object]]] = {}
        # (report date, symbol) -> (fresh until, implied move or None).
        self._implied_cache: dict[tuple[str, str], tuple[float, float | None]] = {}
        self._implied_paused_until = 0.0
        self._quota_day: date | None = None
        self._quota_used = 0
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
        # The held set ranks rows and picks implied-move symbols: a watchlist
        # edit must recompose instead of serving the old ranking for 30 min.
        key = (start.isoformat(), frozenset(held))
        cached = self._payload_cache.get(key)
        if cached is not None and monotonic() - cached[0] < PAYLOAD_CACHE_SECONDS:
            return cached[1]
        payload = await self.get_week(start, held, options_service)
        self._payload_cache[key] = (monotonic(), payload)
        while len(self._payload_cache) > PAYLOAD_CACHE_MAX:
            del self._payload_cache[
                min(self._payload_cache, key=lambda item: self._payload_cache[item][0])
            ]
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
            [
                (day, row["symbol"], row["session"])
                for day, row in detailed
                if row["symbol"] in held
            ],
            options_service,
        )
        for report_day, row in detailed:
            row["held"] = row["symbol"] in held
            row["last4q"] = surprises.get(row["symbol"], [])
            row["implied_move_pct"] = implied.get((report_day.isoformat(), row["symbol"]))
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
        if cached is not None:
            # Nasdaq reports "time not supplied" for dates already past; keep
            # the pre/after-market session an earlier fetch of this day knew.
            known = {row["symbol"]: row["session"] for row in cached[1]}
            for row in rows:
                if row["session"] == "tns" and known.get(row["symbol"], "tns") != "tns":
                    row["session"] = known[row["symbol"]]
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
            if release_at is None:
                # TradingView only knows the *next* release: once a report is
                # out its timestamp moves on, so keep the one learned earlier.
                release_at = stale.get(key)
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
        reports: list[tuple[date, str, str]],
        options_service: OptionsSnapshotService | None,
    ) -> dict[tuple[str, str], float]:
        """(report date, symbol) -> implied move % for reports still ahead.

        Lookups run soonest report first, each cached for hours (absence
        included), and stop once the daily MarketData budget is spent.
        """
        if options_service is None or not reports:
            return {}
        now_et = datetime.now(_EASTERN)
        today = now_et.date()
        # After the close today's expiration no longer trades.
        first_open = today + timedelta(days=1 if now_et.hour >= _MARKET_CLOSE_HOUR else 0)
        clock = monotonic()
        moves: dict[tuple[str, str], float] = {}
        due: list[tuple[tuple[str, str], str, date]] = []
        for report_date, symbol, session in sorted(reports):
            # Before-open reports move today's session; after-close (and
            # unknown-timing) reports move the next one.
            reaction_day = report_date if session == "bmo" else report_date + timedelta(days=1)
            if report_date < today or reaction_day < first_open:
                continue  # already reported: nothing left for options to price
            key = (report_date.isoformat(), symbol)
            cached = self._implied_cache.get(key)
            if cached is not None:
                if cached[1] is not None:
                    moves[key] = cached[1]
                if clock < cached[0]:
                    continue
            due.append((key, symbol, reaction_day))

        if clock < self._implied_paused_until:
            return moves
        for index, (key, symbol, reaction_day) in enumerate(due):
            if not self._reserve_quota(now_et):
                logger.info(
                    "implied-move budget of %d MarketData requests spent for today; "
                    "%d report(s) wait",
                    IMPLIED_MOVE_DAILY_REQUESTS,
                    len(due) - index,
                )
                break
            try:
                move = await self._implied_move(options_service, symbol, reaction_day, today)
            except OptionsDataError as exc:
                if exc.code in _TOKEN_WIDE_OPTIONS_ERRORS:
                    logger.warning(
                        "implied moves paused for %.0f min: MarketData answered %s",
                        IMPLIED_MOVE_RETRY_SECONDS / 60,
                        exc.code,
                    )
                    self._remember_implied(key, clock + IMPLIED_MOVE_RETRY_SECONDS, moves)
                    self._implied_paused_until = clock + IMPLIED_MOVE_RETRY_SECONDS
                    break
                if exc.status_code < 500:
                    # Definitive absence (no listed options, empty chain).
                    logger.info("no implied move for %s: %s", symbol, exc.code)
                    moves.pop(key, None)
                    self._store_implied(key, clock + IMPLIED_MOVE_CACHE_SECONDS, None)
                else:
                    logger.warning("implied move for %s unavailable: %s", symbol, exc.code)
                    self._remember_implied(key, clock + IMPLIED_MOVE_RETRY_SECONDS, moves)
                continue
            except Exception:
                logger.warning("implied move for %s failed", symbol, exc_info=True)
                self._remember_implied(key, clock + IMPLIED_MOVE_RETRY_SECONDS, moves)
                continue
            self._store_implied(key, clock + IMPLIED_MOVE_CACHE_SECONDS, move)
            if move is None:
                moves.pop(key, None)
            else:
                moves[key] = move
        return moves

    def _reserve_quota(self, now_et: datetime) -> bool:
        """Charge one lookup against the budget of the current MarketData day."""
        quota_day = (now_et - _MARKETDATA_QUOTA_RESET).date()
        if quota_day != self._quota_day:
            self._quota_day = quota_day
            self._quota_used = 0
        if self._quota_used + IMPLIED_MOVE_REQUESTS_PER_LOOKUP > IMPLIED_MOVE_DAILY_REQUESTS:
            return False
        self._quota_used += IMPLIED_MOVE_REQUESTS_PER_LOOKUP
        return True

    def _remember_implied(
        self, key: tuple[str, str], fresh_until: float, moves: dict[tuple[str, str], float]
    ) -> None:
        """Transient failure: retry later, keep serving the last known move."""
        self._store_implied(key, fresh_until, moves.get(key))

    def _store_implied(self, key: tuple[str, str], fresh_until: float, move: float | None) -> None:
        self._implied_cache[key] = (fresh_until, move)
        while len(self._implied_cache) > IMPLIED_MOVE_CACHE_MAX:
            del self._implied_cache[
                min(self._implied_cache, key=lambda item: self._implied_cache[item][0])
            ]

    async def _implied_move(
        self,
        options_service: OptionsSnapshotService,
        symbol: str,
        reaction_day: date,
        today: date,
    ) -> float | None:
        # Only the expiration list (no default chain) to find the target, then
        # exactly the one chain that prices the report.
        expirations = await options_service.get_expirations(symbol)
        target = next((exp for exp in sorted(expirations) if exp >= reaction_day.isoformat()), None)
        if target is None:
            return None
        snapshot = await options_service.get_snapshot(symbol, target)
        metrics = snapshot.get("metrics")
        atm_iv = metrics.get("atm_iv") if isinstance(metrics, dict) else None
        if not isinstance(atm_iv, int | float) or atm_iv <= 0:
            return None
        days_to_exp = (date.fromisoformat(target) - today).days
        # ATM IV is annualized and prices the whole span from now until the
        # expiration; scale it to that horizon.
        return round(float(atm_iv) * math.sqrt(max(days_to_exp, 1) / 365.0) * 100, 1)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, headers=_HEADERS)
        return self._client
