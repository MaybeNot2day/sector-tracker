from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import pytest

from app import db
from app.models import AssetConfig, Bar, GroupConfig, Quote
from app.providers.base import QuoteProvider
from app.providers.hyperliquid import HyperliquidProvider
from app.services.quotes import QuoteService, _baseline_close, _official_close


class ScriptedQuotes(QuoteProvider):
    name = "yahoo"

    def __init__(self, quotes: dict[str, Quote]) -> None:
        self._quotes = quotes

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        return [self._quotes[asset.symbol] for asset in assets if asset.symbol in self._quotes]

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        return []


def yahoo_quote(
    symbol: str,
    *,
    last: float = 210.0,
    previous_close: float | None = 208.0,
    timestamp: datetime | None = None,
    currency: str | None = "USD",
    volume: float | None = None,
    error: str | None = None,
    official_close: float | None = None,
) -> Quote:
    return Quote.from_last_and_prev_close(
        symbol=symbol,
        asset_type="equity",
        provider="yahoo",
        last=last,
        previous_close=previous_close,
        timestamp=timestamp or datetime.now(UTC),
        currency=currency,
        volume=volume,
        error=error,
        official_close=official_close,
    )


def hyperliquid_with(tradfi: dict[str, dict[str, Any]]) -> HyperliquidProvider:
    """Real HyperliquidProvider with a warm market map, so overlay never does HTTP."""
    provider = HyperliquidProvider()
    provider._tradfi = tradfi
    provider._markets_time = monotonic()
    provider._tradfi_time = provider._markets_time
    return provider


def aapl_market(last: float = 213.5) -> dict[str, dict[str, Any]]:
    return {"AAPL": {"coin": "xyz:AAPL", "display": "AAPL", "last": last}}


def equity_group(*symbols: str) -> list[GroupConfig]:
    return [
        GroupConfig(
            name="TEST",
            assets=[AssetConfig(symbol=s, type="equity", source="yahoo") for s in symbols],
        )
    ]


# --- _official_close -------------------------------------------------------


def test_official_close_uses_previous_close_while_session_is_live() -> None:
    now = datetime(2026, 7, 3, 15, 0, tzinfo=UTC)
    quote = yahoo_quote(
        "AAPL", last=210.0, previous_close=208.0, timestamp=now - timedelta(minutes=5)
    )

    assert _official_close(quote, now) == 208.0


def test_official_close_uses_last_print_after_hours() -> None:
    now = datetime(2026, 7, 3, 15, 0, tzinfo=UTC)
    quote = yahoo_quote(
        "AAPL", last=210.0, previous_close=208.0, timestamp=now - timedelta(hours=2)
    )

    assert _official_close(quote, now) == 210.0


def test_official_close_treats_naive_timestamps_as_utc() -> None:
    now = datetime(2026, 7, 3, 15, 0, tzinfo=UTC)
    fresh_naive = (now - timedelta(minutes=5)).replace(tzinfo=None)
    stale_naive = (now - timedelta(hours=2)).replace(tzinfo=None)

    fresh = yahoo_quote("AAPL", last=210.0, previous_close=208.0, timestamp=fresh_naive)
    stale = yahoo_quote("AAPL", last=210.0, previous_close=208.0, timestamp=stale_naive)

    assert _official_close(fresh, now) == 208.0
    assert _official_close(stale, now) == 210.0


def test_official_close_prefers_explicit_provider_value() -> None:
    # Friday ~16:30 ET: the venue quote is a FRESH post-market print, so the
    # heuristic would pick previous_close (Thursday); the explicit Friday
    # regular close must win.
    now = datetime(2026, 7, 3, 21, 30, tzinfo=UTC)
    quote = yahoo_quote(
        "AAPL",
        last=210.5,
        previous_close=208.0,
        timestamp=now - timedelta(minutes=5),
        official_close=209.0,
    )

    assert _official_close(quote, now) == 209.0


def test_official_close_has_no_jump_at_freshness_expiry() -> None:
    # With an explicit close the baseline is identical on both sides of the
    # 3600s freshness window, so 1D% cannot jump when it expires.
    closed_at = datetime(2026, 7, 3, 20, 0, tzinfo=UTC)
    quote = yahoo_quote(
        "AAPL", last=210.5, previous_close=208.0, timestamp=closed_at, official_close=209.0
    )

    just_fresh = _official_close(quote, closed_at + timedelta(seconds=3500))
    just_stale = _official_close(quote, closed_at + timedelta(seconds=3700))

    assert just_fresh == just_stale == 209.0


# --- overlay through _fetch_fresh_quotes ------------------------------------


