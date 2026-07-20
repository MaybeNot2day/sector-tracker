"""Enrich Key Dates events with TradingView economic-calendar data.

The key_dates table stores whatever an agent report wrote — a title, a
date, maybe a "14:30 CET" time. This module attaches the numbers the rail
actually wants at print time: consensus, previous, actual, importance and
an indicator description, all fetched from TradingView's keyless calendar
endpoint and matched to the stored rows by fuzzy title + date proximity.

Enrichment is runtime-only: nothing here is ever persisted. A calendar
outage degrades to the plain un-enriched payload, never an error.

Matching is deliberately conservative: candidate calendar rows must fall
within one day of the stored event date (an agent listing an event on the
wrong week must stay un-enriched rather than borrow a far row's numbers),
and low title-overlap scores are rejected outright.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app import db

logger = logging.getLogger(__name__)

CALENDAR_URL = "https://economic-calendar.tradingview.com/events"
FETCH_TIMEOUT = 15.0
# The endpoint is keyless but origin-gated: without a browser-looking
# Origin/User-Agent pair TradingView answers 403.
_HEADERS = {
    "Origin": "https://www.tradingview.com",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}
# A couple of days back keeps freshly printed numbers visible across the
# UTC rollover; ten days forward covers the near edge of the rail.
FETCH_PAST_DAYS = 2
FETCH_AHEAD_DAYS = 10

# Hot-window bounds shared with the frontend: a matched release is HOT
# from 2 minutes before its scheduled UTC time until 45 minutes after,
# while the actual value has not yet printed.
HOT_BEFORE = timedelta(minutes=2)
HOT_AFTER = timedelta(minutes=45)
# While any cached row is hot the cache TTL collapses to this, so actuals
# land on the board within roughly a minute of the release.
HOT_TTL_SECONDS = 20.0

# The rail's "today" is the US Eastern trading date (see /api/key-dates).
_EASTERN = ZoneInfo("America/New_York")

# --- title normalization -------------------------------------------------

# Rewrites applied to BOTH sides before tokenizing, so agent shorthand and
# TradingView row titles collapse onto the same vocabulary. Order matters
# only within the CPI pair below.
_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bm/m\b"), " mom "),
    (re.compile(r"\by/y\b"), " yoy "),
    (re.compile(r"\bq/q\b"), " qoq "),
    (re.compile(r"\bex[- ]autos?\b"), " ex autos "),
    (re.compile(r"\bphilly\b"), " philadelphia "),
    (re.compile(r"\bconference board\b"), " cb "),
    (re.compile(r"\b(?:nfp|non[- ]?farm payrolls)\b"), " non farm payrolls "),
    (re.compile(r"\b(?:fomc|fed)(?: rate)? decision\b"), " fed interest rate decision "),
    (re.compile(r"\b(?:umich|u\.? ?of ?michigan|university of michigan)\b"), " michigan "),
    (re.compile(r"\bspeaks\b"), " speech "),
)

# Agent-side only: "CPI" on the board means the inflation *rate* print,
# which TradingView titles "Inflation Rate"; its literal "CPI" rows are
# index levels the reports never mean. Core first so it wins the rewrite.
_EVENT_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bcore cpi\b"), " core inflation rate "),
    (re.compile(r"\bcpi\b"), " inflation rate "),
    # Bundle names: the headline series of the UK/CA "labour market report"
    # is the unemployment rate row on TradingView.
    (re.compile(r"\blabou?r market(?: report| data)?\b"), " unemployment rate "),
)

_TOKEN = re.compile(r"[a-z0-9]+")
_YEAR_TOKEN = re.compile(r"20\d\d")
# Period qualifiers: month and quarter names position a print in time but
# never appear in TradingView row titles, so they only dilute the overlap.
_PERIOD_TOKENS = frozenset(
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sept sep oct nov dec q1 q2 q3 q4 h1 h2".split()
)
# Grammar glue plus release-cycle qualifiers ("Prel"/"Final" revisions of
# the same print differ only by these; scoring should see them as equal).
_STOPWORDS = frozenset("of and the for an sa nsa final prel preliminary flash".split())
# Geography words are dropped from scoring (rows never carry them) but
# institution cues (fed, ecb, boe...) stay: they appear in row titles too.
_GEO_TOKENS = frozenset(
    "us usa eurozone euro area germany german uk britain british japan japanese "
    "china chinese united states america american canada canadian australia "
    "australian zealand".split()
)

_COUNTRY_CUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("US", re.compile(r"\bu\.?s\.?a?\b|\bfed\b|\bfomc\b|\bunited states\b|\bamerican?\b")),
    ("EU", re.compile(r"\beurozone\b|\beuro ?area\b|\becb\b")),
    ("DE", re.compile(r"\bgerman(?:y)?\b|\bifo\b|\bzew\b|\bbundesbank\b")),
    ("GB", re.compile(r"\buk\b|\bbritain\b|\bbritish\b|\bboe\b")),
    ("JP", re.compile(r"\bjapan(?:ese)?\b|\bboj\b")),
    ("CN", re.compile(r"\bchina\b|\bchinese\b|\bpboc\b")),
    ("CA", re.compile(r"\bcanad(?:a|ian)\b|\bbank of canada\b|\bboc\b")),
    ("AU", re.compile(r"\baustralia(?:n)?\b|\brba\b")),
    ("NZ", re.compile(r"\bnew zealand\b|\brbnz\b")),
)

# A title-only match with no country cue must clear a higher bar: with six
# countries in play, weak token overlap grabs foreign lookalike rows.
_SCORE_THRESHOLD_WITH_COUNTRY = 0.6
_SCORE_THRESHOLD_ANY_COUNTRY = 0.72
_CONTAINMENT_BONUS = 0.1
# An indicator-only overlap ("Interest Rate" under a speech row) must not
# clear the with-country threshold by itself: 0.767 * 0.75 < 0.6.
_INDICATOR_DISCOUNT = 0.75

# Hermes table titles append source attribution after a spaced em/en dash
# ("Industrial Production MoM, Jun — Federal Reserve"); those tokens only
# dilute the overlap score, so scoring strips the suffix.
_ATTRIBUTION_SPLIT = re.compile(r"\s+[—–]\s+")
# A trailing slash-segment is the secondary twin of a combined print; its
# exact-match score must not beat the lead series (1.1 * 0.85 < 0.957,
# the full-variant score for "Initial / Continuing Jobless Claims").
_SECONDARY_SEGMENT_DISCOUNT = 0.85
_EVENT_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*([A-Za-z]{2,4})?\b")
# Coarse zone map for the time-proximity tiebreak only; ZoneInfo handles
# DST for the aliases that have it.
_ZONES = {
    "ET": "America/New_York",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CET": "Europe/Paris",
    "CEST": "Europe/Paris",
    "BST": "Europe/London",
    "GMT": "UTC",
    "UTC": "UTC",
}


def _apply_subs(text: str, subs: tuple[tuple[re.Pattern[str], str], ...]) -> str:
    for pattern, replacement in subs:
        text = pattern.sub(replacement, text)
    return text


def _tokenize(text: str) -> frozenset[str]:
    return frozenset(
        token
        for token in _TOKEN.findall(text)
        if len(token) > 1
        and token not in _PERIOD_TOKENS
        and token not in _STOPWORDS
        and token not in _GEO_TOKENS
        and not _YEAR_TOKEN.fullmatch(token)
    )


def infer_country(title: str) -> str | None:
    lowered = title.lower()
    for country, pattern in _COUNTRY_CUES:
        if pattern.search(lowered):
            return country
    return None


def normalize_event_title(title: str) -> frozenset[str]:
    """Agent-written title -> scoring tokens (months, years, geo dropped)."""
    text = _apply_subs(title.lower(), _EVENT_SUBSTITUTIONS)
    tokens = set(_tokenize(_apply_subs(text, _SUBSTITUTIONS)))
    # Reports say "Jobless Claims" for the weekly initial print; only an
    # explicit "continuing" means the other series.
    if {"jobless", "claims"} <= tokens and "continuing" not in tokens:
        tokens.add("initial")
    return frozenset(tokens)


def event_title_variants(title: str) -> tuple[tuple[frozenset[str], float], ...]:
    """Weighted token-set variants scored against every candidate row.

    The whole (attribution-stripped) title always scores at full weight; a
    slash-combined twin print ("Building Permits / Housing Starts, Jun")
    additionally scores each segment alone, since the combined set matches
    neither row well. Segments after the first are slightly discounted:
    the agent leads with the primary print, and an exact match on the
    trailing twin ("Initial / Continuing Jobless Claims") must not outrank
    the headline series. Single-token segments are dropped: with the
    containment bonus a lone token like "initial" would clear the
    threshold against any row that contains it.
    """
    base = _ATTRIBUTION_SPLIT.split(title, 1)[0]
    variants: dict[frozenset[str], float] = {}
    full = normalize_event_title(base)
    if full:
        variants[full] = 1.0
    segments = base.split(" / ")
    if len(segments) > 1:
        for position, segment in enumerate(segments):
            tokens = normalize_event_title(segment)
            if len(tokens) > 1:
                weight = 1.0 if position == 0 else _SECONDARY_SEGMENT_DISCOUNT
                variants[tokens] = max(variants.get(tokens, 0.0), weight)
    return tuple(variants.items())


def _normalize_row_title(title: str) -> frozenset[str]:
    return _tokenize(_apply_subs(title.lower(), _SUBSTITUTIONS))


# --- calendar rows -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalendarRow:
    title: str
    country: str
    ticker: str
    source: str | None
    period: str
    date: datetime  # aware UTC release moment
    actual: float | None
    forecast: float | None
    previous: float | None
    unit: str | None
    scale: str | None
    importance: int
    comment: str | None
    title_tokens: frozenset[str]
    indicator_tokens: frozenset[str]


def normalize_calendar_rows(raw: list[dict[str, Any]]) -> list[CalendarRow]:
    """Parse raw TradingView rows once at fetch; malformed rows are dropped."""
    rows: list[CalendarRow] = []
    for entry in raw:
        title = str(entry.get("title") or "").strip()
        when = _parse_row_date(entry.get("date"))
        if not title or when is None:
            continue
        indicator = str(entry.get("indicator") or "")
        rows.append(
            CalendarRow(
                title=title,
                country=str(entry.get("country") or ""),
                ticker=str(entry.get("ticker") or ""),
                source=entry.get("source") or None,
                period=str(entry.get("period") or ""),
                date=when,
                actual=_as_number(entry.get("actual")),
                forecast=_as_number(entry.get("forecast")),
                previous=_as_number(entry.get("previous")),
                unit=entry.get("unit") or None,
                scale=entry.get("scale") or None,
                importance=int(entry.get("importance") or 0),
                comment=entry.get("comment") or None,
                title_tokens=_normalize_row_title(title),
                indicator_tokens=_normalize_row_title(indicator),
            )
        )
    return rows


def _parse_row_date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _as_number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


# --- matching ------------------------------------------------------------


def _score(event_tokens: frozenset[str], row: CalendarRow) -> float:
    """Dice overlap of normalized token sets, best of title vs indicator.

    Containment (one set inside the other) earns a small bonus so exact
    sub-phrases beat rows that merely share a few words. Indicator-only
    overlap is discounted: the indicator names the underlying series, not
    the event — speech rows carry indicator "Interest Rate", and "ECB
    Interest Rate Decision" must not enrich from "ECB Cipollone Speech".
    """
    return max(
        _token_score(event_tokens, row.title_tokens),
        _INDICATOR_DISCOUNT * _token_score(event_tokens, row.indicator_tokens),
    )


def _token_score(event_tokens: frozenset[str], tokens: frozenset[str]) -> float:
    if not tokens or not event_tokens:
        return 0.0
    overlap = len(event_tokens & tokens)
    if not overlap:
        return 0.0
    score = 2 * overlap / (len(event_tokens) + len(tokens))
    if event_tokens <= tokens or tokens <= event_tokens:
        score += _CONTAINMENT_BONUS
    return score


def _event_moment_utc(event_date: date, event_time: str | None) -> datetime | None:
    """UTC instant of the stored event when its time carries a known zone."""
    if not event_time:
        return None
    match = _EVENT_TIME.search(event_time)
    if match is None or match.group(3) is None:
        return None
    zone_name = _ZONES.get(match.group(3).upper())
    if zone_name is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    local = datetime(event_date.year, event_date.month, event_date.day, hour, minute)
    return local.replace(tzinfo=ZoneInfo(zone_name)).astimezone(UTC)


_QUARTER_HINT = re.compile(r"\bq([1-4])\b")


def match_release(event: dict[str, object], rows: list[CalendarRow]) -> dict[str, object] | None:
    """Best calendar row for one stored key-date item, as a release dict.

    Hard rules first: only rows within one day of the stored date are
    candidates, and an event naming a quarter ("CPI (YoY, Q2)") never
    pairs with a row whose period says otherwise — a quarterly print
    must not enrich from some country's monthly row that happens to
    share a title. Ties break same-day > adjacent-day, then importance,
    then time proximity when the stored event has a zoned HH:MM time.
    """
    try:
        event_date = date.fromisoformat(str(event.get("date") or ""))
    except ValueError:
        return None
    title = str(event.get("title") or "")
    if not title:
        return None
    variants = event_title_variants(title)
    if not variants:
        return None
    country = infer_country(title)
    threshold = (
        _SCORE_THRESHOLD_WITH_COUNTRY if country else _SCORE_THRESHOLD_ANY_COUNTRY
    )
    quarter_match = _QUARTER_HINT.search(title.lower())
    quarter = f"q{quarter_match.group(1)}" if quarter_match else None
    time_raw = event.get("time")
    moment = _event_moment_utc(event_date, str(time_raw) if time_raw else None)

    best: tuple[float, int, int, float] | None = None
    best_row: CalendarRow | None = None
    for row in rows:
        if country is not None and row.country != country:
            continue
        row_date = row.date.date()
        if abs((row_date - event_date).days) > 1:
            continue
        row_period = row.period.lower()
        if quarter is not None and row_period and quarter not in row_period:
            continue
        score = max(weight * _score(tokens, row) for tokens, weight in variants)
        if score < threshold:
            continue
        proximity = -abs((row.date - moment).total_seconds()) if moment else 0.0
        key = (score, 1 if row_date == event_date else 0, row.importance, proximity)
        if best is None or key > best:
            best = key
            best_row = row
    if best_row is None:
        return None
    return _release_payload(best_row)


def _series_url(ticker: str) -> str | None:
    clean = ticker.strip()
    if not clean or ":" not in clean:
        return None
    return f"https://www.tradingview.com/symbols/{clean.replace(':', '-')}/"


def _release_payload(row: CalendarRow) -> dict[str, object]:
    surprise: float | None = None
    if row.actual is not None and row.forecast is not None:
        surprise = round(row.actual - row.forecast, 4)
    return {
        "time_utc": row.date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period": row.period or None,
        "country": row.country or None,
        "actual": format_display(row.actual, unit=row.unit, scale=row.scale),
        "forecast": format_display(row.forecast, unit=row.unit, scale=row.scale),
        "previous": format_display(row.previous, unit=row.unit, scale=row.scale),
        "surprise": surprise,
        "importance": row.importance,
        "comment": row.comment,
        "matched_title": row.title,
        "source": row.source,
        "series_url": _series_url(row.ticker),
    }


# --- display formatting --------------------------------------------------


def format_display(value: float | None, *, unit: str | None, scale: str | None) -> str | None:
    """Compact value + units: "208K", "-0.2%", "-7.8 €B". Null-safe."""
    if value is None:
        return None
    number = _format_number(value)
    if unit == "%":
        return f"{number}%"
    if unit:  # currency symbol: € + B -> "-7.8 €B"
        return f"{number} {unit}{scale or ''}"
    if scale:
        return f"{number}{scale}"
    return number


def _format_number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return str(value)


# --- hot window ----------------------------------------------------------


def _is_hot(release_time: datetime, actual: object, now: datetime) -> bool:
    return actual is None and release_time - HOT_BEFORE <= now <= release_time + HOT_AFTER


def any_hot_release(items: list[dict[str, object]], now: datetime | None = None) -> bool:
    """True when any *matched* item is inside its hot window awaiting a print."""
    now = now or datetime.now(UTC)
    for item in items:
        release = item.get("release")
        if not isinstance(release, dict):
            continue
        release_time = _parse_row_date(release.get("time_utc"))
        if release_time is not None and _is_hot(release_time, release.get("actual"), now):
            return True
    return False


# --- service -------------------------------------------------------------


class EconCalendarService:
    """Cached TradingView calendar snapshot + key-date enrichment.

    Same shape as the other scrape-backed services: TTL cache behind an
    asyncio.Lock herd guard, and a failed fetch keeps the previous snapshot
    with a short cooldown so an outage never hammers the endpoint or blanks
    the board.
    """

    FAILURE_RETRY_SECONDS = 60.0

    def __init__(
        self, *, cache_seconds: int = 300, countries: str = "US,EU,DE,GB,JP,CN,CA,AU,NZ"
    ) -> None:
        self.cache_seconds = cache_seconds
        self.countries = countries
        self._rows: list[CalendarRow] = []
        # None (not 0.0): monotonic() is near-zero right after host boot,
        # which would otherwise read as a fresh fetch / live cooldown.
        self._fetched: float | None = None
        self._failed: float | None = None
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    def _ttl_seconds(self) -> float:
        # Adaptive TTL: any cached row inside its hot window (its own
        # actual still null) collapses the cache so the print lands fast.
        now = datetime.now(UTC)
        if any(_is_hot(row.date, row.actual, now) for row in self._rows):
            return HOT_TTL_SECONDS
        return float(self.cache_seconds)

    def _fresh(self) -> bool:
        return self._fetched is not None and time.monotonic() - self._fetched < self._ttl_seconds()

    def _cooling_down(self) -> bool:
        if self._failed is None:
            return False
        return time.monotonic() - self._failed < self.FAILURE_RETRY_SECONDS

    async def refresh(self) -> None:
        if self._fresh() or self._cooling_down():
            return
        async with self._lock:
            # Herd guard: whoever lost the race sees the winner's snapshot.
            if self._fresh() or self._cooling_down():
                return
            try:
                raw = await self._fetch()
            except Exception:
                # Keep the previous snapshot: stale enrichment beats none,
                # and the cooldown stops per-request refetch storms.
                self._failed = time.monotonic()
                logger.warning("economic calendar fetch failed", exc_info=True)
                return
            self._rows = normalize_calendar_rows(raw)
            self._fetched = time.monotonic()
            self._failed = None

    async def _fetch(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        response = await self._http_client().get(
            CALENDAR_URL,
            params={
                "from": _iso_z(now - timedelta(days=FETCH_PAST_DAYS)),
                "to": _iso_z(now + timedelta(days=FETCH_AHEAD_DAYS)),
                "countries": self.countries,
            },
            headers=_HEADERS,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result")
        if payload.get("status") != "ok" or not isinstance(result, list):
            raise ValueError(f"unexpected calendar payload: status={payload.get('status')!r}")
        return result

    async def enrich(self, items: list[dict[str, object]]) -> None:
        """Attach a `release` dict (or None) to every key-date item in place."""
        await self.refresh()
        rows = self._rows
        for item in items:
            item["release"] = match_release(item, rows)

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=FETCH_TIMEOUT)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _iso_z(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# --- payload -------------------------------------------------------------


async def key_dates_payload(
    database_path: Path,
    service: EconCalendarService | None,
    *,
    days: int = 90,
    limit: int = 200,
) -> dict[str, object]:
    """The /api/key-dates payload; also rebuilt by the scheduler loop.

    "Today" is the US Eastern trading date — the panel renders an ET clock,
    and an evening UTC rollover must not drop the current session's events.
    Enrichment failure of any kind serves the plain payload, never a 500.
    """
    today = datetime.now(_EASTERN).date()
    items = await asyncio.to_thread(
        db.load_key_dates,
        database_path,
        start=today.isoformat(),
        end=(today + timedelta(days=days)).isoformat(),
        limit=limit,
    )
    if service is not None:
        try:
            await service.enrich(items)
        except Exception:
            logger.exception("key-dates enrichment failed")
    # Contract: every item carries the key, null when unmatched/unavailable.
    for item in items:
        item.setdefault("release", None)
    return {"key_dates": items, "as_of": today.isoformat()}
