import httpx
import pytest

from app.models import AssetConfig
from app.providers import hyperliquid
from app.providers.hyperliquid import (
    HyperliquidProvider,
    _number,
    _parse_market_contexts,
)

_PAYLOAD = [
    {"universe": [{"name": "BTC"}, {"name": "ETH"}, {"name": "SOL"}]},
    [
        {
            "markPx": "50000",
            "midPx": "50010",
            "prevDayPx": "49000",
            "funding": "0.0000125",
            "openInterest": "1000",
        },
        {"markPx": "3000", "prevDayPx": "3100", "funding": "-0.0001"},
        {"midPx": "150", "prevDayPx": "140", "openInterest": "2000"},
    ],
]


def _asset(symbol: str) -> AssetConfig:
    return AssetConfig(symbol=symbol, type="crypto_perp", source="hyperliquid")


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self._payload


def _install_payload(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, url: str, json: object = None) -> _FakeResponse:
            return _FakeResponse(payload)

    monkeypatch.setattr(hyperliquid.httpx, "AsyncClient", _FakeClient)


@pytest.mark.asyncio
async def test_get_quotes_maps_funding_and_open_interest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_payload(monkeypatch, _PAYLOAD)

    quotes = await HyperliquidProvider().get_quotes(
        [_asset("btc"), _asset("ETH"), _asset("SOL"), _asset("DOGE")]
    )

    by_symbol = {quote.symbol: quote for quote in quotes}
    # DOGE is not in the universe and is dropped; symbol case is preserved.
    assert set(by_symbol) == {"btc", "ETH", "SOL"}

    btc = by_symbol["btc"]
    assert btc.last == 50010.0  # midPx preferred over markPx
    assert btc.previous_close == 49000.0
    assert btc.change_pct == 2.061224
    assert btc.funding_rate == 1.25e-05
    assert btc.open_interest_usd == 50_000_000.0  # openInterest x markPx
    assert btc.provider == "hyperliquid"
    assert btc.currency == "USD"

    eth = by_symbol["ETH"]
    assert eth.last == 3000.0  # falls back to markPx without midPx
    assert eth.funding_rate == -0.0001
    assert eth.open_interest_usd is None  # openInterest missing

    sol = by_symbol["SOL"]
    assert sol.last == 150.0
    assert sol.funding_rate is None
    assert sol.open_interest_usd is None  # markPx missing


@pytest.mark.asyncio
async def test_get_quotes_returns_empty_on_http_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_FailingClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, url: str, json: object = None) -> object:
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(hyperliquid.httpx, "AsyncClient", _FailingClient)

    assert await HyperliquidProvider().get_quotes([_asset("BTC")]) == []


def test_parse_market_contexts_pairs_universe_with_contexts() -> None:
    ctx_a = {"markPx": "1"}
    ctx_b = {"markPx": "2"}
    payload = [{"universe": [{"name": "btc"}, {"name": "ETH"}]}, [ctx_a, ctx_b]]

    assert _parse_market_contexts(payload) == {"BTC": ctx_a, "ETH": ctx_b}


def test_parse_market_contexts_rejects_malformed_payloads() -> None:
    good_meta = {"universe": [{"name": "BTC"}]}
    cases = [
        None,
        {},
        "meta",
        [good_meta],
        [good_meta, [{}], "extra"],
        ["not-a-dict", [{}]],
        [good_meta, "not-a-list"],
        [{"universe": "not-a-list"}, [{}]],
    ]
    for payload in cases:
        assert _parse_market_contexts(payload) == {}, repr(payload)


def test_parse_market_contexts_skips_entries_without_names() -> None:
    payload = [
        {"universe": [{"name": "BTC"}, "not-a-dict", {"noname": True}, {"name": "ETH"}]},
        [{"markPx": "1"}, {"markPx": "2"}, {"markPx": "3"}, {"markPx": "4"}],
    ]

    assert _parse_market_contexts(payload) == {
        "BTC": {"markPx": "1"},
        "ETH": {"markPx": "4"},
    }


def test_number_parses_strings_and_rejects_junk() -> None:
    assert _number("3.5") == 3.5
    assert _number(7) == 7.0
    assert _number(None) is None
    assert _number("not-a-number") is None
    assert _number("nan") is None
    assert _number("inf") is None
