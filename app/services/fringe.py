"""Fringe Corner: a Hermes-managed daily trading-ideas ledger.

Hermes pushes a markdown report through the normal reports pipeline; any
section whose heading mentions "fringe" feeds the book, one action per
bullet::

    - OPEN LONG CIFR — thesis text [horizon: 2w]
    - HOLD SHORT XLU — updated note
    - CLOSE LONG NVDA — reason text

ACTION is OPEN/HOLD/CLOSE (case-insensitive), DIRECTION LONG/SHORT, the
ticker an uppercase [A-Z0-9.-=] token, then an em-dash/colon (or a spaced
hyphen — tickers like BRK-B own the unspaced one) before the free text,
with an optional trailing ``[horizon: ...]`` tag. Malformed bullets are
skipped, never fatal — same forgiveness as key_dates.

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
_HORIZON = re.compile(r"\[\s*horizon\s*:\s*([^\]]*?)\s*\]\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FringeAction:
    action: str  # open | hold | close
    ticker: str
    direction: str  # long | short
    text: str  # thesis / updated note / close reason
    horizon: str | None


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
    horizon: str | None = None
    horizon_match = _HORIZON.search(remainder)
    if horizon_match is not None:
        horizon = horizon_match.group(1)[:_MAX_HORIZON] or None
        remainder = remainder[: horizon_match.start()].rstrip()
    return FringeAction(
        action=match.group("action").lower(),
        ticker=match.group("ticker"),
        direction=match.group("direction").lower(),
        text=remainder[:_MAX_TEXT],
        horizon=horizon,
    )


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
        """The /api/fringe contract: open book marked to market + recent closes."""
        open_rows, closed_rows = await self._load_and_restamp()
        latest = await asyncio.to_thread(db.latest_fringe_mention, self.database_path)
        prices = await self._prices_for({str(row["ticker"]) for row in open_rows})
        return {
            "as_of": datetime.now(UTC).isoformat(),
            "open": [_open_item(row, prices.get(str(row["ticker"])), latest) for row in open_rows],
            "closed": [_closed_item(row) for row in closed_rows],
        }

    async def _load_and_restamp(
        self,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Load both books, lazily re-stamping prices a past ingest missed."""
        open_rows = await asyncio.to_thread(
            db.load_fringe_ideas, self.database_path, status="open"
        )
        closed_rows = await asyncio.to_thread(
            db.load_fringe_ideas, self.database_path, status="closed", limit=self.RECENT_CLOSED
        )
        need_entry = [row for row in open_rows if row["entry_price"] is None]
        need_exit = [row for row in closed_rows if row["exit_price"] is None]
        if not need_entry and not need_exit:
            return open_rows, closed_rows
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
        return open_rows, closed_rows

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
    return {
        "id": row["id"],
        "ticker": row["ticker"],
        "direction": row["direction"],
        "thesis": row["thesis"],
        "horizon": row["horizon"],
        "opened": row["opened_date"],
        "last_mentioned": last_mentioned,
        # Open but not refreshed by the newest report that fed the book.
        "stale": latest_mention is not None and last_mentioned < latest_mention,
        "entry_price": _round_price(entry),
        "last": _round_price(last),
        "unrealized_pct": _pnl_pct(str(row["direction"]), entry, last),
        "source_slug": row["source_slug"],
    }


def _closed_item(row: dict[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "ticker": row["ticker"],
        "direction": row["direction"],
        "thesis": row["thesis"],
        "opened": row["opened_date"],
        "closed": row["closed_date"],
        "entry_price": _round_price(row["entry_price"]),
        "exit_price": _round_price(row["exit_price"]),
        "realized_pct": _pnl_pct(str(row["direction"]), row["entry_price"], row["exit_price"]),
        "close_reason": row["close_reason"],
    }


def _pnl_pct(direction: str, entry: object, exit_: object) -> float | None:
    """Signed percent move: long profits up, short profits down; null-safe."""
    if not isinstance(entry, int | float) or not isinstance(exit_, int | float) or entry == 0:
        return None
    pct = (exit_ - entry) / entry * 100.0
    if direction == "short":
        pct = -pct
    return round(pct, 2)


def _round_price(value: object) -> float | None:
    return round(value, 4) if isinstance(value, int | float) else None
