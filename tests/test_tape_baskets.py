import json
from time import monotonic
from typing import Any

import httpx
import pytest

from app.models import AssetConfig
from app.providers.hyperliquid import INFO_URL, HyperliquidProvider, _basket

BTC_PERP = AssetConfig(symbol="BTC", type="crypto_perp", source="hyperliquid")
AAPL_EQUITY = AssetConfig(symbol="AAPL", type="equity", source="hyperliquid")


def forbid_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to build an HTTP client fails the test."""

    class _Boom:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("HTTP client constructed during a cached call")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)


class InfoAPI:
    """Scripted https://api.hyperliquid.xyz/info, keyed on the request dex."""

    def __init__(self, main: list[Any], tradfi: list[Any]) -> None:
        self._payloads = {None: main, "xyz": tradfi}
        self.requests: list[dict[str, Any]] = []

    def install(self, provider: HyperliquidProvider) -> None:
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == INFO_URL
        body = json.loads(request.content)
        assert body["type"] == "metaAndAssetCtxs"
        self.requests.append(body)
        return httpx.Response(200, json=self._payloads[body.get("dex")])


# --- _basket: static category snapshot, MEMES > AI > LAYER_2 > LAYER_1 > DEFI ---


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        pytest.param("DOGE", "Memes", id="memes-tag"),
        pytest.param("TAO", "AI", id="ai-tag"),
        pytest.param("ARB", "L2", id="layer-2-tag"),
        pytest.param("ADA", "L1", id="layer-1-tag"),
        pytest.param("UNI", "DeFi", id="defi-tag"),
        pytest.param("KAITO", "AI", id="ai-beats-defi"),
        pytest.param("LIT", "L2", id="layer-2-beats-defi"),
        pytest.param("HYPE", "L1", id="layer-1-beats-defi"),
        pytest.param("FIL", "AI", id="ai-beats-layer-1"),
        pytest.param("BTC", "L1", id="non-basket-tag-ignored"),
        pytest.param("btc", "L1", id="lookup-case-folded"),
        pytest.param("USDHKD", "Other", id="unmatched-tags-only"),
        pytest.param("kPEPE", "Memes", id="snapshot-meme-wrapper"),
        pytest.param("kNEWMEME", "Memes", id="k-prefix-untagged"),
        pytest.param("1000RATS", "Memes", id="1000-prefix-untagged"),
        pytest.param("ZZZ", "Other", id="unknown-symbol"),
    ],
)
def test_basket(symbol: str, expected: str) -> None:
    assert _basket(symbol) == expected


# --- crypto_tape_cached: basket derived from the snapshot, cache only ---


def test_crypto_tape_rows_carry_baskets_from_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_http(monkeypatch)
    provider = HyperliquidProvider()
    provider._crypto = {
        symbol.upper(): {"coin": symbol, "display": symbol, "last": 1.0}
        for symbol in ["UNI", "ARB", "kFOO", "NEWCOIN"]
    }
    provider._markets_time = monotonic()

    tape = provider.crypto_tape_cached()

    assert {row["symbol"]: row["basket"] for row in tape} == {
        "UNI": "DeFi",
        "ARB": "L2",
        "kFOO": "Memes",  # not in the map; the k-wrapper fallback applies
        "NEWCOIN": "Other",  # not in the map, no fallback
    }


# --- get_quotes: one meta refresh serves the quotes and the tape ---


@pytest.mark.asyncio
async def test_perp_quotes_refresh_the_market_map_for_the_tape() -> None:
    api = InfoAPI(
        main=[
            {"universe": [{"name": "BTC", "szDecimals": 5}]},
            [{"markPx": "62000.0", "prevDayPx": "61000.0", "funding": "0.0000125"}],
        ],
        tradfi=[
            {"universe": [{"name": "xyz:AAPL"}]},
            [{"markPx": "212.5", "prevDayPx": "210.0"}],
        ],
    )
    provider = HyperliquidProvider()
    api.install(provider)

    quotes = await provider.get_quotes([BTC_PERP, AAPL_EQUITY])

    assert [(quote.symbol, quote.last) for quote in quotes] == [("BTC", 62000.0), ("AAPL", 212.5)]
    # One refresh, one POST per dex — no per-symbol or category endpoints.
    assert [body.get("dex") for body in api.requests] == [None, "xyz"]
    # The same refresh feeds the synchronous tape build; TradFi stays out.
    tape = provider.crypto_tape_cached()
    assert [(row["symbol"], row["basket"]) for row in tape] == [("BTC", "L1")]
    assert len(api.requests) == 2


@pytest.mark.asyncio
async def test_quotes_within_the_market_ttl_reuse_the_cached_map() -> None:
    api = InfoAPI(
        main=[
            {"universe": [{"name": "BTC"}]},
            [{"markPx": "62000.0", "prevDayPx": "61000.0"}],
        ],
        tradfi=[{"universe": []}, []],
    )
    provider = HyperliquidProvider()
    api.install(provider)

    first = await provider.get_quotes([BTC_PERP])
    second = await provider.get_quotes([BTC_PERP])

    assert [quote.symbol for quote in first] == [quote.symbol for quote in second] == ["BTC"]
    # The successful fetch restarts the TTL: one refresh serves both calls.
    assert len(api.requests) == 2
