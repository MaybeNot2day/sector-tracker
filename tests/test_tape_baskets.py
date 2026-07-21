from time import monotonic
from typing import Any

import httpx
import pytest

from app.models import AssetConfig
from app.providers import lighter as lighter_module
from app.providers.lighter import (
    CATEGORY_TTL_SECONDS,
    LighterProvider,
    _basket,
    _parse_categories,
)

BTC_PERP = AssetConfig(symbol="BTC", type="crypto_perp", source="lighter")
AAPL_EQUITY = AssetConfig(symbol="AAPL", type="equity", source="lighter")


def forbid_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to build an HTTP client fails the test."""

    class _Boom:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("HTTP client constructed during a cached call")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)


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

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        class _Client:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

            async def get(self, url: str, params: dict[str, Any] | None = None) -> FakeResponse:
                path = url.removeprefix(lighter_module.BASE_URL)
                fake.requests.append((path, dict(params or {})))
                result = fake.routes[path]
                return result if isinstance(result, FakeResponse) else FakeResponse(result)

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

    def count(self, path: str) -> int:
        return sum(1 for requested, _ in self.requests if requested == path)


# --- _parse_categories: /tokenlist payload -> {SYMBOL: [TAG, ...]} ---


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            {
                "tokens": [
                    {"symbol": "lit", "asset_type": "CRYPTO", "categories": ["defi", "Layer_2"]},
                    {"symbol": "PEPE", "asset_type": "CRYPTO", "categories": ["MEMES"]},
                ]
            },
            {"LIT": ["DEFI", "LAYER_2"], "PEPE": ["MEMES"]},
            id="uppercases-symbols-and-tags",
        ),
        pytest.param(
            {
                "tokens": [
                    {"symbol": "AAPL", "asset_type": "STOCK", "categories": ["DEFI"]},
                    {"symbol": "XAU", "categories": ["DEFI"]},
                    {"symbol": "BTC", "asset_type": "CRYPTO", "categories": ["MAJOR"]},
                ]
            },
            {"BTC": ["MAJOR"]},
            id="keeps-only-crypto-asset-type",
        ),
        pytest.param(
            {"tokens": [{"symbol": "UNI", "asset_type": "CRYPTO"}]},
            {},
            id="missing-categories-skips-token",
        ),
        pytest.param(
            {"tokens": [{"symbol": "UNI", "asset_type": "CRYPTO", "categories": "DEFI"}]},
            {},
            id="non-list-categories-skips-token",
        ),
        pytest.param(
            {"tokens": [{"symbol": "WIF", "asset_type": "CRYPTO", "categories": []}]},
            {"WIF": []},
            id="empty-categories-kept-as-empty-list",
        ),
        pytest.param(
            {
                "tokens": [
                    "not-a-dict",
                    42,
                    {"symbol": "", "asset_type": "CRYPTO", "categories": ["DEFI"]},
                    {"symbol": "OK", "asset_type": "CRYPTO", "categories": ["AI"]},
                ]
            },
            {"OK": ["AI"]},
            id="skips-non-dict-entries-and-empty-symbols",
        ),
        pytest.param(None, {}, id="none-payload"),
        pytest.param(["tokens"], {}, id="non-dict-payload"),
        pytest.param({}, {}, id="missing-tokens-key"),
        pytest.param({"tokens": "nope"}, {}, id="non-list-tokens"),
    ],
)
def test_parse_categories(payload: Any, expected: dict[str, list[str]]) -> None:
    assert _parse_categories(payload) == expected


# --- _basket: tag priority MEMES > AI > LAYER_2 > LAYER_1 > DEFI ---


@pytest.mark.parametrize(
    ("symbol", "categories", "expected"),
    [
        pytest.param("PEPE", ["MEMES"], "Memes", id="memes-tag"),
        pytest.param("TAO", ["AI"], "AI", id="ai-tag"),
        pytest.param("ARB", ["LAYER_2"], "L2", id="layer-2-tag"),
        pytest.param("SOL", ["LAYER_1"], "L1", id="layer-1-tag"),
        pytest.param("UNI", ["DEFI"], "DeFi", id="defi-tag"),
        pytest.param("CHIP", ["DEFI", "AI"], "AI", id="ai-beats-defi"),
        pytest.param("OP", ["DEFI", "LAYER_2"], "L2", id="layer-2-beats-defi"),
        pytest.param("ETH", ["LAYER_1", "MAJOR"], "L1", id="non-basket-tag-ignored"),
        pytest.param("DOGE", ["MEMES", "AI", "LAYER_1"], "Memes", id="memes-beats-everything"),
        pytest.param("BTC", ["MAJOR"], "Other", id="unmatched-tags-only"),
        pytest.param("WIF", [], "Other", id="empty-tags"),
        pytest.param("NEW", None, "Other", id="no-tags"),
        pytest.param("1000PEPE", None, "Memes", id="1000-prefix-untagged"),
        pytest.param("1000BONK", [], "Memes", id="1000-prefix-empty-tags"),
        pytest.param("1000FLOKI", ["MAJOR"], "Memes", id="1000-prefix-unmatched-tags"),
        pytest.param("1000SHIB", ["DEFI"], "DeFi", id="1000-prefix-loses-to-real-tag"),
    ],
)
def test_basket(symbol: str, categories: list[str] | None, expected: str) -> None:
    assert _basket(symbol, categories) == expected


# --- crypto_tape_cached: basket derived from the categories cache ---


def test_crypto_tape_rows_carry_baskets_from_categories_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_http(monkeypatch)
    provider = LighterProvider()
    provider._details = {
        symbol: {
            "symbol": symbol,
            "strategy_index": 2,
            "status": "active",
            "market_id": market_id,
            "last_trade_price": 1.0,
        }
        for market_id, symbol in enumerate(["UNI", "ARB", "1000PEPE", "WIF"])
    }
    provider._details_time = monotonic()
    provider._categories = {"UNI": ["DEFI"], "ARB": ["LAYER_2", "DEFI"]}
    provider._categories_time = monotonic()

    tape = provider.crypto_tape_cached()

    assert {row["symbol"]: row["basket"] for row in tape} == {
        "UNI": "DeFi",
        "ARB": "L2",
        "1000PEPE": "Memes",  # not in the map; the 1000-wrapper fallback applies
        "WIF": "Other",  # not in the map, no fallback
    }


# --- _get_categories: 3600s TTL, sticky cache on failed fetch ---


@pytest.mark.asyncio
async def test_categories_served_from_warm_cache_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_http(monkeypatch)
    provider = LighterProvider()
    provider._categories = {"UNI": ["DEFI"]}
    provider._categories_time = monotonic()

    assert await provider._get_categories() == {"UNI": ["DEFI"]}


@pytest.mark.asyncio
async def test_expired_categories_refetch_once_then_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP(
        {
            "/tokenlist": {
                "tokens": [{"symbol": "uni", "asset_type": "CRYPTO", "categories": ["defi"]}]
            }
        }
    )
    fake.install(monkeypatch)
    provider = LighterProvider()
    provider._categories = {"STALE": ["AI"]}
    provider._categories_time = monotonic() - CATEGORY_TTL_SECONDS - 1

    first = await provider._get_categories()
    second = await provider._get_categories()

    assert first == second == {"UNI": ["DEFI"]}
    # The successful fetch restarts the TTL: exactly one request serves both calls.
    assert fake.count("/tokenlist") == 1


@pytest.mark.asyncio
async def test_failed_categories_fetch_keeps_old_cache_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP({"/tokenlist": FakeResponse(None, status_code=500)})
    fake.install(monkeypatch)
    provider = LighterProvider()
    provider._categories = {"OLD": ["DEFI"]}
    provider._categories_time = monotonic() - CATEGORY_TTL_SECONDS - 1

    first = await provider._get_categories()
    second = await provider._get_categories()

    assert first == second == {"OLD": ["DEFI"]}
    # The failure cooldown suppresses an immediate hammer-retry: one request
    # serves both calls while the endpoint is down...
    assert fake.count("/tokenlist") == 1
    # ...but a failed fetch must not restart the FULL TTL: once the short
    # cooldown lapses, the next call retries instead of waiting an hour.
    provider._cooldown_until["/tokenlist"] = 0.0
    third = await provider._get_categories()
    assert third == {"OLD": ["DEFI"]}
    assert fake.count("/tokenlist") == 2


# --- get_quotes: perp batches keep the basket tags warm ---


@pytest.mark.asyncio
async def test_perp_quotes_refresh_categories_for_the_tape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP(
        {
            "/orderBookDetails": {
                "order_book_details": [
                    {
                        "symbol": "BTC",
                        "strategy_index": 2,
                        "status": "active",
                        "market_id": 1,
                        "last_trade_price": 62000.0,
                        "daily_price_change": 0.59,
                    }
                ]
            },
            "/funding-rates": {
                "funding_rates": [
                    {"market_id": 1, "exchange": "lighter", "symbol": "BTC", "rate": 9.6e-05}
                ]
            },
            "/tokenlist": {
                "tokens": [{"symbol": "BTC", "asset_type": "CRYPTO", "categories": ["LAYER_1"]}]
            },
        }
    )
    fake.install(monkeypatch)
    provider = LighterProvider()

    quotes = await provider.get_quotes([BTC_PERP])

    assert [quote.symbol for quote in quotes] == ["BTC"]
    assert fake.count("/orderBookDetails") == 1
    assert fake.count("/funding-rates") == 1
    assert fake.count("/tokenlist") == 1
    # The fetched tags feed the synchronous tape build.
    tape = provider.crypto_tape_cached()
    assert [(row["symbol"], row["basket"]) for row in tape] == [("BTC", "L1")]


@pytest.mark.asyncio
async def test_non_perp_quotes_skip_the_categories_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP(
        {
            "/orderBookDetails": {
                "order_book_details": [
                    {
                        "symbol": "AAPL",
                        "strategy_index": 5,
                        "status": "active",
                        "market_id": 6,
                        "last_trade_price": 212.5,
                    }
                ]
            }
        }
    )
    fake.install(monkeypatch)
    provider = LighterProvider()

    quotes = await provider.get_quotes([AAPL_EQUITY])

    assert [quote.symbol for quote in quotes] == ["AAPL"]
    assert fake.count("/tokenlist") == 0
