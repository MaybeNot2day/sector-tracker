from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TypeAlias

from app import db
from app.models import AssetConfig, GroupConfig, ProviderName, Quote
from app.providers.base import QuoteProvider
from app.providers.lighter import LighterProvider

GroupsCacheKey: TypeAlias = tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...]


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
        self._refresh_lock = asyncio.Lock()

    async def get_board_quotes(self, groups: list[GroupConfig]) -> dict[str, list[Quote]]:
        cache_key = _groups_cache_key(groups)
        cached = self._cached_quotes(cache_key)
        if cached is not None:
            return cached

        async with self._refresh_lock:
            cached = self._cached_quotes(cache_key)
            if cached is not None:
                return cached

            fresh_by_symbol = await self._fetch_fresh_quotes(groups)
            db.save_quotes(self.database_path, list(fresh_by_symbol.values()))

            result: dict[str, list[Quote]] = {}
            for group in groups:
                quotes: list[Quote] = []
                for asset in group.assets:
                    quote = fresh_by_symbol.get(asset.symbol)
                    if quote is None:
                        quote = self._stale_or_error(asset)
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

    async def _fetch_fresh_quotes(self, groups: list[GroupConfig]) -> dict[str, Quote]:
        by_provider: dict[ProviderName, list[AssetConfig]] = {}
        for group in groups:
            for asset in group.assets:
                by_provider.setdefault(asset.source, []).append(asset)
        by_provider = {
            source: self._prioritize_uncached_assets(assets)
            for source, assets in by_provider.items()
        }

        fresh_by_symbol: dict[str, Quote] = {}
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

        await self._overlay_lighter_prices(groups, fresh_by_symbol)
        return fresh_by_symbol

    async def _overlay_lighter_prices(
        self,
        groups: list[GroupConfig],
        fresh_by_symbol: dict[str, Quote],
    ) -> None:
        """Live 24/7 prices for equities/ETFs that Lighter also lists as perps.

        Lighter's synthetic equity markets trade around the clock, so they
        drive price discovery while official-session data (previous close,
        share volume, daily bars) stays with the listing venue. The 1D
        baseline is the last official close — the venue's previous close
        while its quote is fresh (session live), else its last print (after
        hours and weekends).
        """
        lighter = self.providers.get("lighter")
        if not isinstance(lighter, LighterProvider):
            return
        candidates = {
            asset.symbol
            for group in groups
            for asset in group.assets
            if asset.type in {"equity", "etf"} and asset.source != "lighter"
        }
        if not candidates:
            return
        try:
            live_prices = await lighter.live_prices(candidates)
        except Exception:
            return
        now = datetime.now(UTC)
        for symbol, live in live_prices.items():
            quote = fresh_by_symbol.get(symbol)
            if quote is None or quote.error or quote.last <= 0:
                continue
            if quote.currency not in (None, "USD"):
                # Non-USD listings keep their venue pricing; the FX display
                # pipeline is built around the listing currency.
                continue
            baseline = _official_close(quote, now)
            if baseline is None or baseline <= 0:
                continue
            fresh_by_symbol[symbol] = replace(
                quote,
                provider="lighter",
                last=live,
                previous_close=baseline,
                change_abs=round(live - baseline, 6),
                change_pct=round((live - baseline) / baseline * 100, 6),
                timestamp=now,
            )

    def _prioritize_uncached_assets(self, assets: list[AssetConfig]) -> list[AssetConfig]:
        return sorted(
            assets,
            key=lambda asset: (
                db.load_latest_quote(self.database_path, asset.symbol) is not None,
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
            return []

    def _stale_or_error(self, asset: AssetConfig) -> Quote:
        cached = db.load_latest_quote(self.database_path, asset.symbol)
        if cached:
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


# A listing-venue quote older than this means the session (incl. pre/post
# prints) is over; its last price then IS the most recent official close.
OFFICIAL_QUOTE_FRESH_SECONDS = 3600.0


def _official_close(quote: Quote, now: datetime) -> float | None:
    """Most recent official session close for a listing-venue quote.

    While the venue prints trades (fresh timestamp) the last completed close
    is `previous_close`; once prints stop (overnight, weekends) the venue's
    final `last` becomes the close to measure against.
    """
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
    return {
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
        "display_last": quote.display_last if quote.display_last is not None else quote.last,
        "display_previous_close": (
            quote.display_previous_close
            if quote.display_previous_close is not None
            else quote.previous_close
        ),
        "display_change_abs": (
            quote.display_change_abs if quote.display_change_abs is not None else quote.change_abs
        ),
        "display_change_pct": (
            quote.display_change_pct if quote.display_change_pct is not None else quote.change_pct
        ),
        "display_currency": quote.display_currency or quote.currency,
        "volume": quote.volume,
        "funding_rate": quote.funding_rate,
        "open_interest_usd": quote.open_interest_usd,
    }


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
