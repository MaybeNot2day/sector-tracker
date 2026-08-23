from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from starlette.testclient import TestClient

from app.main import app
from app.services.sofr import (
    REGULAR_POLL_SECONDS,
    RELEASE_POLL_SECONDS,
    SOFRService,
    sofr_poll_seconds,
)


class NewYorkFedAPI:
    def __init__(self) -> None:
        self.fail = False
        self.requests = 0

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.fail:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "refRates": [
                    {
                        "effectiveDate": "2026-08-20",
                        "type": "SOFR",
                        "percentRate": 3.63,
                        "percentPercentile1": 3.58,
                        "percentPercentile25": 3.60,
                        "percentPercentile75": 3.68,
                        "percentPercentile99": 3.71,
                        "volumeInBillions": 2922,
                        "revisionIndicator": "",
                    },
                    {
                        "effectiveDate": "2026-08-19",
                        "type": "SOFR",
                        "percentRate": 3.62,
                        "percentPercentile1": 3.58,
                        "percentPercentile25": 3.60,
                        "percentPercentile75": 3.67,
                        "percentPercentile99": 3.70,
                        "volumeInBillions": 2923,
                        "revisionIndicator": "",
                    },
                ]
            },
        )


@pytest.mark.asyncio
async def test_fetch_persists_distribution_history_and_macro_item(tmp_path: Path) -> None:
    api = NewYorkFedAPI()
    service = SOFRService(tmp_path / "board.sqlite3", client=api.client(), cache_seconds=300)

    payload = await service.get_payload()

    latest = payload["latest"]
    assert isinstance(latest, dict)
    assert latest == {
        "effective_date": "2026-08-20",
        "rate": 3.63,
        "percentile_1": 3.58,
        "percentile_25": 3.6,
        "percentile_75": 3.68,
        "percentile_99": 3.71,
        "volume_billions": 2922.0,
        "revision_indicator": "",
        "fetched_at": payload["as_of"],
        "change_bp": 1.0,
    }
    assert payload["series"] == [
        {"date": "2026-08-19", "rate": 3.62, "volume_billions": 2923.0},
        {"date": "2026-08-20", "rate": 3.63, "volume_billions": 2922.0},
    ]
    assert service.macro_item() == {
        "symbol": "SOFR",
        "label": "SOFR",
        "unit": "yield",
        "last": 3.63,
        "change_abs": 0.01,
        "change_pct": None,
        "invert_tone": True,
        "is_stale": False,
    }
    assert api.requests == 1
    await service.aclose()

    restarted = SOFRService(tmp_path / "board.sqlite3")
    assert restarted.snapshot() is not None
    assert restarted.snapshot()["stale"] is True  # type: ignore[index]


@pytest.mark.asyncio
async def test_refresh_failure_keeps_persisted_payload(tmp_path: Path) -> None:
    api = NewYorkFedAPI()
    service = SOFRService(tmp_path / "board.sqlite3", client=api.client(), cache_seconds=0)
    await service.get_payload()
    api.fail = True

    changed = await service.refresh(force=True)

    assert changed is False
    payload = service.snapshot()
    assert payload is not None
    assert payload["stale"] is True
    assert "persisted data" in str(payload["warning"])
    await service.aclose()


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-08-20T07:54:00-04:00", REGULAR_POLL_SECONDS),
        ("2026-08-20T07:55:00-04:00", RELEASE_POLL_SECONDS),
        ("2026-08-20T09:29:00-04:00", RELEASE_POLL_SECONDS),
        ("2026-08-20T09:30:00-04:00", REGULAR_POLL_SECONDS),
        ("2026-08-22T08:00:00-04:00", REGULAR_POLL_SECONDS),
    ],
)
def test_poll_cadence_tightens_across_release_window(timestamp: str, expected: float) -> None:
    now = datetime.fromisoformat(timestamp).astimezone(ZoneInfo("America/New_York"))
    assert sofr_poll_seconds(now) == expected


@pytest.mark.asyncio
async def test_sofr_endpoint_serves_service_payload(tmp_path: Path) -> None:
    api = NewYorkFedAPI()
    service = SOFRService(tmp_path / "board.sqlite3", client=api.client())
    saved = getattr(app.state, "sofr_service", None)
    had_service = hasattr(app.state, "sofr_service")
    app.state.sofr_service = service
    try:
        response = TestClient(app).get("/api/sofr")
        assert response.status_code == 200
        assert response.json()["latest"]["rate"] == 3.63
    finally:
        await service.aclose()
        if had_service:
            app.state.sofr_service = saved
        else:
            del app.state.sofr_service
