"""Fringe Corner: a Hermes-managed daily trading-ideas ledger.

Hermes pushes a markdown report through the normal reports pipeline; any
section whose heading mentions "fringe" feeds the book, one action per
bullet::

    - OPEN LONG CIFR — thesis text [target: $12] [horizon: 2w]
    - HOLD SHORT XLU — updated note
    - CLOSE LONG NVDA — reason text

ACTION is OPEN/HOLD/CLOSE (case-insensitive), DIRECTION LONG/SHORT, the
ticker an uppercase [A-Z0-9.-=] token, then an em-dash/colon (or a spaced
hyphen — tickers like BRK-B own the unspaced one) before the free text,
with optional trailing ``[horizon: ...]`` / ``[target: ...]`` tags in any
order. The target is free text (usually a price); a price-looking number
inside it is parsed out for the distance-to-target read. Malformed bullets
are skipped, never fatal — same forgiveness as key_dates.

Unlike the key-dates mirror, the ledger ACCRUES: the agent manages its own
book with explicit actions, unmentioned ideas stay open (flagged stale),
and deleting a report leaves the book intact. Reconcile semantics live in
db.apply_fringe_actions.

Price stamping is best-effort: entry at ingest, exit at close, both left
null on provider failure and lazily retried on the next /api/fringe build.
Mark-to-market quotes sit behind a short TTL cache so panel refreshes and
the market-context digest never hammer the providers.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from app import db
from app.models import AssetConfig, ProviderName, Quote
from app.providers.base import QuoteProvider
from app.providers.lighter import LighterProvider

logger = logging.getLogger(__name__)

MAX_ACTIONS = 50
_MAX_TEXT = 500
_MAX_HORIZON = 40
_MAX_TARGET = 40
_MAX_STOP = 40

# --- Paper portfolio --------------------------------------------------------
# The book manages a fixed paper bankroll. Position sizes are computed HERE,
# not by the agent: the agent declares its edge ([conf: 60%], [stop: $X],
# [target: $Y]) and the ledger derives a half-Kelly fraction from it, so the
# arithmetic is deterministic and auditable. Sizes are set once, when the
# entry price is stamped, and never change on HOLD.
STARTING_CAPITAL = 10_000.0
KELLY_MULTIPLIER = 0.5  # half-Kelly: declared p and b are estimates
MAX_POSITION_FRACTION = 0.25
MIN_POSITION_FRACTION = 0.02  # floor for ideas Kelly grades at <= 0
DEFAULT_POSITION_FRACTION = 0.05  # OPEN without usable conf/stop inputs

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SECTION_TITLE = re.compile(r"fringe", re.IGNORECASE)
_BULLET = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}\.)\s+(.*\S)\s*$")
# The ticker class is deliberately case-sensitive (the verbs around it are
# not) so prose words never read as symbols; a bare `-` separates only when
# spaced, because tickers like BRK-B contain it unspaced.
_ACTION = re.compile(
    r"^(?i:(?P<action>OPEN|HOLD|CLOSE))\s+(?i:(?P<direction>LONG|SHORT))\s+"
    r"(?P<ticker>[A-Z0-9.\-=]{1,15})"
    r"(?:\s*[—–:]\s*|\s+-+\s+|\s*$)"
    r"(?P<text>.*)$"
)
# Trailing metadata tags, stripped right-to-left so both may appear in any
# order; the rightmost occurrence of a duplicated key wins.
_TRAILING_TAG = re.compile(
    r"\[\s*(?P<key>horizon|target|conf|stop)\s*:\s*(?P<value>[^\]]*?)\s*\]\s*$",
    re.IGNORECASE,
)
# First price-looking number in the target's free text ("$120", "0.55-0.60",
# "75k"); a k suffix scales by a thousand.
_TARGET_PRICE = re.compile(r"\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*([kK])?")


@dataclass(frozen=True, slots=True)
class FringeAction:
    action: str  # open | hold | close
    ticker: str
    direction: str  # long | short
    text: str  # thesis / updated note / close reason
    horizon: str | None
    target: str | None
    confidence: float | None  # declared win probability, percent (5-95)
    stop: str | None


def parse_fringe_actions(body: str) -> list[FringeAction] | None:
    """Actions under fringe headings, in report order, capped.

    Returns None when the report has NO fringe section at all — the ledger
    must not be touched (in particular the same-day mirror must not delete
    today's ideas) just because a brief skipped the section. An empty list
    means the section exists but carries no valid bullets.
    """
    actions: list[FringeAction] | None = None
    in_section = False
    section_level = 0
    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            level, text = len(heading.group(1)), heading.group(2)
            if in_section and level > section_level:
                continue  # subheading inside the section
            in_section = _SECTION_TITLE.search(text) is not None
            if in_section:
                section_level = level
                if actions is None:
                    actions = []
            continue
        if not in_section or actions is None:
            continue
        bullet = _BULLET.match(line)
        if bullet is None:
            continue
        action = _parse_bullet(bullet.group(1))
        if action is not None and len(actions) < MAX_ACTIONS:
            actions.append(action)
    return actions


def _parse_bullet(text: str) -> FringeAction | None:
    match = _ACTION.match(text)
    if match is None:
        return None
    remainder = match.group("text").strip()
    limits = {"horizon": _MAX_HORIZON, "target": _MAX_TARGET, "conf": 12, "stop": _MAX_STOP}
    tags: dict[str, str | None] = {"horizon": None, "target": None, "conf": None, "stop": None}
    while (tag := _TRAILING_TAG.search(remainder)) is not None:
        key = tag.group("key").lower()
        if tags[key] is None:
            tags[key] = tag.group("value")[: limits[key]] or None
        remainder = remainder[: tag.start()].rstrip()
    return FringeAction(
        action=match.group("action").lower(),
        ticker=match.group("ticker"),
        direction=match.group("direction").lower(),
        text=remainder[:_MAX_TEXT],
        horizon=tags["horizon"],
        target=tags["target"],
        confidence=_confidence_pct(tags["conf"]),
        stop=tags["stop"],
    )


def _confidence_pct(raw: str | None) -> float | None:
    """`60%`, `60`, or `0.6` -> percent, clamped to a sane 5-95 band."""
    if raw is None:
        return None
    match = _TARGET_PRICE.match(raw.strip())
    if match is None:
        return None
    value = float(match.group(1).replace(",", ""))
    if value <= 1.0:
        value *= 100.0
    return round(min(max(value, 5.0), 95.0), 1)


class FringeService:
    """Ledger ingest, price stamping, and the /api/fringe payload."""

    QUOTE_TTL_SECONDS = 60.0
    RECENT_CLOSED = 10

    def __init__(self, database_path: Path, providers: dict[ProviderName, QuoteProvider]) -> None:
        self.database_path = database_path
        self.providers = providers
        # symbol -> (last price, fetched monotonic); mark-to-market cache.
        self._quote_cache: dict[str, tuple[float, float]] = {}

    async def stamp_prices(self) -> None:
        """Stamp missing entry/exit prices right after ingest (freshest fill)."""
        await self._load_and_restamp()

    async def payload(self) -> dict[str, object]:
        """The /api/fringe contract: marked book, performance, recent closes."""
        open_rows, closed_rows = await self._load_and_restamp()
        latest = await asyncio.to_thread(db.latest_fringe_mention, self.database_path)
        prices = await self._prices_for({str(row["ticker"]) for row in open_rows})
        open_items = [_open_item(row, prices.get(str(row["ticker"])), latest) for row in open_rows]
        closed_items = [_closed_item(row) for row in closed_rows]
        return {
            "as_of": datetime.now(UTC).isoformat(),
            "summary": _performance_summary(open_items, closed_items),
            "open": open_items,
            "closed": closed_items[: self.RECENT_CLOSED],
        }

    async def _load_and_restamp(
        self,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Load both books, lazily re-stamping prices a past ingest missed."""
        open_rows = await asyncio.to_thread(
            db.load_fringe_ideas, self.database_path, status="open"
        )
        closed_rows = await asyncio.to_thread(
            db.load_fringe_ideas, self.database_path, status="closed"
        )
        need_entry = [row for row in open_rows if row["entry_price"] is None]
        need_exit = [row for row in closed_rows if row["exit_price"] is None]
        if need_entry or need_exit:
            await self._stamp_missing(need_entry, need_exit)
        await self._size_new_positions(open_rows, closed_rows)
        return open_rows, closed_rows

    async def _stamp_missing(
        self,
        need_entry: list[dict[str, object]],
        need_exit: list[dict[str, object]],
    ) -> None:
        prices = await self._prices_for(
            {str(row["ticker"]) for row in need_entry + need_exit}
        )
        entries = _stampable(need_entry, prices)
        exits = _stampable(need_exit, prices)
        if entries or exits:
            await asyncio.to_thread(
                db.stamp_fringe_prices, self.database_path, entries=entries, exits=exits
            )
            # Reflect the stamp locally instead of re-reading the table.
            for row, (_, price) in zip(need_entry, _paired(need_entry, prices), strict=True):
                row["entry_price"] = price
            for row, (_, price) in zip(need_exit, _paired(need_exit, prices), strict=True):
                row["exit_price"] = price

    async def _size_new_positions(
        self,
        open_rows: list[dict[str, object]],
        closed_rows: list[dict[str, object]],
    ) -> None:
        """Kelly-size freshly stamped opens against the paper bankroll.

        Bankroll = starting capital + cumulative realized dollars. New
        positions claim min(fraction x bankroll, uncommitted bankroll) so
        gross exposure never exceeds 100% of the paper book.
        """
        unsized = [
            row
            for row in open_rows
            if row["entry_price"] is not None and row["size_notional"] is None
        ]
        if not unsized:
            return
        realized = sum(
            usd for row in closed_rows if (usd := _realized_usd(row)) is not None
        )
        bankroll = STARTING_CAPITAL + realized
        committed = sum(
            float(str(row["size_notional"]))
            for row in open_rows
            if row["size_notional"] is not None
        )
        sizes: list[tuple[int, float]] = []
        for row in unsized:
            fraction = _position_fraction(row)
            available = max(bankroll - committed, 0.0)
            notional = round(min(fraction * bankroll, available), 2)
            row["size_notional"] = notional
            committed += notional
            sizes.append((int(str(row["id"])), notional))
        await asyncio.to_thread(db.set_fringe_sizes, self.database_path, sizes=sizes)

    async def _prices_for(self, tickers: set[str]) -> dict[str, float | None]:
        """Last prices for arbitrary tickers, TTL-cached; failures yield None."""
        now = monotonic()
        prices: dict[str, float | None] = {}
        missing: list[str] = []
        for ticker in sorted(tickers):
            cached = self._quote_cache.get(ticker)
            if cached is not None and now - cached[1] < self.QUOTE_TTL_SECONDS:
                prices[ticker] = cached[0]
            else:
                missing.append(ticker)
        if not missing:
            return prices
        by_source: dict[ProviderName, list[AssetConfig]] = {}
        for ticker in missing:
            prices[ticker] = None  # provider failure leaves the price null
            asset = await self._asset_for(ticker)
            by_source.setdefault(asset.source, []).append(asset)
        results = await asyncio.gather(
            *(self._fetch_quotes(source, assets) for source, assets in by_source.items()),
        )
        for quotes in results:
            for quote in quotes:
                if quote.error is None and quote.last > 0:
                    prices[quote.symbol] = quote.last
                    self._quote_cache[quote.symbol] = (quote.last, monotonic())
        return prices

    async def _asset_for(self, ticker: str) -> AssetConfig:
        """Route arbitrary (non-watchlist) tickers to a provider.

        Lighter serves it only when it both lists the market AND classifies
        it as crypto: its TradFi synthetics collide with exchange tickers
        (Lighter's ROBO token is not the robotics ETF). Everything else is
        treated as a Yahoo equity — Yahoo resolves ETFs/futures fine.
        """
        lighter = self.providers.get("lighter")
        if isinstance(lighter, LighterProvider):
            try:
                if await lighter.has_market(ticker) and lighter.is_crypto_market(ticker):
                    return AssetConfig(symbol=ticker, type="crypto_perp", source="lighter")
            except Exception:
                logger.warning("lighter market lookup failed for %s", ticker, exc_info=True)
        return AssetConfig(symbol=ticker, type="equity", source="yahoo")

    async def _fetch_quotes(
        self, source: ProviderName, assets: list[AssetConfig]
    ) -> list[Quote]:
        provider = self.providers.get(source)
        if provider is None:
            return []
        try:
            return await provider.get_quotes(assets)
        except Exception:
            logger.warning("fringe quote fetch via %s failed", source, exc_info=True)
            return []


def _stampable(
    rows: list[dict[str, object]], prices: dict[str, float | None]
) -> list[tuple[int, float]]:
    return [(idea_id, price) for idea_id, price in _paired(rows, prices) if price is not None]


def _paired(
    rows: list[dict[str, object]], prices: dict[str, float | None]
) -> list[tuple[int, float | None]]:
    return [(int(str(row["id"])), prices.get(str(row["ticker"]))) for row in rows]


def _open_item(
    row: dict[str, object], last: float | None, latest_mention: str | None
) -> dict[str, object]:
    entry = row["entry_price"]
    last_mentioned = str(row["last_mentioned"])
    target_price = _target_price(row["target"])
    unrealized_pct = _pnl_pct(str(row["direction"]), entry, last)
    return {
        "id": row["id"],
        "ticker": row["ticker"],
        "direction": row["direction"],
        "thesis": row["thesis"],
        "horizon": row["horizon"],
        "target": row["target"],
        "target_price": _round_price(target_price),
        "opened": row["opened_date"],
        "last_mentioned": last_mentioned,
        # Open but not refreshed by the newest report that fed the book.
        "stale": latest_mention is not None and last_mentioned < latest_mention,
        "entry_price": _round_price(entry),
        "last": _round_price(last),
        "unrealized_pct": unrealized_pct,
        # Signed % from the current mark to the target in the idea's
        # direction — the move still on the table; null without both.
        "to_target_pct": _pnl_pct(str(row["direction"]), last, target_price),
        "confidence": row["confidence"],
        "stop": row["stop"],
        "stop_price": _round_price(_target_price(row["stop"])),
        "size_notional": _round_usd(row["size_notional"]),
        "unrealized_usd": _position_usd(row["size_notional"], unrealized_pct),
        "source_slug": row["source_slug"],
    }


def _closed_item(row: dict[str, object]) -> dict[str, object]:
    realized_pct = _pnl_pct(str(row["direction"]), row["entry_price"], row["exit_price"])
    return {
        "id": row["id"],
        "ticker": row["ticker"],
        "direction": row["direction"],
        "thesis": row["thesis"],
        "target": row["target"],
        "opened": row["opened_date"],
        "closed": row["closed_date"],
        "entry_price": _round_price(row["entry_price"]),
        "exit_price": _round_price(row["exit_price"]),
        "realized_pct": realized_pct,
        "size_notional": _round_usd(row["size_notional"]),
        "realized_usd": _position_usd(row["size_notional"], realized_pct),
        "close_reason": row["close_reason"],
    }


def _performance_summary(
    open_items: list[dict[str, object]], closed_items: list[dict[str, object]]
) -> dict[str, object]:
    """Equal-weight return across every marked idea, plus the paper portfolio."""
    pnl_values: list[float] = []
    for item in open_items:
        value = item["unrealized_pct"]
        if isinstance(value, int | float):
            pnl_values.append(float(value))
    for item in closed_items:
        value = item["realized_pct"]
        if isinstance(value, int | float):
            pnl_values.append(float(value))
    overall = round(sum(pnl_values) / len(pnl_values), 2) if pnl_values else None
    return {
        "overall_pnl_pct": 0.0 if overall == 0 else overall,
        "marked_count": len(pnl_values),
        "idea_count": len(open_items) + len(closed_items),
        "open_count": len(open_items),
        "closed_count": len(closed_items),
        "portfolio": _portfolio_summary(open_items, closed_items),
    }


def _portfolio_summary(
    open_items: list[dict[str, object]], closed_items: list[dict[str, object]]
) -> dict[str, object]:
    """The $-denominated paper book: equity, exposure, realized/unrealized."""
    realized = sum(
        float(str(item["realized_usd"]))
        for item in closed_items
        if item["realized_usd"] is not None
    )
    unrealized = sum(
        float(str(item["unrealized_usd"]))
        for item in open_items
        if item["unrealized_usd"] is not None
    )
    invested = sum(
        float(str(item["size_notional"]))
        for item in open_items
        if item["size_notional"] is not None
    )
    equity = STARTING_CAPITAL + realized + unrealized
    return {
        "starting_capital": STARTING_CAPITAL,
        "equity": round(equity, 2),
        "return_pct": round((equity / STARTING_CAPITAL - 1.0) * 100.0, 2),
        "realized_usd": round(realized, 2),
        "unrealized_usd": round(unrealized, 2),
        "invested_notional": round(invested, 2),
        "exposure_pct": round(invested / equity * 100.0, 1) if equity > 0 else None,
    }


def _position_fraction(row: dict[str, object]) -> float:
    """Bankroll fraction for a new position: clamped half-Kelly, or the
    conservative default when the agent declared no usable edge inputs."""
    kelly = _kelly_fraction(
        str(row["direction"]),
        row["entry_price"],
        _target_price(row["stop"]),
        _target_price(row["target"]),
        row["confidence"],
    )
    if kelly is None:
        return DEFAULT_POSITION_FRACTION
    scaled = kelly * KELLY_MULTIPLIER
    return min(max(scaled, MIN_POSITION_FRACTION), MAX_POSITION_FRACTION)


def _kelly_fraction(
    direction: str,
    entry: object,
    stop: float | None,
    target: float | None,
    confidence: object,
) -> float | None:
    """Kelly f* = p - q/b with b = reward/risk from entry vs target/stop.

    None when the declared geometry is unusable (missing numbers, stop on
    the wrong side, target behind the entry) — the caller falls back to the
    default fraction instead of trusting broken inputs.
    """
    if (
        not isinstance(entry, int | float)
        or not isinstance(confidence, int | float)
        or stop is None
        or target is None
        or entry <= 0
    ):
        return None
    sign = -1.0 if direction == "short" else 1.0
    reward = sign * (target - entry)
    risk = sign * (entry - stop)
    if reward <= 0 or risk <= 0:
        return None
    b = reward / risk
    p = float(confidence) / 100.0
    return p - (1.0 - p) / b


def _realized_usd(row: dict[str, object]) -> float | None:
    """Dollar result of a sized closed idea; None for the pre-capital era."""
    return _position_usd(
        row["size_notional"],
        _pnl_pct(str(row["direction"]), row["entry_price"], row["exit_price"]),
    )


def _position_usd(notional: object, pct: object) -> float | None:
    if not isinstance(notional, int | float) or not isinstance(pct, int | float):
        return None
    return round(float(notional) * float(pct) / 100.0, 2)


def _round_usd(value: object) -> float | None:
    return round(float(value), 2) if isinstance(value, int | float) else None


def _pnl_pct(direction: str, entry: object, exit_: object) -> float | None:
    """Signed percent move: long profits up, short profits down; null-safe."""
    if not isinstance(entry, int | float) or not isinstance(exit_, int | float) or entry == 0:
        return None
    pct = (exit_ - entry) / entry * 100.0
    if direction == "short":
        pct = -pct
    return round(pct, 2)


def _target_price(target: object) -> float | None:
    """Price parsed from the target's free text; None when it names no number."""
    if not isinstance(target, str):
        return None
    match = _TARGET_PRICE.search(target)
    if match is None:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:  # pragma: no cover - the pattern only admits floats
        return None
    return value * 1000.0 if match.group(2) else value


def _round_price(value: object) -> float | None:
    return round(value, 4) if isinstance(value, int | float) else None