@pytest.mark.asyncio
async def test_overlay_replaces_price_with_hyperliquid_live_and_keeps_volume(
    tmp_path: Path,
) -> None:
    groups = equity_group("AAPL")
    yahoo = ScriptedQuotes(
        {"AAPL": yahoo_quote("AAPL", last=210.0, previous_close=208.0, volume=1_234_567.0)}
    )
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": yahoo, "hyperliquid": hyperliquid_with(aapl_market(213.5))},
    )

    fresh = await service._fetch_fresh_quotes(groups)

    quote = fresh["AAPL"]
    assert quote.provider == "hyperliquid"
    assert quote.last == 213.5
    # Session live (fresh venue quote) -> baseline is the venue previous close.
    assert quote.previous_close == 208.0
    assert quote.change_abs == pytest.approx(5.5)
    assert quote.change_pct == pytest.approx(2.644231)
    # Official-session share volume survives the overlay.
    assert quote.volume == 1_234_567.0
    assert quote.currency == "USD"


@pytest.mark.asyncio
async def test_overlay_baseline_is_venue_last_after_hours(tmp_path: Path) -> None:
    groups = equity_group("AAPL")
    stale_ts = datetime.now(UTC) - timedelta(hours=2)
    yahoo = ScriptedQuotes(
        {"AAPL": yahoo_quote("AAPL", last=210.0, previous_close=208.0, timestamp=stale_ts)}
    )
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": yahoo, "hyperliquid": hyperliquid_with(aapl_market(213.5))},
    )

    fresh = await service._fetch_fresh_quotes(groups)

    quote = fresh["AAPL"]
    assert quote.provider == "hyperliquid"
    assert quote.last == 213.5
    # Venue closed and no explicit close carried -> heuristic fallback: the
    # final print is the official close baseline.
    assert quote.previous_close == 210.0
    assert quote.change_abs == pytest.approx(3.5)
    assert quote.change_pct == pytest.approx(1.666667)


@pytest.mark.asyncio
async def test_overlay_weekend_baseline_is_friday_regular_close(tmp_path: Path) -> None:
    # Saturday: the venue's last print is Friday's post-market trade, but the
    # baseline must be Friday's REGULAR close carried by the provider — not
    # the post print (heuristic fallback) nor Thursday's previous_close.
    groups = equity_group("AAPL")
    weekend_stale = datetime.now(UTC) - timedelta(hours=40)
    yahoo = ScriptedQuotes(
        {
            "AAPL": yahoo_quote(
                "AAPL",
                last=210.4,  # Friday post-market print
                previous_close=208.0,  # Thursday regular close
                timestamp=weekend_stale,
                official_close=209.5,  # Friday regular close
            )
        }
    )
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": yahoo, "hyperliquid": hyperliquid_with(aapl_market(213.5))},
    )

    fresh = await service._fetch_fresh_quotes(groups)

    quote = fresh["AAPL"]
    assert quote.provider == "hyperliquid"
    assert quote.last == 213.5
    assert quote.previous_close == 209.5
    assert quote.change_abs == pytest.approx(4.0)
    assert quote.change_pct == pytest.approx(1.909308)


@pytest.mark.asyncio
async def test_overlay_friday_evening_baseline_is_friday_close_not_thursday(
    tmp_path: Path,
) -> None:
    # Friday 16:00->post window: the venue quote is a FRESH post-market
    # print, which used to pull Thursday's previous_close in as the baseline.
    groups = equity_group("AAPL")
    fresh_post = datetime.now(UTC) - timedelta(minutes=10)
    yahoo = ScriptedQuotes(
        {
            "AAPL": yahoo_quote(
                "AAPL",
                last=210.4,
                previous_close=208.0,  # Thursday regular close
                timestamp=fresh_post,
                official_close=209.5,  # Friday regular close
            )
        }
    )
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": yahoo, "hyperliquid": hyperliquid_with(aapl_market(213.5))},
    )

    fresh = await service._fetch_fresh_quotes(groups)

    quote = fresh["AAPL"]
    assert quote.previous_close == 209.5
    assert quote.change_pct == pytest.approx(1.909308)


@pytest.mark.asyncio
async def test_overlay_skips_non_usd_listings(tmp_path: Path) -> None:
    groups = equity_group("SMSN")
    original = yahoo_quote("SMSN", last=71000.0, previous_close=70500.0, currency="KRW")
    yahoo = ScriptedQuotes({"SMSN": original})
    hyperliquid = hyperliquid_with({"SMSN": {"coin": "xyz:SMSN", "display": "SMSN", "last": 50.0}})
    service = QuoteService(tmp_path / "board.sqlite3", {"yahoo": yahoo, "hyperliquid": hyperliquid})

    fresh = await service._fetch_fresh_quotes(groups)

    assert fresh["SMSN"] == original


# --- Hyperliquid-first quotes ----------------------------------------------


