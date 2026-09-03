from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import TypeAlias

from app import db
from app.models import AssetConfig, GroupConfig, ProviderName, Quote
from app.providers.base import QuoteProvider
from app.providers.hyperliquid import HyperliquidProvider

logger = logging.getLogger(__name__)

GroupsCacheKey: TypeAlias = tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...]

# With a live poll loop the last snapshot is served instantly while a refresh
# runs in the background; beyond this age the data is too old to serve blind.
STALE_WHILE_REVALIDATE_SECONDS = 120.0

# Personal-list lookups bypass the board cache; keep a short per-symbol TTL so
# rapid list switching or re-renders reuse the same provider round-trip.
LOOKUP_CACHE_SECONDS = 30.0


class QuoteService:
    def __init__(
        self,
        database_path: Path,
        providers: dict[ProviderName, QuoteProvider],
        *,
        min_refresh_seconds: int = 0,
    ) -> None:
        self.database_path = database_path
        self.providers = providers
        self.min_refresh_seconds = min_refresh_seconds
        self._cache_key: GroupsCacheKey | None = None
        self._cache_time = 0.0
        self._cached_grouped: dict[str, list[Quote]] | None = None
        self._lookup_cache: dict[str, tuple[float, Quote]] = {}
        self._lookup_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()

    async def get_board_quotes(
        self, groups: list[GroupConfig], *, allow_stale: bool = False
    ) -> dict[str, list[Quote]]:
        cache_key = _groups_cache_key(groups)
        cached = self._cached_quotes(cache_key)
        if cached is not None:
            return cached
        if (
            allow_stale
            and self._cached_grouped is not None
            and self._cache_key == cache_key
            and monotonic() - self._cache_time < STALE_WHILE_REVALIDATE_SECONDS
        ):
            # Serve the last snapshot instantly and refresh in the background.
            # Gated on allow_stale: serverless deployments have no poll loop,
            # so the synchronous fetch below is their only refresh path.
            self._schedule_refresh(groups, cache_key)
            return self._cached_grouped
        if allow_stale and self._cached_grouped is None:
            # Cold process (fresh deploy): the last poll's quotes are already
            # persisted in SQLite. Serve them instantly as stale and let the
            # (possibly already in-flight) refresh replace them, instead of
            # blocking the first paint behind a full provider sweep.
            seeded = await self._seed_from_persisted(groups, cache_key)
            if seeded is not None:
                self._schedule_refresh(groups, cache_key)
                return seeded
        async with self._refresh_lock:
            cached = self._cached_quotes(cache_key)
            if cached is not None:
                return cached
            return await self._refresh(groups, cache_key)

    async def _seed_from_persisted(
        self, groups: list[GroupConfig], cache_key: GroupsCacheKey
    ) -> dict[str, list[Quote]] | None:
        symbols = sorted({asset.symbol for group in groups for asset in group.assets})
        try:
            cached_by_symbol = await asyncio.to_thread(
                db.load_latest_quotes, self.database_path, symbols
            )
        except Exception:
            logger.warning("persisted quote seed failed", exc_info=True)
            return None
        if self._cached_grouped is not None:
            # A concurrent refresh landed while we were reading SQLite: the
            # live snapshot must win — never clobber it with stale rows.
            return self._cached_grouped if self._cache_key == cache_key else None
        hits = 0
        result: dict[str, list[Quote]] = {}
        for group in groups:
            quotes: list[Quote] = []
            for asset in group.assets:
                cached = cached_by_symbol.get(asset.symbol.upper())
                if cached is not None and _cached_quote_matches(asset, cached):
                    hits += 1
                quotes.append(self._stale_or_error(asset, cached_by_symbol))
            result[group.name] = quotes
        if not hits:
            return None  # empty database: nothing better than waiting for live
        # Install as the current snapshot but dated one refresh window in the
        # past: subsequent requests ride stale-while-revalidate immediately,
        # and the scheduled refresh still runs because the cache reads expired.
        self._cache_key = cache_key
        self._cached_grouped = result
        self._cache_time = monotonic() - max(self.min_refresh_seconds, 1.0)
        return result

    async def get_lookup_quotes(self, assets: list[AssetConfig]) -> dict[str, Quote]:
        """Fresh quotes for arbitrary (possibly off-board) assets.

        Powers the personal named watch lists: their symbols are free-typed,
        so they may not exist in any board group. A short per-symbol cache
        keeps list re-renders from hammering the providers.
        """
        now = monotonic()
        result: dict[str, Quote] = {}
        missing: list[AssetConfig] = []
        for asset in assets:
            cached = self._lookup_cache.get(asset.symbol.upper())
            if cached is not None and now - cached[0] < LOOKUP_CACHE_SECONDS:
                result[asset.symbol.upper()] = cached[1]
            else:
                missing.append(asset)
        if not missing:
            return result
        async with self._lookup_lock:
            now = monotonic()
            still_missing: list[AssetConfig] = []
            for asset in missing:
                cached = self._lookup_cache.get(asset.symbol.upper())
                if cached is not None and now - cached[0] < LOOKUP_CACHE_SECONDS:
                    result[asset.symbol.upper()] = cached[1]
                else:
                    still_missing.append(asset)
            if still_missing:
                by_provider: dict[ProviderName, list[AssetConfig]] = {}
                for asset in still_missing:
                    by_provider.setdefault(asset.source, []).append(asset)
                tasks = [
                    self._safe_provider_quotes(source, items)
                    for source, items in by_provider.items()
                    if source in self.providers
                ]
                fetched = monotonic()
                for quotes in await asyncio.gather(*tasks):
                    for quote in quotes:
                        if quote.error or quote.last <= 0:
                            continue
                        key = quote.symbol.upper()
                        result[key] = quote
                        self._lookup_cache[key] = (fetched, quote)
        return result

    def _schedule_refresh(self, groups: list[GroupConfig], cache_key: GroupsCacheKey) -> None:
        if self._refresh_lock.locked():
            return  # a refresh is already in flight
        task = asyncio.create_task(self._refresh_locked(groups, cache_key))
        task.add_done_callback(self._log_refresh_failure)

    async def _refresh_locked(self, groups: list[GroupConfig], cache_key: GroupsCacheKey) -> None:
        async with self._refresh_lock:
            if self._cached_quotes(cache_key) is not None:
                return  # the waiter's refresh already covered us
            await self._refresh(groups, cache_key)

    @staticmethod
    def _log_refresh_failure(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("background quote refresh failed", exc_info=exc)

    async def _refresh(
        self, groups: list[GroupConfig], cache_key: GroupsCacheKey
    ) -> dict[str, list[Quote]]:
        requested_symbols = sorted({asset.symbol for group in groups for asset in group.assets})
        cached_by_symbol = await asyncio.to_thread(
            db.load_latest_quotes, self.database_path, requested_symbols
        )
        matching_cached_symbols = {
            asset.symbol
            for group in groups
            for asset in group.assets
            if (cached_quote := cached_by_symbol.get(asset.symbol.upper())) is not None
            and _cached_quote_matches(asset, cached_quote)
        }
        fresh_by_symbol = await self._fetch_fresh_quotes(groups, matching_cached_symbols)
        await asyncio.to_thread(db.save_quotes, self.database_path, list(fresh_by_symbol.values()))

        result: dict[str, list[Quote]] = {}
        for group in groups:
            quotes: list[Quote] = []
            for asset in group.assets:
                quote = fresh_by_symbol.get(asset.symbol)
                if quote is None:
                    quote = self._stale_or_error(asset, cached_by_symbol)
                quotes.append(quote)
            result[group.name] = quotes

        self._cache_key = cache_key
        self._cache_time = monotonic()
        self._cached_grouped = result
        return result

    def _cached_quotes(self, cache_key: GroupsCacheKey) -> dict[str, list[Quote]] | None:
        if self.min_refresh_seconds <= 0 or self._cached_grouped is None:
            return None
        if self._cache_key != cache_key:
            return None
        if monotonic() - self._cache_time >= self.min_refresh_seconds:
            return None
        return self._cached_grouped

    async def _fetch_fresh_quotes(
        self,
        groups: list[GroupConfig],
        cached_symbols: set[str] | None = None,
    ) -> dict[str, Quote]:
        cached_symbols = cached_symbols or set()
        by_provider: dict[ProviderName, list[AssetConfig]] = {}
        for group in groups:
            for asset in group.assets:
                by_provider.setdefault(asset.source, []).append(asset)
        fresh_by_symbol = await self._hl_first_quotes(groups)
        by_provider = {
            source: self._prioritize_uncached_assets(assets, cached_symbols)
            for source, assets in by_provider.items()
        }
        by_provider = {
            source: [asset for asset in assets if asset.symbol not in fresh_by_symbol]
            for source, assets in by_provider.items()
        }

        tasks = [
            self._safe_provider_quotes(source, assets)
            for source, assets in by_provider.items()
            if source in self.providers
        ]
        for quotes in await asyncio.gather(*tasks):
            for quote in quotes:
                fresh_by_symbol[quote.symbol] = quote

        missing_fallback_assets = [
            asset
            for group in groups
            for asset in group.assets
            if asset.symbol not in fresh_by_symbol
            and asset.source != "stooq"
            and asset.type in {"equity", "etf"}
        ]
        stooq = self.providers.get("stooq")
        if stooq and missing_fallback_assets:
            for quote in await self._safe_provider_quotes("stooq", missing_fallback_assets):
                fresh_by_symbol[quote.symbol] = quote

        await self._overlay_hyperliquid_prices(groups, fresh_by_symbol)
        return fresh_by_symbol

    async def _hl_first_quotes(self, groups: list[GroupConfig]) -> dict[str, Quote]:
        """Quotes sourced entirely from Hyperliquid for equities/ETFs it lists.

        The venue round-trip is skipped for covered symbols: last price is the
        24/7 xyz mark, and the 1D baseline is the last COMPLETED official
        session close from the cached daily bars (same semantics as the
        overlay's `_official_close`). Symbols without a live mark or a usable
        baseline fall through to the normal venue fetch.
        """
        hyperliquid = self.providers.get("hyperliquid")
        if not isinstance(hyperliquid, HyperliquidProvider):
            return {}
        assets = {
            asset.symbol.upper(): asset
            for group in groups
            for asset in group.assets
            if asset.type in {"equity", "etf"} and asset.source != "hyperliquid"
        }
        if not assets:
            return {}
        try:
            live_prices = await hyperliquid.live_prices(set(assets))
        except Exception:
            logger.warning("Hyperliquid-first quote probe failed", exc_info=True)
            return {}
        if not live_prices:
            return {}
        try:
            tails = await asyncio.to_thread(
                db.load_daily_close_tails, self.database_path, list(live_prices)
            )
        except Exception:
            logger.warning("baseline close load failed", exc_info=True)
            return {}
        now = datetime.now(UTC)
        quotes: dict[str, Quote] = {}
        for symbol, live in live_prices.items():
            asset = assets.get(symbol.upper())
            baseline = _baseline_close(tails.get(symbol.upper(), []), now)
            if asset is None or baseline is None:
                continue  # no trustworthy official close: let the venue answer
            quotes[asset.symbol] = Quote.from_last_and_prev_close(
                symbol=asset.symbol,
                asset_type=asset.type,
                provider="hyperliquid",
                last=live,
                previous_close=baseline,
                timestamp=now,
                currency="USD",
                official_close=baseline,
            )
        return quotes

    async def _overlay_hyperliquid_prices(
        self,
        groups: list[GroupConfig],
        fresh_by_symbol: dict[str, Quote],
    ) -> None:
        """Live 24/7 prices for equities/ETFs that Hyperliquid also lists as perps.

        Hyperliquid's synthetic equity markets trade around the clock, so they
        drive price discovery while official-session data (previous close,
        share volume, daily bars) stays with the listing venue. The 1D
        baseline is the close of the last COMPLETED official session — the
        provider's explicit value when carried, else a freshness heuristic.
        """
        hyperliquid = self.providers.get("hyperliquid")
        if not isinstance(hyperliquid, HyperliquidProvider):
            return
        candidates = {
            asset.symbol
            for group in groups
            for asset in group.assets
            if asset.type in {"equity", "etf"} and asset.source != "hyperliquid"
        }
        if not candidates:
            return
        try:
            live_prices = await hyperliquid.live_prices(candidates)
        except Exception:
            logger.warning(
                "Hyperliquid equity overlay failed for %d symbols",
                len(candidates),
                exc_info=True,
            )
            return
        now = datetime.now(UTC)
        for symbol, live in live_prices.items():
            quote = fresh_by_symbol.get(symbol)
            if quote is None or quote.error or quote.last <= 0:
                continue
            if quote.currency != "USD":
                # Unknown or non-USD listing currency is not safe to combine
                # with a USD-denominated synthetic mark.
                continue
            baseline = _official_close(quote, now)
            if baseline is None or baseline <= 0:
                continue
            fresh_by_symbol[symbol] = replace(
                quote,
                provider="hyperliquid",
                last=live,
                previous_close=baseline,
                change_abs=round(live - baseline, 6),
                change_pct=round((live - baseline) / baseline * 100, 6),
                timestamp=now,
            )

    def _prioritize_uncached_assets(
        self, assets: list[AssetConfig], cached_symbols: set[str]
    ) -> list[AssetConfig]:
        return sorted(
            assets,
            key=lambda asset: (
                asset.symbol.upper() in cached_symbols,
                asset.symbol,
            ),
        )

    async def _safe_provider_quotes(
        self, source: ProviderName, assets: list[AssetConfig]
    ) -> list[Quote]:
        provider = self.providers[source]
        try:
            return await provider.get_quotes(assets)
        except Exception:
            logger.warning(
                "quote fetch via %s failed for %d assets",
                source,
                len(assets),
                exc_info=True,
            )
            return []

    def _stale_or_error(self, asset: AssetConfig, cached_by_symbol: dict[str, Quote]) -> Quote:
        cached = cached_by_symbol.get(asset.symbol.upper())
        if cached is not None and _cached_quote_matches(asset, cached):
            return db.mark_stale(cached)
        return Quote(
            symbol=asset.symbol,
            asset_type=asset.type,
            provider=asset.source,
            last=0.0,
            previous_close=None,
            change_abs=None,
            change_pct=None,
            timestamp=datetime.now(UTC),
            is_stale=True,
            error="no_quote_available",
        )


def _cached_quote_matches(asset: AssetConfig, cached: Quote) -> bool:
    """Reject symbol collisions after a watchlist asset changes identity."""
    return cached.asset_type == asset.type


# A listing-venue quote older than this means the session (incl. pre/post
# prints) is over; its last price then IS the most recent official close.
OFFICIAL_QUOTE_FRESH_SECONDS = 3600.0
# US regular sessions end 20:00-21:00 UTC (DST shift); a daily bar counts as
# the completed official close once its date's buffer passes.
SESSION_CLOSE_BUFFER = time(21, 30)
# Older baselines mean the venue history is broken; re-fetch via the venue.
BASELINE_MAX_AGE = timedelta(days=5)


def _baseline_close(tail: list[tuple[datetime, float]], now: datetime) -> float | None:
    """Last completed official session close from a daily-bar close tail.

    A bar dated today whose session has not ended yet is still forming, so the
    baseline is the previous bar's close — the venue's `previous_close`.
    """
    if not tail:
        return None
    last_ts, last_close = tail[-1]
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=UTC)
    if now - last_ts > BASELINE_MAX_AGE:
        return None
    session_end = datetime.combine(last_ts.date(), SESSION_CLOSE_BUFFER, tzinfo=UTC)
    close = last_close if now >= session_end else (tail[-2][1] if len(tail) >= 2 else None)
    return close if close is not None and close > 0 else None


