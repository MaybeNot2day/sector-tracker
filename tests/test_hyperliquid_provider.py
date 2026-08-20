import json
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import httpx
import pytest

from app.models import AssetConfig
from app.providers import hyperliquid as hl_module
from app.providers.hyperliquid import (
    INFO_URL,
    MARKETS_TTL_SECONDS,
    HyperliquidProvider,
)

BTC_PERP = AssetConfig(symbol="BTC", type="crypto_perp", source="hyperliquid")
AAPL_EQUITY = AssetConfig(symbol="AAPL", type="equity", source="hyperliquid")

MAIN_KEY = "metaAndAssetCtxs"
XYZ_KEY = "metaAndAssetCtxs:xyz"
CANDLE_KEY = "candleSnapshot"


class InfoAPI:
    """MockTransport around the single POST /info endpoint.

    Routes are keyed by request type ("metaAndAssetCtxs", "candleSnapshot"),
    with ":<dex>" appended when the body carries a dex ("metaAndAssetCtxs:xyz").
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[dict[str, Any]] = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == INFO_URL
        body = json.loads(request.content)
        self.requests.append(body)
        key = body["type"]
        if body.get("dex"):
            key += f":{body['dex']}"
        result = self.routes[key]
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result)

    def install(self, provider: HyperliquidProvider) -> None:
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(self._handler))

    def count(self, key: str) -> int:
        def key_of(body: dict[str, Any]) -> str:
            return str(body["type"]) + (f":{body['dex']}" if body.get("dex") else "")

        return sum(1 for body in self.requests if key_of(body) == key)


def main_dex_payload() -> list[Any]:
    """[meta, assetCtxs] for the main (crypto perp) dex, universe[i] <-> ctxs[i]."""
    return [
        {
            "universe": [
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10},
                {"name": "OLD", "szDecimals": 1, "maxLeverage": 3, "isDelisted": True},
                {"name": "HALTED", "szDecimals": 1, "maxLeverage": 3},
                {"name": "REKT", "szDecimals": 1, "maxLeverage": 3},
            ]
        },
        [
            {
                "markPx": "62639.1",
                "prevDayPx": "63717.0",
                "funding": "0.0000125",
                "openInterest": "41900.11",
                "dayNtlVlm": "1635841155.69",
                "dayBaseVlm": "26056.5",
            },
            {
                "markPx": "0.008",
                "prevDayPx": "0.0079",
                "funding": "0.0000521",
                "openInterest": "1000.0",
                "dayNtlVlm": "500000000.0",
                "dayBaseVlm": "9000.0",
            },
            {"markPx": "5.0", "prevDayPx": "4.9"},  # delisted -> must be dropped
            {"markPx": "0", "prevDayPx": "1.0"},  # zero mark -> must be dropped
            {"markPx": "10.0", "prevDayPx": "0"},  # zero prev day -> no baseline
        ],
    ]


def xyz_dex_payload() -> list[Any]:
    """TradFi synthetics dex: names carry the xyz: prefix."""
    return [
        {
            "universe": [
                {"name": "xyz:AAPL", "szDecimals": 2},
                {"name": "xyz:SPY", "szDecimals": 2},
                {"name": "xyz:HALTED", "szDecimals": 2},
            ]
        },
        [
            {"markPx": "212.5", "prevDayPx": "209.87654", "dayNtlVlm": "1000000.0"},
            {"markPx": "744.5", "prevDayPx": "740.0"},
            {"markPx": "0", "prevDayPx": "1.0"},
        ],
    ]


def live_api(
    main: Any | None = None, tradfi: Any | None = None, candles: Any | None = None
) -> tuple[HyperliquidProvider, InfoAPI]:
    api = InfoAPI(
        {
            MAIN_KEY: main_dex_payload() if main is None else main,
            XYZ_KEY: xyz_dex_payload() if tradfi is None else tradfi,
            CANDLE_KEY: [] if candles is None else candles,
        }
    )
    provider = HyperliquidProvider()
    api.install(provider)
    return provider, api


def crypto_record(
    coin: str,
    last: float,
    *,
    day_volume_usd: float | None = None,
    funding: float | None = None,
    change_pct: float | None = None,
    open_interest_usd: float | None = None,
) -> dict[str, Any]:
    return {
        "coin": coin,
        "display": coin,
        "last": last,
        "prev_day": None,
        "change_pct": change_pct,
        "funding": funding,
        "open_interest_usd": open_interest_usd,
        "day_volume_usd": day_volume_usd,
        "day_base_volume": None,
    }


def tradfi_record(symbol: str, last: float) -> dict[str, Any]:
    record = crypto_record(f"xyz:{symbol}", last)
    record["display"] = symbol
    return record


def seeded_provider(
    crypto: dict[str, dict[str, Any]] | None = None,
    tradfi: dict[str, dict[str, Any]] | None = None,
) -> tuple[HyperliquidProvider, InfoAPI]:
    """Provider with a warm market map, so lookups never refresh over HTTP."""
    provider, api = live_api()
    provider._crypto = crypto or {}
    provider._tradfi = tradfi or {}
    provider._markets_time = monotonic()
    provider._crypto_time = provider._markets_time if provider._crypto else 0.0
    provider._tradfi_time = provider._markets_time if provider._tradfi else 0.0
    return provider, api


@pytest.mark.asyncio
async def test_discovery_universe_returns_only_fresh_complete_dex_maps() -> None:
    provider, _ = live_api()

    snapshot = await provider.discovery_universe()

    assert snapshot == {
        "crypto": {"BTC": "BTC", "KPEPE": "kPEPE", "REKT": "REKT"},
        "xyz": {"AAPL": "xyz:AAPL", "SPY": "xyz:SPY"},
    }

    # A stale dex is omitted, not emitted as an empty universe that callers
    # could mistake for a mass delisting.
    provider._markets_time = monotonic()
    provider._crypto_time = 0.0
    snapshot = await provider.discovery_universe()
    assert "crypto" not in snapshot
    assert snapshot["xyz"] == {"AAPL": "xyz:AAPL", "SPY": "xyz:SPY"}


@pytest.mark.asyncio
async def test_get_quotes_maps_perp_ctx_with_funding_oi_and_volume() -> None:
    provider, _ = live_api()

    quotes = await provider.get_quotes([BTC_PERP])

    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.symbol == "BTC"
    assert quote.provider == "hyperliquid"
    assert quote.currency == "USD"
    assert quote.last == 62639.1
    # last = markPx, previous close = prevDayPx, change derived from the two
    assert quote.previous_close == 63717.0
    assert quote.change_abs == pytest.approx(62639.1 - 63717.0, abs=1e-6)
    assert quote.change_pct == pytest.approx((62639.1 - 63717.0) / 63717.0 * 100, abs=1e-6)
    # Funding is the hourly fraction straight from the ctx — no conversion.
    assert quote.funding_rate == pytest.approx(1.25e-05)
    assert quote.open_interest_usd == pytest.approx(41900.11 * 62639.1)
    # Rolling 24h base volume rides the same ctx.
    assert quote.volume == pytest.approx(26056.5)


@pytest.mark.asyncio
async def test_tradfi_quotes_strip_xyz_prefix_and_carry_no_perp_fields() -> None:
    provider, _ = live_api()

    quotes = await provider.get_quotes([AAPL_EQUITY])

    assert len(quotes) == 1
    quote = quotes[0]
    # The xyz: prefix is stripped for lookup and display.
    assert quote.symbol == "AAPL"
    assert quote.last == 212.5
    assert quote.previous_close == pytest.approx(209.87654)
    # Funding, OI, and volume are crypto_perp-only.
    assert quote.funding_rate is None
    assert quote.open_interest_usd is None
    assert quote.volume is None


@pytest.mark.asyncio
async def test_get_quotes_skips_delisted_zero_mark_and_unknown_symbols() -> None:
    provider, _ = live_api()
    assets = [
        AssetConfig(symbol="OLD", type="crypto_perp", source="hyperliquid"),
        AssetConfig(symbol="HALTED", type="crypto_perp", source="hyperliquid"),
        AssetConfig(symbol="NOSUCH", type="crypto_perp", source="hyperliquid"),
        AssetConfig(symbol="HALTED", type="equity", source="hyperliquid"),
    ]

    quotes = await provider.get_quotes(assets)

    assert quotes == []


@pytest.mark.asyncio
async def test_zero_prev_day_yields_no_previous_close() -> None:
    provider, _ = live_api()

    quotes = await provider.get_quotes(
        [AssetConfig(symbol="REKT", type="crypto_perp", source="hyperliquid")]
    )

    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.last == 10.0
    # prevDayPx of 0 would divide by zero; the guard drops the baseline.
    assert quote.previous_close is None
    assert quote.change_pct is None


@pytest.mark.asyncio
async def test_markets_cached_within_ttl() -> None:
    provider, api = live_api()

    first = await provider.get_quotes([AAPL_EQUITY])
    second = await provider.get_quotes([AAPL_EQUITY])

    # One refresh serves both calls; each refresh hits each dex exactly once.
    assert api.count(MAIN_KEY) == 1
    assert api.count(XYZ_KEY) == 1
    assert first[0].last == second[0].last == 212.5


@pytest.mark.asyncio
async def test_empty_caches_fetch_even_when_process_monotonic_is_below_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hl_module, "monotonic", lambda: 1.0)
    provider, api = live_api()

    await provider.get_quotes([BTC_PERP])
    await provider.get_quotes([BTC_PERP])

    # A cold cache (markets_time == 0) must fetch even though monotonic() is
    # below the TTL; the stamped refresh then absorbs the second call.
    assert api.count(MAIN_KEY) == 1
    assert api.count(XYZ_KEY) == 1


@pytest.mark.asyncio
async def test_http_client_is_reused_and_closed() -> None:
    provider, api = live_api()

    await provider.get_quotes([AAPL_EQUITY])
    client = provider._client
    provider._markets_time -= MARKETS_TTL_SECONDS
    await provider.get_quotes([AAPL_EQUITY])

    assert api.count(MAIN_KEY) == 2
    assert provider._client is client  # one client across refreshes
    await provider.aclose()
    assert client is not None and client.is_closed
    assert provider._client is None


@pytest.mark.asyncio
async def test_failed_refresh_keeps_stale_markets() -> None:
    provider, api = live_api()

    await provider.get_quotes([BTC_PERP])
    # Expire the cache, then break the API: quotes must degrade to stale
    # marks instead of vanishing.
    provider._markets_time -= MARKETS_TTL_SECONDS
    api.routes[MAIN_KEY] = httpx.Response(500)
    api.routes[XYZ_KEY] = httpx.Response(500)
    quotes = await provider.get_quotes([BTC_PERP])

    assert api.count(MAIN_KEY) == 2
    assert len(quotes) == 1
    assert quotes[0].last == 62639.1


@pytest.mark.asyncio
async def test_429_triggers_cooldown_that_blocks_further_requests() -> None:
    provider, api = live_api(main=httpx.Response(429), tradfi=httpx.Response(429))

    assert await provider.get_quotes([AAPL_EQUITY]) == []
    assert await provider.get_quotes([AAPL_EQUITY]) == []
    # The second call must be absorbed by the cooldown, not retried.
    assert api.count(MAIN_KEY) == 1
    assert api.count(XYZ_KEY) == 1

    # Once the cooldown lapses, fetching resumes.
    provider._cooldown_until = {}
    api.routes[MAIN_KEY] = main_dex_payload()
    api.routes[XYZ_KEY] = xyz_dex_payload()
    quotes = await provider.get_quotes([AAPL_EQUITY])
    assert len(quotes) == 1
    assert quotes[0].last == 212.5


@pytest.mark.asyncio
async def test_get_history_builds_bars_from_string_candles_and_skips_malformed() -> None:
    candles = [
        # Served newest-first to prove the provider sorts ascending.
        {
            "t": 1748566800000,
            "T": 1748570399999,
            "s": "BTC",
            "i": "1h",
            "o": "1.5",
            "h": "1.8",
            "l": "1.2",
            "c": "1.6",
            "v": "4.0",
            "n": 3,
        },
        {
            "t": 1748563200000,
            "T": 1748566799999,
            "s": "BTC",
            "i": "1h",
            "o": "1.0",
            "h": "2.0",
            "l": "0.5",
            "c": "1.5",
            "v": "10.0",
            "n": 5,
        },
        {"t": 1748570400000, "o": "1.5", "h": "1.6", "l": "1.4"},  # missing close
        "garbage",  # not a dict
        {"t": None, "o": "1.0", "h": "1.0", "l": "1.0", "c": "1.0"},  # bad timestamp
    ]
    provider, api = seeded_provider(crypto={"BTC": crypto_record("BTC", 62000.0)})
    api.routes[CANDLE_KEY] = candles

    bars = await provider.get_history(BTC_PERP, interval="1h", range_="1d")

    assert len(bars) == 2
    bar = bars[0]
    assert bar.symbol == "BTC"
    assert bar.provider == "hyperliquid"
    assert bar.interval == "1h"
    # ms timestamps, string OHLCV
    assert bar.timestamp == datetime(2025, 5, 30, tzinfo=UTC)
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (1.0, 2.0, 0.5, 1.5, 10.0)
    assert bars[1].timestamp == datetime(2025, 5, 30, 1, tzinfo=UTC)
    # The single request targets the exact coin name from the market map.
    assert api.count(CANDLE_KEY) == 1
    req = api.requests[-1]["req"]
    assert req["coin"] == "BTC"
    assert req["interval"] == "1h"
    assert req["startTime"] < req["endTime"]


@pytest.mark.asyncio
async def test_get_history_uses_prefixed_coin_for_tradfi_synthetics() -> None:
    provider, api = seeded_provider(tradfi={"AAPL": tradfi_record("AAPL", 212.5)})

    bars = await provider.get_history(AAPL_EQUITY, interval="1d", range_="1mo")

    assert bars == []
    # Candle requests must use the exact API coin name, prefix included.
    assert api.requests[-1]["req"]["coin"] == "xyz:AAPL"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("1m", "1m"),
        ("5m", "5m"),
        ("15m", "15m"),
        ("30m", "30m"),
        ("1h", "1h"),
        ("4h", "4h"),
        ("12h", "12h"),
        ("1d", "1d"),
        ("1wk", "1w"),  # native weekly — no local aggregation
        ("1mo", "1M"),  # native monthly
        ("90m", "1d"),  # unsupported intervals collapse to daily
    ],
)
async def test_get_history_maps_interval_natively(interval: str, expected: str) -> None:
    provider, api = seeded_provider(crypto={"BTC": crypto_record("BTC", 62000.0)})

    await provider.get_history(BTC_PERP, interval=interval, range_="1d")

    # One candleSnapshot request per history call — no paging.
    assert api.count(CANDLE_KEY) == 1
    assert api.requests[-1]["req"]["interval"] == expected


@pytest.mark.asyncio
async def test_get_history_unknown_symbol_returns_empty_without_fetch() -> None:
    provider, api = seeded_provider(crypto={"BTC": crypto_record("BTC", 62000.0)})

    bars = await provider.get_history(
        AssetConfig(symbol="NOSUCH", type="equity", source="hyperliquid"),
        interval="1h",
        range_="1d",
    )

    assert bars == []
    assert api.requests == []


@pytest.mark.asyncio
async def test_cached_market_helpers_answer_without_http() -> None:
    provider, api = seeded_provider(
        crypto={
            "BTC": crypto_record("BTC", 62000.0),
            # Ticker collision: a token sharing an equity's symbol.
            "ROBO": crypto_record("ROBO", 1.5),
        },
        tradfi={
            "SPY": tradfi_record("SPY", 744.5),
            "ROBO": tradfi_record("ROBO", 30.25),
            "HALTED": tradfi_record("HALTED", 0.0),
        },
    )

    assert await provider.has_market("btc") is True
    assert await provider.has_market("spy") is True
    assert await provider.has_market("NOSUCH") is False
    assert provider.is_crypto_market("robo") is True
    assert provider.is_tradfi_market("robo") is True
    assert provider.is_crypto_market("SPY") is False
    assert provider.is_tradfi_market("BTC") is False

    prices = await provider.live_prices({"btc", "SPY", "ROBO", "HALTED", "NOSUCH"})

    # TradFi synthetics only: BTC (crypto-only) is excluded, the collided
    # ROBO resolves to the xyz mark, and zero marks are dropped.
    assert prices == {"SPY": 744.5, "ROBO": 30.25}
    assert api.requests == []


@pytest.mark.asyncio
async def test_crypto_tape_orders_by_day_volume_and_maps_baskets() -> None:
    provider, api = seeded_provider(
        crypto={
            "BTC": crypto_record(
                "BTC",
                62639.1,
                day_volume_usd=1_635_841_155.69,
                funding=1.25e-05,
                change_pct=-1.69,
                open_interest_usd=2_624_580_179.9,
            ),
            "KPEPE": crypto_record("kPEPE", 0.008, day_volume_usd=500_000_000.0),
            "KZZZ": crypto_record("kZZZ", 2.0, day_volume_usd=1_000_000.0),
            "MYSTERY": crypto_record("MYSTERY", 3.0, day_volume_usd=None),
            "HALTED": crypto_record("HALTED", 0.0, day_volume_usd=9e9),
        }
    )

    tape = provider.crypto_tape_cached()

    # Zero-price markets are excluded; rows sort by day_volume_usd descending
    # with missing volume treated as zero.
    assert [row["symbol"] for row in tape] == ["BTC", "kPEPE", "kZZZ", "MYSTERY"]
    baskets = {row["symbol"]: row["basket"] for row in tape}
    assert baskets["BTC"] == "L1"  # known snapshot symbol -> its basket
    assert baskets["kPEPE"] == "Memes"  # snapshot hit (KPEPE)
    assert baskets["kZZZ"] == "Memes"  # unmapped k-prefixed 1000x wrapper
    assert baskets["MYSTERY"] == "Other"  # unknown symbol
    btc = tape[0]
    assert btc["last"] == 62639.1
    assert btc["funding_rate"] == pytest.approx(1.25e-05)
    assert btc["change_pct"] == pytest.approx(-1.69)
    assert btc["open_interest_usd"] == pytest.approx(2_624_580_179.9)
    assert api.requests == []


@pytest.mark.asyncio
async def test_validate_asset_valid_not_found_unavailable() -> None:
    provider, _ = live_api()
    assert await provider.validate_asset(BTC_PERP) == "valid"
    assert await provider.validate_asset(AAPL_EQUITY) == "valid"
    assert (
        await provider.validate_asset(
            AssetConfig(symbol="NOSUCH", type="equity", source="hyperliquid")
        )
        == "not_found"
    )

    # An upstream outage leaves the caches empty: unavailable, not not-found.
    broken, _ = live_api(main=httpx.Response(500), tradfi=httpx.Response(500))
    assert await broken.validate_asset(BTC_PERP) == "unavailable"


@pytest.mark.asyncio
async def test_stale_dex_maps_never_look_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(hl_module, "monotonic", lambda: clock[0])
    provider, _ = seeded_provider(
        crypto={"BTC": crypto_record("BTC", 62000.0)},
        tradfi={"AAPL": tradfi_record("AAPL", 213.5)},
    )
    provider._crypto_time = clock[0]
    provider._tradfi_time = clock[0]
    provider._markets_time = clock[0]

    clock[0] += hl_module.MAX_QUOTE_AGE_SECONDS + 1
    # Keep the aggregate refresh throttle warm: this isolates the read-side
    # freshness contract from HTTP retry behavior.
    provider._markets_time = clock[0]
    quotes = await provider.get_quotes([BTC_PERP, AAPL_EQUITY])

    assert [quote.is_stale for quote in quotes] == [True, True]
    assert await provider.live_prices({"AAPL"}) == {}
    assert provider.crypto_tape_cached() == []


@pytest.mark.asyncio
async def test_one_dex_success_does_not_refresh_the_other_dex_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(hl_module, "monotonic", lambda: clock[0])
    provider, api = live_api()
    await provider.get_quotes([BTC_PERP, AAPL_EQUITY])
    original_tradfi_time = provider._tradfi_time

    clock[0] += MARKETS_TTL_SECONDS + 1
    api.routes[XYZ_KEY] = httpx.Response(500)
    await provider.get_quotes([BTC_PERP])

    assert provider._crypto_time == clock[0]
    assert provider._tradfi_time == original_tradfi_time


@pytest.mark.asyncio
async def test_candle_failure_cooldown_is_per_symbol() -> None:
    provider, _ = seeded_provider(
        crypto={
            "BTC": crypto_record("BTC", 62000.0),
            "ETH": crypto_record("ETH", 3000.0),
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["type"] == "candleSnapshot" and body["req"]["coin"] == "BTC":
            return httpx.Response(500)
        return httpx.Response(200, json=[])

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    eth = AssetConfig(symbol="ETH", type="crypto_perp", source="hyperliquid")

    assert await provider.get_history(BTC_PERP, interval="1h", range_="1d") == []
    assert "candleSnapshot:BTC" in provider._cooldown_until
    assert await provider.get_history(eth, interval="1h", range_="1d") == []
    assert "candleSnapshot:ETH" not in provider._cooldown_until
