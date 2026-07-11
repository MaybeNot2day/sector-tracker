from datetime import date
from typing import Any

import pytest

from app.models import AssetConfig
from app.providers import stooq as stooq_module
from app.providers.stooq import StooqProvider


class FakeResponse:
    text = "Date,Open,High,Low,Close,Volume\n2026-07-09,10,12,9,11,100\n"

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self, tracker: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        self.tracker = tracker
        tracker["constructions"] += 1
        tracker["client_kwargs"] = kwargs

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        timeout: float | None = None,
    ) -> FakeResponse:
        self.tracker["requests"].append((url, dict(params), timeout))
        return FakeResponse()

    async def aclose(self) -> None:
        self.tracker["closes"] += 1


@pytest.mark.asyncio
async def test_history_bounds_requests_reuses_client_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker: dict[str, Any] = {
        "constructions": 0,
        "closes": 0,
        "client_kwargs": {},
        "requests": [],
    }
    monkeypatch.setattr(stooq_module, "_utc_today", lambda: date(2026, 7, 10))
    monkeypatch.setattr(
        stooq_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeClient(tracker, *args, **kwargs),
    )
    provider = StooqProvider()
    asset = AssetConfig(symbol="SPY", type="etf", source="stooq")

    await provider.get_history(asset, interval="1d", range_="1mo")
    await provider.get_history(asset, interval="1d", range_="ytd")
    await provider.get_history(asset, interval="1d", range_="all")

    requests = tracker["requests"]
    assert len(requests) == 3
    assert requests[0][1] == {
        "s": "spy.us",
        "i": "d",
        "d1": "20260602",
        "d2": "20260710",
    }
    assert requests[1][1] == {
        "s": "spy.us",
        "i": "d",
        "d1": "20251225",
        "d2": "20260710",
    }
    assert requests[2][1] == {"s": "spy.us", "i": "d", "d2": "20260710"}
    assert [request[2] for request in requests] == [15.0, 15.0, 15.0]
    assert tracker["client_kwargs"] == {"timeout": 10.0}
    assert tracker["constructions"] == 1

    await provider.aclose()
    assert tracker["closes"] == 1
