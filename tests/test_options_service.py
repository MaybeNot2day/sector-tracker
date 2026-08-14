from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import AssetConfig, GroupConfig
from app.services.options import (
    MarketDataOptionsService,
    OptionsDataError,
    build_options_snapshot,
)


def _contract(
    strike: float,
    option_type: str,
    open_interest: int,
    gamma: float,
    iv: float,
) -> dict[str, object]:
    return {
        "strike": strike,
        "option_type": option_type,
        "open_interest": open_interest,
        "contract_size": 100,
        "greeks": {"gamma": gamma, "mid_iv": iv},
    }


def _chain() -> list[dict[str, object]]:
    return [
        _contract(95, "call", 10, 0.01, 0.24),
        _contract(95, "put", 400, 0.01, 0.25),
        _contract(100, "call", 200, 0.02, 0.20),
        _contract(100, "put", 150, 0.02, 0.22),
        _contract(105, "call", 500, 0.01, 0.23),
        _contract(105, "put", 10, 0.01, 0.24),
    ]


def test_snapshot_calculates_positioning_metrics_and_signed_strike_gex() -> None:
    payload = build_options_snapshot(
        "SPY",
        "2099-01-17",
        ["2099-01-17", "2099-01-24"],
        100.0,
        cast(list[dict[str, Any]], _chain()),
        source="marketdata",
    )

    metrics = cast(dict[str, object], payload["metrics"])
    strikes = cast(list[dict[str, object]], payload["strikes"])
    by_strike = {float(cast(float | int, row["strike"])): row for row in strikes}

    assert metrics == {
        "atm_iv": 0.21,
        "put_call_oi": 0.7887,
        "net_gex": 20_000.0,
        "call_wall": 105.0,
        "put_wall": 95.0,
        "max_pain": 100.0,
        "call_oi": 710,
        "put_oi": 560,
    }
    assert by_strike[95.0]["net_gex"] == -39_000.0
    assert by_strike[100.0]["net_gex"] == 10_000.0
    assert by_strike[105.0]["net_gex"] == 49_000.0
    assert payload["methodology"] == "dealer_gamma_proxy"


