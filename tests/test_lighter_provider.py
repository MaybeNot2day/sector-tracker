from datetime import UTC, datetime
from time import monotonic
from typing import Any

import httpx
import pytest

from app.models import AssetConfig
from app.providers import lighter as lighter_module
from app.providers.lighter import DETAILS_TTL_SECONDS, LighterProvider

BTC_PERP = AssetConfig(symbol="BTC", type="crypto_perp", source="lighter")
AAPL_EQUITY = AssetConfig(symbol="AAPL", type="equity", source="lighter")


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class FakeHTTP:
    """Stands in for httpx.AsyncClient, routing GET paths to scripted payloads."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.constructions = 0
        self.closes = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        class _Client:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                fake.constructions += 1

            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

            async def aclose(self) -> None:
                fake.closes += 1

            async def get(self, url: str, params: dict[str, Any] | None = None) -> FakeResponse:
                path = url.removeprefix(lighter_module.BASE_URL)
                fake.requests.append((path, dict(params or {})))
                result = fake.routes[path]
                if callable(result):
                    # Dynamic routes let pagination tests answer per-request.
                    result = result(dict(params or {}))
                return result if isinstance(result, FakeResponse) else FakeResponse(result)

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

    def count(self, path: str) -> int:
        return sum(1 for requested, _ in self.requests if requested == path)


def details_payload() -> dict[str, Any]:
    return {
        "order_book_details": [
            {
                "symbol": "BTC",
                "market_id": 1,
                "status": "active",
                "last_trade_price": 62000.0,
                "daily_price_change": 0.59,
                "open_interest": 1729.9,
            },
            {
                "symbol": "AAPL",
                "market_id": 42,
                "status": "active",
                "last_trade_price": 212.5,
                "daily_price_change": 1.25,
                "open_interest": 500.0,
            },
            {
                "symbol": "DELISTED",
                "market_id": 7,
                "status": "frozen",
                "last_trade_price": 5.0,
                "daily_price_change": 0.1,
            },
            {
                "symbol": "HALTED",
                "market_id": 8,
                "status": "active",
                "last_trade_price": 0.0,
                "daily_price_change": 0.0,
            },
            {
                "symbol": "REKT",
                "market_id": 9,
                "status": "active",
                "last_trade_price": 10.0,
                "daily_price_change": -100.0,
            },
        ]
    }


def funding_payload() -> dict[str, Any]:
    return {
        "funding_rates": [
            {"market_id": 1, "exchange": "lighter", "symbol": "BTC", "rate": 9.6e-05},
            # Other venues' rates for the same symbol must be ignored.
            {"market_id": 1, "exchange": "hyperliquid", "symbol": "BTC", "rate": 8.0e-04},
        ]
    }


def seeded_provider(details: dict[str, dict[str, Any]]) -> LighterProvider:
    """Provider with a warm details cache, so lookups never hit HTTP."""
    provider = LighterProvider()
    provider._details = details
    provider._details_time = monotonic()
    return provider


@pytest.mark.asyncio
async def test_get_quotes_maps_perp_detail_and_lighter_funding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP({"/orderBookDetails": details_payload(), "/funding-rates": funding_payload()})
    fake.install(monkeypatch)
    provider = LighterProvider()

    quotes = await provider.get_quotes([BTC_PERP])

    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.symbol == "BTC"
    assert quote.provider == "lighter"
    assert quote.currency == "USD"
    assert quote.last == 62000.0
    # previous close is derived from the daily change percentage
    assert quote.previous_close == pytest.approx(61636.34556118898)
    assert quote.change_pct == pytest.approx(0.59)
    assert quote.change_abs == pytest.approx(363.654439, abs=1e-6)
    # 8h-normalized 9.6e-05 becomes an hourly 1.2e-05; the hyperliquid row
    # (8e-04, i.e. 1e-04 hourly) must not leak in.
    assert quote.funding_rate == pytest.approx(1.2e-05)
    assert quote.open_interest_usd == pytest.approx(107_253_800.0)


@pytest.mark.asyncio
async def test_non_perp_assets_get_no_funding_and_skip_funding_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP({"/orderBookDetails": details_payload(), "/funding-rates": funding_payload()})
    fake.install(monkeypatch)
    provider = LighterProvider()

    quotes = await provider.get_quotes([AAPL_EQUITY])

    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.last == 212.5
    assert quote.funding_rate is None
    assert quote.open_interest_usd is None
    # No crypto_perp in the batch -> the funding endpoint is never hit.
    assert fake.count("/funding-rates") == 0


@pytest.mark.asyncio
async def test_get_quotes_skips_inactive_zero_price_and_unknown_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP({"/orderBookDetails": details_payload()})
    fake.install(monkeypatch)
    provider = LighterProvider()
    assets = [
        AssetConfig(symbol="DELISTED", type="equity", source="lighter"),
        AssetConfig(symbol="HALTED", type="equity", source="lighter"),
        AssetConfig(symbol="NOSUCH", type="equity", source="lighter"),
    ]

    quotes = await provider.get_quotes(assets)

    assert quotes == []


@pytest.mark.asyncio
async def test_full_crash_daily_change_yields_no_previous_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP({"/orderBookDetails": details_payload()})
    fake.install(monkeypatch)
    provider = LighterProvider()

    quotes = await provider.get_quotes(
        [AssetConfig(symbol="REKT", type="equity", source="lighter")]
    )

    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.last == 10.0
    # daily_price_change of -100 would divide by zero; the guard drops the baseline.
    assert quote.previous_close is None
    assert quote.change_pct is None


@pytest.mark.asyncio
async def test_details_cached_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeHTTP({"/orderBookDetails": details_payload()})
    fake.install(monkeypatch)
    provider = LighterProvider()

    first = await provider.get_quotes([AAPL_EQUITY])
    second = await provider.get_quotes([AAPL_EQUITY])

    assert fake.count("/orderBookDetails") == 1
    assert first[0].last == second[0].last == 212.5


@pytest.mark.asyncio
async def test_http_client_is_reused_and_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeHTTP({"/orderBookDetails": details_payload()})
    fake.install(monkeypatch)
    provider = LighterProvider()

    await provider.get_quotes([AAPL_EQUITY])
    provider._details_time -= DETAILS_TTL_SECONDS
    await provider.get_quotes([AAPL_EQUITY])

    assert fake.count("/orderBookDetails") == 2
    assert fake.constructions == 1
    await provider.aclose()
    assert fake.closes == 1


@pytest.mark.asyncio
async def test_funding_cache_outlives_details_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP({"/orderBookDetails": details_payload(), "/funding-rates": funding_payload()})
    fake.install(monkeypatch)
    provider = LighterProvider()

    await provider.get_quotes([BTC_PERP])
    # Expire only the details cache; funding (300s TTL) stays warm.
    provider._details_time -= DETAILS_TTL_SECONDS
    quotes = await provider.get_quotes([BTC_PERP])

    assert fake.count("/orderBookDetails") == 2
    assert fake.count("/funding-rates") == 1
    assert quotes[0].funding_rate == pytest.approx(1.2e-05)


@pytest.mark.asyncio
async def test_empty_successful_funding_response_advances_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP(
        {"/orderBookDetails": details_payload(), "/funding-rates": {"funding_rates": []}}
    )
    fake.install(monkeypatch)
    provider = LighterProvider()

    await provider.get_quotes([BTC_PERP])
    await provider.get_quotes([BTC_PERP])

    # A successful-but-empty response is a fresh answer: the TTL absorbs the
    # second poll instead of burning another request on the 60/min budget.
    assert fake.count("/funding-rates") == 1


@pytest.mark.asyncio
async def test_failed_funding_fetch_stamps_no_ttl_and_retries_after_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP(
        {
            "/orderBookDetails": details_payload(),
            "/funding-rates": FakeResponse({}, status_code=500),
        }
    )
    fake.install(monkeypatch)
    provider = LighterProvider()

    await provider.get_quotes([BTC_PERP])
    # Failures rely on the endpoint cooldown alone; once it lapses the next
    # poll fetches again and lands the rates.
    provider._cooldown_until = {}
    fake.routes["/funding-rates"] = funding_payload()
    quotes = await provider.get_quotes([BTC_PERP])

    assert fake.count("/funding-rates") == 2
    assert quotes[0].funding_rate == pytest.approx(1.2e-05)


@pytest.mark.asyncio
async def test_429_triggers_cooldown_that_blocks_further_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP({"/orderBookDetails": FakeResponse({}, status_code=429)})
    fake.install(monkeypatch)
    provider = LighterProvider()

    assert await provider.get_quotes([AAPL_EQUITY]) == []
    assert await provider.get_quotes([AAPL_EQUITY]) == []
    # The second call must be absorbed by the cooldown, not retried.
    assert fake.count("/orderBookDetails") == 1

    # Once the cooldown lapses, fetching resumes.
    provider._cooldown_until = {}
    fake.routes["/orderBookDetails"] = details_payload()
    quotes = await provider.get_quotes([AAPL_EQUITY])
    assert len(quotes) == 1
    assert quotes[0].last == 212.5


@pytest.mark.asyncio
async def test_get_history_builds_bars_and_skips_malformed_candles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candles = {
        "c": [
            {"t": 1748563200000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0},
            {"t": 1748566800000, "o": 1.5, "h": 1.6, "l": 1.4},  # missing close
            "garbage",  # not a dict
            {"t": None, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0},  # bad timestamp
        ]
    }
    fake = FakeHTTP({"/candles": candles})
    fake.install(monkeypatch)
    provider = seeded_provider({"BTC": {"symbol": "BTC", "market_id": 1, "status": "active"}})

    bars = await provider.get_history(BTC_PERP, interval="1h", range_="1d")

    assert len(bars) == 1
    bar = bars[0]
    assert bar.symbol == "BTC"
    assert bar.provider == "lighter"
    assert bar.interval == "1h"
    assert bar.timestamp == datetime(2025, 5, 30, tzinfo=UTC)
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (1.0, 2.0, 0.5, 1.5, 10.0)
    # The request targets the cached market id.
    path, params = fake.requests[-1]
    assert path == "/candles"
    assert params["market_id"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interval", "expected_resolution"),
    [
        ("1m", "1m"),
        ("5m", "5m"),
        ("15m", "15m"),
        ("30m", "30m"),
        ("1h", "1h"),
        ("4h", "4h"),
        ("12h", "12h"),
        ("1d", "1d"),
        ("1wk", "1d"),  # unsupported resolutions collapse to daily
        ("90m", "1d"),
    ],
)
async def test_get_history_maps_interval_to_supported_resolution(
    monkeypatch: pytest.MonkeyPatch, interval: str, expected_resolution: str
) -> None:
    fake = FakeHTTP({"/candles": {"c": []}})
    fake.install(monkeypatch)
    provider = seeded_provider({"BTC": {"symbol": "BTC", "market_id": 1, "status": "active"}})

    await provider.get_history(BTC_PERP, interval=interval, range_="1d")

    _, params = fake.requests[-1]
    assert params["resolution"] == expected_resolution


@pytest.mark.asyncio
async def test_get_history_pages_backwards_past_the_500_candle_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1200 one-minute candles ending "now": one call caps at MAX_CANDLES, so
    # the provider must walk end_timestamp backwards until the short last page.
    step_ms = 60_000
    newest_ms = int(datetime.now(UTC).timestamp() * 1000) // step_ms * step_ms
    history = [newest_ms - i * step_ms for i in range(1200)][::-1]

    def candles(params: dict[str, Any]) -> dict[str, Any]:
        window = [t for t in history if params["start_timestamp"] <= t <= params["end_timestamp"]]
        page = window[-lighter_module.MAX_CANDLES :]  # newest-N, served ascending
        return {"c": [{"t": t, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1.0} for t in page]}

    fake = FakeHTTP({"/candles": candles})
    fake.install(monkeypatch)
    provider = seeded_provider({"BTC": {"symbol": "BTC", "market_id": 1, "status": "active"}})

    bars = await provider.get_history(BTC_PERP, interval="1m", range_="1d")

    assert fake.count("/candles") == 3  # 500 + 500 + 200
    assert len(bars) == 1200
    timestamps = [bar.timestamp for bar in bars]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == 1200  # boundary candles deduplicated
    assert timestamps[-1] == datetime.fromtimestamp(newest_ms / 1000, tz=UTC)


@pytest.mark.asyncio
async def test_get_history_failed_later_page_keeps_earlier_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    step_ms = 60_000
    newest_ms = int(datetime.now(UTC).timestamp() * 1000) // step_ms * step_ms

    def candles(params: dict[str, Any]) -> dict[str, Any]:
        if params["end_timestamp"] < newest_ms:
            raise RuntimeError("second page 500s")
        page = [newest_ms - i * step_ms for i in range(lighter_module.MAX_CANDLES)][::-1]
        return {"c": [{"t": t, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1.0} for t in page]}

    fake = FakeHTTP({"/candles": candles})
    fake.install(monkeypatch)
    provider = seeded_provider({"BTC": {"symbol": "BTC", "market_id": 1, "status": "active"}})

    bars = await provider.get_history(BTC_PERP, interval="1m", range_="1d")

    # The failed second page must degrade to the data already fetched,
    # not raise or discard the first full page.
    assert fake.count("/candles") == 2
    assert len(bars) == lighter_module.MAX_CANDLES

@pytest.mark.asyncio
async def test_get_history_unknown_symbol_returns_empty_without_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP({})
    fake.install(monkeypatch)
    provider = seeded_provider({"BTC": {"symbol": "BTC", "market_id": 1, "status": "active"}})

    bars = await provider.get_history(
        AssetConfig(symbol="NOSUCH", type="equity", source="lighter"),
        interval="1h",
        range_="1d",
    )

    assert bars == []
    assert fake.requests == []


@pytest.mark.asyncio
async def test_cached_detail_helpers_answer_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP({})
    fake.install(monkeypatch)
    provider = seeded_provider(
        {
            "BTC": {
                "symbol": "BTC",
                "market_id": 1,
                "status": "active",
                "strategy_index": 2,
                "last_trade_price": 62000.0,
            },
            "SPY": {
                "symbol": "SPY",
                "market_id": 128,
                "status": "active",
                "strategy_index": 5,
                "last_trade_price": 744.5,
            },
            "HALTED": {
                "symbol": "HALTED",
                "market_id": 8,
                "status": "active",
                "strategy_index": 5,
                "last_trade_price": 0.0,
            },
        }
    )

    assert await provider.has_market("btc") is True
    assert await provider.has_market("NOSUCH") is False
    assert await provider.market_id("btc") == 1
    assert await provider.market_id("NOSUCH") is None
    prices = await provider.live_prices({"btc", "SPY", "HALTED", "NOSUCH"})

    # TradFi synthetics only: crypto-classified markets (ticker collisions
    # like Lighter's ROBO token) never feed the equity overlay.
    assert prices == {"SPY": 744.5}
    assert fake.requests == []
