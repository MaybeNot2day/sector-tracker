from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import pytest

from app import db
from app.models import AssetConfig, Bar, GroupConfig, Quote
from app.providers.base import QuoteProvider
from app.providers.hyperliquid import HyperliquidProvider
from app.services import quotes as quotes_module
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


def seed_daily_bars(
    database: Path, bars: list[tuple[datetime, float]], *, fetched_at: datetime
) -> None:
    """Yahoo daily bars for AAPL, persisted as one fetch at `fetched_at`."""
    db.save_bars(
        database,
        [
            Bar(
                symbol="AAPL",
                provider="yahoo",
                interval="1d",
                timestamp=timestamp,
                open=close - 0.5,
                high=close + 1,
                low=close - 1,
                close=close,
            )
            for timestamp, close in bars
        ],
        fetched_at=fetched_at,
    )


# US DST ends Sun 2026-11-01: the regular close moves from 20:00 to 21:00 UTC.
FRI_OCT_30 = datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
MON_NOV_2 = datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
TUE_NOV_3 = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_hl_first_serves_covered_equity_without_yahoo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "board.sqlite3"
    # Tuesday morning: Monday's session is the last completed one.
    monkeypatch.setattr(quotes_module, "_utc_now", lambda: datetime(2026, 11, 3, 15, tzinfo=UTC))
    seed_daily_bars(
        database,
        [(FRI_OCT_30, 200.0), (MON_NOV_2, 208.0)],
        fetched_at=datetime(2026, 11, 2, 22, tzinfo=UTC),
    )
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
async def test_hl_first_falls_back_when_completed_session_bar_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Wednesday with Monday as the newest bar: Tuesday's close is unknown, so
    # Monday's close would turn 1D into a two-session change.
    database = tmp_path / "board.sqlite3"
    monkeypatch.setattr(quotes_module, "_utc_now", lambda: datetime(2026, 11, 4, 15, tzinfo=UTC))
    seed_daily_bars(
        database,
        [(FRI_OCT_30, 200.0), (MON_NOV_2, 208.0)],
        fetched_at=datetime(2026, 11, 2, 22, tzinfo=UTC),
    )
    yahoo = RecordingQuotes({"AAPL": yahoo_quote("AAPL")})
    service = QuoteService(
        database,
        {"yahoo": yahoo, "hyperliquid": hyperliquid_with(aapl_market(213.5))},
    )

    fresh = await service._fetch_fresh_quotes(equity_group("AAPL"))

    assert fresh["AAPL"].provider == "hyperliquid"  # overlay on the venue quote
    assert fresh["AAPL"].previous_close == 208.0  # the venue's own value
    assert "AAPL" in yahoo.requested


# --- _baseline_close --------------------------------------------------------


def test_baseline_close_uses_previous_session_while_today_is_forming() -> None:
    tail = [(MON_NOV_2, 208.0), (TUE_NOV_3, 210.0)]  # Tuesday still trading
    now = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)

    assert _baseline_close(tail, now, now) == 208.0


def test_baseline_close_counts_first_hour_after_close_after_dst_ends() -> None:
    # Mon 2026-11-02 21:15 UTC is 16:15 EST: the session closed at 21:00 UTC
    # and the bar was refreshed after it, so it is Monday's official close.
    tail = [(FRI_OCT_30, 200.0), (MON_NOV_2, 208.0)]
    now = datetime(2026, 11, 2, 21, 15, tzinfo=UTC)

    assert _baseline_close(tail, now, datetime(2026, 11, 2, 21, 10, tzinfo=UTC)) == 208.0


def test_baseline_close_rejects_bar_fetched_before_the_close() -> None:
    # Same instant, but the newest bar was last fetched mid-session (20:30 UTC
    # = 15:30 EST): its close is an intraday print, not the official close.
    tail = [(FRI_OCT_30, 200.0), (MON_NOV_2, 208.0)]
    now = datetime(2026, 11, 2, 21, 15, tzinfo=UTC)

    assert _baseline_close(tail, now, datetime(2026, 11, 2, 20, 30, tzinfo=UTC)) is None


def test_baseline_close_uses_session_close_during_dst() -> None:
    # Fri 2026-10-30 20:15 UTC is 16:15 EDT: Friday's session is complete.
    tail = [(datetime(2026, 10, 29, 13, 30, tzinfo=UTC), 199.0), (FRI_OCT_30, 200.0)]
    now = datetime(2026, 10, 30, 20, 15, tzinfo=UTC)
    fetched = datetime(2026, 10, 30, 20, 5, tzinfo=UTC)

    assert _baseline_close(tail, now, fetched) == 200.0
    # Over the weekend Friday stays the last completed session.
    assert _baseline_close(tail, datetime(2026, 10, 31, 12, tzinfo=UTC), fetched) == 200.0


def test_baseline_close_rejects_stale_or_thin_tails() -> None:
    wednesday = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
    stale = [(FRI_OCT_30, 200.0), (MON_NOV_2, 208.0)]
    forming_only = [(datetime(2026, 11, 4, 14, 30, tzinfo=UTC), 210.0)]

    assert _baseline_close(stale, wednesday, wednesday) is None
    assert _baseline_close(forming_only, wednesday, wednesday) is None
    assert _baseline_close([], wednesday, wednesday) is None


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