@pytest.mark.asyncio
async def test_marketdata_service_selects_expiration_and_caches_each_snapshot() -> None:
    calls: Counter[str] = Counter()
    chain_expirations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls[request.url.path] += 1
        assert request.headers["Authorization"] == "Bearer token"
        if "/expirations/" in request.url.path:
            return httpx.Response(
                200,
                json={"s": "ok", "expirations": ["2099-01-17", "2099-01-24"]},
            )
        if "/chain/" in request.url.path:
            chain_expirations.append(request.url.params["expiration"])
            contracts = _chain()
            return httpx.Response(
                200,
                json={
                    "s": "ok",
                    "side": [contract["option_type"] for contract in contracts],
                    "strike": [contract["strike"] for contract in contracts],
                    "openInterest": [contract["open_interest"] for contract in contracts],
                    "gamma": [
                        cast(dict[str, object], contract["greeks"])["gamma"]
                        for contract in contracts
                    ],
                    "iv": [
                        cast(dict[str, object], contract["greeks"])["mid_iv"]
                        for contract in contracts
                    ],
                    "underlyingPrice": [100.0] * len(contracts),
                    "updated": [4_071_398_400] * len(contracts),
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = MarketDataOptionsService(
        "token",
        base_url="https://marketdata.test",
        cache_seconds=60,
        client=client,
    )

    first = await service.get_snapshot("spy")
    cached = await service.get_snapshot("SPY")
    second_expiration = await service.get_snapshot("SPY", "2099-01-24")
    await service.aclose()

    assert first == cached
    assert first["source"] == "marketdata"
    assert first["expiration"] == "2099-01-17"
    assert first["updated_at"] == "2099-01-06T16:00:00+00:00"
    assert second_expiration["expiration"] == "2099-01-24"
    assert chain_expirations == ["2099-01-17", "2099-01-24"]
    assert calls == {
        "/v1/options/expirations/SPY/": 1,
        "/v1/options/chain/SPY/": 2,
    }


@pytest.mark.asyncio
async def test_different_symbols_fetch_options_concurrently() -> None:
    chain_symbols: set[str] = set()
    both_chains_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/expirations/" in request.url.path:
            return httpx.Response(200, json={"s": "ok", "expirations": ["2099-01-17"]})
        symbol = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        chain_symbols.add(symbol)
        if len(chain_symbols) == 2:
            both_chains_started.set()
        await asyncio.wait_for(both_chains_started.wait(), timeout=0.5)
        contracts = _chain()
        return httpx.Response(
            200,
            json={
                "s": "ok",
                "side": [contract["option_type"] for contract in contracts],
                "strike": [contract["strike"] for contract in contracts],
                "openInterest": [contract["open_interest"] for contract in contracts],
                "gamma": [
                    cast(dict[str, object], contract["greeks"])["gamma"] for contract in contracts
                ],
                "iv": [
                    cast(dict[str, object], contract["greeks"])["mid_iv"] for contract in contracts
                ],
                "underlyingPrice": [100.0] * len(contracts),
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = MarketDataOptionsService("token", client=client)

    snapshots = await asyncio.gather(
        service.get_snapshot("SPY"),
        service.get_snapshot("QQQ"),
    )
    await service.aclose()

    assert chain_symbols == {"SPY", "QQQ"}
    assert {str(snapshot["symbol"]) for snapshot in snapshots} == {"SPY", "QQQ"}


@pytest.mark.asyncio
async def test_options_caches_and_symbol_locks_remain_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/expirations/" in request.url.path:
            return httpx.Response(200, json={"s": "ok", "expirations": ["2099-01-17"]})
        contracts = _chain()
        return httpx.Response(
            200,
            json={
                "s": "ok",
                "side": [contract["option_type"] for contract in contracts],
                "strike": [contract["strike"] for contract in contracts],
                "openInterest": [contract["open_interest"] for contract in contracts],
                "gamma": [
                    cast(dict[str, object], contract["greeks"])["gamma"] for contract in contracts
                ],
                "iv": [
                    cast(dict[str, object], contract["greeks"])["mid_iv"] for contract in contracts
                ],
                "underlyingPrice": [100.0] * len(contracts),
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = MarketDataOptionsService("token", client=client)

    for index in range(65):
        await service.get_snapshot(f"SYM{index:02d}")
    await service.aclose()

    assert len(service._expirations_cache) == 32
    assert len(service._snapshot_cache) == 64
    assert len(service._symbol_locks) <= 64


@pytest.mark.asyncio
async def test_chain_failure_cooldown_skips_the_second_http_attempt() -> None:
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        calls[request.url.path] += 1
        if "/expirations/" in request.url.path:
            return httpx.Response(200, json={"s": "ok", "expirations": ["2099-01-17"]})
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = MarketDataOptionsService("token", client=client)

    with pytest.raises(OptionsDataError, match="marketdata_request_failed"):
        await service.get_snapshot("SPY")
    with pytest.raises(OptionsDataError, match="marketdata_request_failed"):
        await service.get_snapshot("SPY")
    await service.aclose()

    assert calls["/v1/options/expirations/SPY/"] == 1
    assert calls["/v1/options/chain/SPY/"] == 1


@pytest.mark.asyncio
async def test_service_fails_explicitly_without_marketdata_token() -> None:
    service = MarketDataOptionsService("")

    with pytest.raises(OptionsDataError) as error:
        await service.get_snapshot("SPY")

    assert error.value.code == "options_not_configured"
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_marketdata_service_maps_rejected_token_to_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"s": "error", "errmsg": "Unauthorized"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = MarketDataOptionsService("bad-token", client=client)

    with pytest.raises(OptionsDataError) as error:
        await service.get_snapshot("SPY")
    await service.aclose()

    assert error.value.code == "marketdata_auth_failed"
    assert error.value.status_code == 502


class StubOptionsService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.error: OptionsDataError | None = None

    async def get_snapshot(self, symbol: str, expiration: str | None = None) -> dict[str, object]:
        self.calls.append((symbol, expiration))
        if self.error is not None:
            raise self.error
        return {
            "status": "ok",
            "symbol": symbol,
            "expiration": expiration or "2099-01-17",
        }


@pytest.fixture
def options_api_state() -> Iterator[StubOptionsService]:
    tracked = ("groups", "options_service")
    saved = {name: getattr(app.state, name) for name in tracked if hasattr(app.state, name)}
    service = StubOptionsService()
    app.state.groups = [
        GroupConfig(
            name="OPTIONS",
            assets=[
                AssetConfig(
                    symbol="SPY",
                    type="etf",
                    source="yahoo",
                    exchange="NYSEARCA",
                ),
                AssetConfig(
                    symbol="005930.KS",
                    type="equity",
                    source="yahoo",
                    exchange="KRX",
                ),
            ],
        )
    ]
    app.state.options_service = service
    yield service
    for name in tracked:
        if name in saved:
            setattr(app.state, name, saved[name])
        elif hasattr(app.state, name):
            delattr(app.state, name)


def test_options_route_forwards_symbol_and_expiration(
    options_api_state: StubOptionsService,
) -> None:
    response = TestClient(app).get("/api/options/spy?expiration=2099-01-24")

    assert response.status_code == 200
    assert response.json()["expiration"] == "2099-01-24"
    assert options_api_state.calls == [("SPY", "2099-01-24")]


def test_options_route_rejects_non_us_listing(
    options_api_state: StubOptionsService,
) -> None:
    response = TestClient(app).get("/api/options/005930.KS")

    assert response.status_code == 422
    assert response.json() == {"detail": "options_asset_unsupported"}
    assert options_api_state.calls == []


def test_options_route_preserves_provider_error_contract(
    options_api_state: StubOptionsService,
) -> None:
    options_api_state.error = OptionsDataError("marketdata_auth_failed", status_code=502)

    response = TestClient(app).get("/api/options/SPY")

    assert response.status_code == 502
    assert response.json() == {"detail": "marketdata_auth_failed"}