class RecordingQuotes(ScriptedQuotes):
    def __init__(self, quotes: dict[str, Quote]) -> None:
        super().__init__(quotes)
        self.requested: list[str] = []

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        self.requested.extend(asset.symbol for asset in assets)
        return await super().get_quotes(assets)


def seed_daily_bars(database: Path, closes: list[float], *, ages_days: list[int]) -> None:
    """Daily bars `ages_days` before now (descending age = oldest first)."""
    now = datetime.now(UTC)
    db.save_bars(
        database,
        [
            Bar(
                symbol="AAPL",
                provider="yahoo",
                interval="1d",
                timestamp=now - timedelta(days=age),
                open=close - 0.5,
                high=close + 1,
                low=close - 1,
                close=close,
            )
            for close, age in zip(closes, ages_days, strict=True)
        ],
    )


@pytest.mark.asyncio
async def test_hl_first_serves_covered_equity_without_yahoo(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    # Two completed sessions; the freshest (208.0) is the baseline.
    seed_daily_bars(database, [200.0, 208.0], ages_days=[2, 1])
    yahoo = RecordingQuotes({"AAPL": yahoo_quote("AAPL")})
    service = QuoteService(
        database,
        {"yahoo": yahoo, "hyperliquid": hyperliquid_with(aapl_market(213.5))},
    )

    fresh = await service._fetch_fresh_quotes(equity_group("AAPL"))

    quote = fresh["AAPL"]
    assert quote.provider == "hyperliquid"
    assert quote.last == 213.5
    assert quote.previous_close == 208.0
    assert quote.change_pct == pytest.approx(2.644231)
    assert "AAPL" not in yahoo.requested


@pytest.mark.asyncio
async def test_hl_first_falls_back_to_yahoo_without_baseline(tmp_path: Path) -> None:
    # No cached daily bars: the venue fetch + overlay path must still serve.
    yahoo = RecordingQuotes(
        {"AAPL": yahoo_quote("AAPL", last=210.0, previous_close=208.0, volume=99.0)}
    )
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": yahoo, "hyperliquid": hyperliquid_with(aapl_market(213.5))},
    )

    fresh = await service._fetch_fresh_quotes(equity_group("AAPL"))

    quote = fresh["AAPL"]
    assert quote.provider == "hyperliquid"
    assert quote.last == 213.5
    assert quote.previous_close == 208.0
    assert quote.volume == 99.0
    assert "AAPL" in yahoo.requested


@pytest.mark.asyncio
async def test_hl_first_falls_back_when_stale_baseline(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    seed_daily_bars(database, [200.0, 208.0], ages_days=[8, 7])
    yahoo = RecordingQuotes({"AAPL": yahoo_quote("AAPL")})
    service = QuoteService(
        database,
        {"yahoo": yahoo, "hyperliquid": hyperliquid_with(aapl_market(213.5))},
    )

    fresh = await service._fetch_fresh_quotes(equity_group("AAPL"))

    assert fresh["AAPL"].provider == "hyperliquid"  # overlay on the venue quote
    assert "AAPL" in yahoo.requested


# --- _baseline_close --------------------------------------------------------


def test_baseline_close_uses_previous_session_while_today_is_forming() -> None:
    tail = [
        (datetime(2026, 7, 2, 13, 30, tzinfo=UTC), 208.0),
        (datetime(2026, 7, 3, 13, 30, tzinfo=UTC), 210.0),  # still trading
    ]
    now = datetime(2026, 7, 3, 16, 0, tzinfo=UTC)

    assert _baseline_close(tail, now) == 208.0


def test_baseline_close_uses_last_bar_once_its_session_ends() -> None:
    tail = [
        (datetime(2026, 7, 2, 13, 30, tzinfo=UTC), 208.0),
        (datetime(2026, 7, 3, 13, 30, tzinfo=UTC), 210.0),
    ]
    after_close = datetime(2026, 7, 3, 22, 0, tzinfo=UTC)
    weekend = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)

    assert _baseline_close(tail, after_close) == 210.0
    assert _baseline_close(tail, weekend) == 210.0


def test_baseline_close_rejects_stale_or_thin_tails() -> None:
    now = datetime(2026, 7, 10, 16, 0, tzinfo=UTC)
    stale = [(datetime(2026, 6, 30, 13, 30, tzinfo=UTC), 208.0)]
    forming_only = [(datetime(2026, 7, 10, 13, 30, tzinfo=UTC), 210.0)]

    assert _baseline_close(stale, now) is None
    assert _baseline_close(forming_only, now) is None
    assert _baseline_close([], now) is None