def _official_close(quote: Quote, now: datetime) -> float | None:
    """Close of the last completed official session for a listing-venue quote.

    Providers that see the venue payload carry it explicitly (Yahoo) — that
    value is exact in every state, including the Friday close→post window
    and weekends where `last` is a pre/post print. Without it, fall back to
    freshness: while the venue prints trades (fresh timestamp) the last
    completed close is `previous_close`; once prints stop (overnight,
    weekends) the venue's final `last` becomes the close to measure against.
    """
    if quote.official_close is not None and quote.official_close > 0:
        return quote.official_close
    timestamp = quote.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    age = (now - timestamp).total_seconds()
    if age <= OFFICIAL_QUOTE_FRESH_SECONDS:
        return quote.previous_close
    return quote.last


def grouped_quotes_payload(
    groups: list[GroupConfig],
    grouped_quotes: dict[str, list[Quote]],
    summaries: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    summaries = summaries or {}
    return {
        "groups": [
            {
                "name": group.name,
                "assets": [
                    {
                        "symbol": asset.symbol,
                        "name": asset.name,
                        "type": asset.type,
                        "exchange": asset.exchange,
                        "quote": quote_payload(quote),
                        "summary": summaries.get(asset.symbol, {}),
                    }
                    for asset, quote in zip(
                        group.assets, grouped_quotes.get(group.name, []), strict=False
                    )
                ],
            }
            for group in groups
        ]
    }


def quote_payload(quote: Quote) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": quote.symbol,
        "asset_type": quote.asset_type,
        "provider": quote.provider,
        "last": quote.last,
        "previous_close": quote.previous_close,
        "change_abs": quote.change_abs,
        "change_pct": quote.change_pct,
        "timestamp": quote.timestamp.isoformat(),
        "is_stale": quote.is_stale,
        "error": quote.error,
        "currency": quote.currency,
        "volume": quote.volume,
        "funding_rate": quote.funding_rate,
        "open_interest_usd": quote.open_interest_usd,
    }
    # display_* mirror the base fields for most (USD) quotes. The frontend
    # falls back to the base field when a display key is absent, so only ship
    # them when FX normalization actually made them differ — this trims five
    # duplicated fields per asset from every board frame.
    if quote.display_last is not None and quote.display_last != quote.last:
        payload["display_last"] = quote.display_last
    if (
        quote.display_previous_close is not None
        and quote.display_previous_close != quote.previous_close
    ):
        payload["display_previous_close"] = quote.display_previous_close
    if quote.display_change_abs is not None and quote.display_change_abs != quote.change_abs:
        payload["display_change_abs"] = quote.display_change_abs
    if quote.display_change_pct is not None and quote.display_change_pct != quote.change_pct:
        payload["display_change_pct"] = quote.display_change_pct
    if quote.display_currency and quote.display_currency != quote.currency:
        payload["display_currency"] = quote.display_currency
    return payload


def clone_quote_with_provider(quote: Quote, provider: ProviderName) -> Quote:
    return replace(quote, provider=provider)


def _groups_cache_key(groups: list[GroupConfig]) -> GroupsCacheKey:
    return tuple(
        (
            group.name,
            tuple((asset.symbol, asset.type, asset.source) for asset in group.assets),
        )
        for group in groups
    )
