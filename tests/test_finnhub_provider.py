import asyncio
from time import monotonic
from typing import Any

import pytest

from app.models import AssetConfig
from app.providers import finnhub as finnhub_module
from app.providers.finnhub import FinnhubProvider


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


class DelayedClient:
    def __init__(self, tracker: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        self.tracker = tracker
        tracker["constructions"] += 1

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        symbol = kwargs["params"]["symbol"]
        self.tracker["requests"].append(symbol)
        self.tracker["active"] += 1
        self.tracker["max_active"] = max(
            self.tracker["max_active"], self.tracker["active"]
        )
        try:
            await asyncio.sleep(0.05)
            last = 0 if symbol == "S12" else 100
            return FakeResponse({"c": last, "pc": 99})
        finally:
            self.tracker["active"] -= 1

    async def aclose(self) -> None:
        self.tracker["closes"] += 1


class RateLimitClient:
    def __init__(self, tracker: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        self.tracker = tracker
        tracker["constructions"] += 1

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        symbol = kwargs["params"]["symbol"]
        self.tracker["requests"].append(symbol)
        self.tracker["active"] += 1
        if self.tracker["active"] == finnhub_module.MAX_QUOTE_CONCURRENCY:
            self.tracker["batch_ready"].set()
        try:
            await self.tracker["batch_ready"].wait()
            if symbol == "S0":
                self.tracker["rate_limited"].set()
                return FakeResponse(
                    {}, status_code=429, headers={"Retry-After": "120"}
                )
            await self.tracker["rate_limited"].wait()
            return FakeResponse({"c": 100, "pc": 99})
        finally:
            self.tracker["active"] -= 1

    async def aclose(self) -> None:
        self.tracker["closes"] += 1


def assets(count: int) -> list[AssetConfig]:
    return [
        AssetConfig(symbol=f"S{index}", type="equity", source="finnhub")
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_quotes_use_bounded_parallelism_and_one_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker: dict[str, Any] = {
        "requests": [],
        "active": 0,
        "max_active": 0,
        "constructions": 0,
        "closes": 0,
    }
    monkeypatch.setattr(
        finnhub_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: DelayedClient(tracker, *args, **kwargs),
    )
    provider = FinnhubProvider("secret")

    started = monotonic()
    quotes = await provider.get_quotes(assets(13))
    elapsed = monotonic() - started

    assert len(tracker["requests"]) == 13
    assert 1 < tracker["max_active"] <= finnhub_module.MAX_QUOTE_CONCURRENCY
    assert tracker["max_active"] == 6
    # Thirteen 50ms calls take >=650ms sequentially; three bounded waves finish well below that.
    assert elapsed < 0.5
    assert len(quotes) == 12  # S12's Finnhub c=0 response is an unknown-symbol sentinel.
    assert tracker["constructions"] == 1
    await provider.aclose()
    assert tracker["closes"] == 1


@pytest.mark.asyncio
async def test_first_batch_429_blocks_queue_and_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 1_000.0
    tracker: dict[str, Any] = {
        "requests": [],
        "active": 0,
        "constructions": 0,
        "closes": 0,
        "batch_ready": asyncio.Event(),
        "rate_limited": asyncio.Event(),
    }
    monkeypatch.setattr(finnhub_module, "monotonic", lambda: clock)
    monkeypatch.setattr(
        finnhub_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: RateLimitClient(tracker, *args, **kwargs),
    )
    provider = FinnhubProvider("secret")

    first = await provider.get_quotes(assets(10))

    assert len(tracker["requests"]) == finnhub_module.MAX_QUOTE_CONCURRENCY
    assert len(first) == finnhub_module.MAX_QUOTE_CONCURRENCY - 1
    assert provider._cooldown_until == 1_120.0

    before = len(tracker["requests"])
    assert await provider.get_quotes(assets(3)) == []
    assert len(tracker["requests"]) == before