@pytest.mark.asyncio
async def test_overlay_skips_unknown_listing_currency(tmp_path: Path) -> None:
    groups = equity_group("AAPL")
    original = yahoo_quote("AAPL", currency=None)
    yahoo = ScriptedQuotes({"AAPL": original})
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": yahoo, "hyperliquid": hyperliquid_with(aapl_market())},
    )

    fresh = await service._fetch_fresh_quotes(groups)

    assert fresh["AAPL"] == original


@pytest.mark.asyncio
async def test_overlay_skips_error_quotes(tmp_path: Path) -> None:
    groups = equity_group("AAPL")
    broken = yahoo_quote("AAPL", last=210.0, previous_close=208.0, error="upstream_down")
    yahoo = ScriptedQuotes({"AAPL": broken})
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": yahoo, "hyperliquid": hyperliquid_with(aapl_market(213.5))},
    )

    fresh = await service._fetch_fresh_quotes(groups)

    assert fresh["AAPL"] == broken


@pytest.mark.asyncio
async def test_overlay_leaves_symbols_without_hyperliquid_market_untouched(
    tmp_path: Path,
) -> None:
    groups = equity_group("MSFT")
    original = yahoo_quote("MSFT", last=430.0, previous_close=425.0)
    yahoo = ScriptedQuotes({"MSFT": original})
    service = QuoteService(
        tmp_path / "board.sqlite3",
        {"yahoo": yahoo, "hyperliquid": hyperliquid_with(aapl_market())},
    )

    fresh = await service._fetch_fresh_quotes(groups)

    assert fresh["MSFT"] == original


class RecordingHyperliquid(HyperliquidProvider):
    def __init__(
        self,
        crypto: dict[str, dict[str, Any]],
        tradfi: dict[str, dict[str, Any]],
    ) -> None:
        super().__init__()
        self._crypto = crypto
        self._tradfi = tradfi
        self._markets_time = monotonic()  # keep the map warm: no HTTP
        self._crypto_time = self._markets_time if crypto else 0.0
        self._tradfi_time = self._markets_time if tradfi else 0.0
        self.requested_candidates: set[str] | None = None

    async def live_prices(self, symbols: set[str]) -> dict[str, float]:
        self.requested_candidates = set(symbols)
        return await super().live_prices(symbols)


@pytest.mark.asyncio
async def test_overlay_candidates_exclude_hyperliquid_sourced_and_crypto_assets(
    tmp_path: Path,
) -> None:
    groups = [
        GroupConfig(
            name="TEST",
            assets=[
                AssetConfig(symbol="AAPL", type="equity", source="yahoo"),
                AssetConfig(symbol="XLE", type="etf", source="yahoo"),
                AssetConfig(symbol="BTC", type="crypto_perp", source="hyperliquid"),
                AssetConfig(symbol="SYN", type="equity", source="hyperliquid"),
            ],
        )
    ]
    yahoo = ScriptedQuotes(
        {
            "AAPL": yahoo_quote("AAPL"),
            "XLE": yahoo_quote("XLE", last=95.0, previous_close=94.0),
        }
    )
    hyperliquid = RecordingHyperliquid(
        crypto={"BTC": {"coin": "BTC", "display": "BTC", "last": 62000.0}},
        tradfi={
            "AAPL": {"coin": "xyz:AAPL", "display": "AAPL", "last": 213.5},
            "SYN": {"coin": "xyz:SYN", "display": "SYN", "last": 12.0},
        },
    )
    service = QuoteService(tmp_path / "board.sqlite3", {"yahoo": yahoo, "hyperliquid": hyperliquid})

    await service._fetch_fresh_quotes(groups)

    # Only listing-venue equities/ETFs are overlay candidates; assets already
    # sourced from Hyperliquid (crypto perps and synthetic equities) are not.
    assert hyperliquid.requested_candidates == {"AAPL", "XLE"}


@pytest.mark.asyncio
async def test_overlay_skips_crypto_classified_ticker_collisions(
    tmp_path: Path,
) -> None:
    """Hyperliquid's ROBO is a crypto token; the ROBO ETF must keep its venue quote."""
    yahoo = ScriptedQuotes({"ROBO": yahoo_quote("ROBO", last=83.4, previous_close=85.4)})
    hyperliquid = HyperliquidProvider()
    # Listed on the main (crypto) dex only: a ticker collision with the ETF.
    hyperliquid._crypto = {"ROBO": {"coin": "ROBO", "display": "ROBO", "last": 0.014}}
    hyperliquid._markets_time = monotonic()
    hyperliquid._crypto_time = hyperliquid._markets_time
    service = QuoteService(tmp_path / "board.sqlite3", {"yahoo": yahoo, "hyperliquid": hyperliquid})

    quotes = await service._fetch_fresh_quotes(equity_group("ROBO"))

    assert quotes["ROBO"].provider == "yahoo"
    assert quotes["ROBO"].last == 83.4
