"""Weekly earnings calendar: Nasdaq parsing, ranking, caching, and the route."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from app.main import app
from app.models import AssetConfig, GroupConfig
from app.services import earnings as earnings_module
from app.services.earnings import EarningsCalendarService, week_start

WEEK_MONDAY = date(2026, 8, 17)


def _calendar_row(
    symbol: str,
    *,
    name: str | None = None,
    time: str = "time-pre-market",
    eps: str = "$1.00",
    market_cap: str = "$1,000,000",
    estimates: str = "3",
) -> dict[str, str]:
    return {
        "symbol": symbol,
        "name": name or f"{symbol} Inc.",
        "time": time,
        "epsForecast": eps,
        "marketCap": market_cap,
        "noOfEsts": estimates,
        "fiscalQuarterEnding": "Jun/2026",
    }


def _surprise_payload(surprises: list[str]) -> dict[str, Any]:
    # Nasdaq returns newest-first rows.
    return {
        "data": {
            "earningsSurpriseTable": {
                "rows": [{"percentageSurprise": value} for value in surprises]
            }
        }
    }


class NasdaqAPI:
    """Scripted api.nasdaq.com: calendar rows per date, surprises per symbol."""

    def __init__(
        self,
        days: dict[str, list[dict[str, str]]],
        surprises: dict[str, list[str]] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.days = days
        self.surprises = surprises or {}
        self.fail = fail
        self.calendar_requests: list[str] = []
        self.surprise_requests: list[str] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(503)
        path = request.url.path
        if path == "/api/calendar/earnings":
            day = request.url.params["date"]
            self.calendar_requests.append(day)
            return httpx.Response(200, json={"data": {"rows": self.days.get(day, [])}})
        symbol = path.removeprefix("/api/company/").removesuffix("/earnings-surprise")
        self.surprise_requests.append(symbol)
        return httpx.Response(200, json=_surprise_payload(self.surprises.get(symbol, [])))


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 8, 17), date(2026, 8, 17)),  # Monday keeps its week
        (date(2026, 8, 19), date(2026, 8, 17)),  # midweek snaps back
        (date(2026, 8, 15), date(2026, 8, 17)),  # Saturday rolls forward
        (date(2026, 8, 16), date(2026, 8, 17)),  # Sunday rolls forward
    ],
)
def test_week_start(today: date, expected: date) -> None:
    assert week_start(today) == expected


def _days(payload: dict[str, object]) -> list[dict[str, Any]]:
    days = payload["days"]
    assert isinstance(days, list)
    return days


@pytest.mark.asyncio
async def test_week_payload_parses_ranks_and_enriches() -> None:
    api = NasdaqAPI(
        days={
            "2026-08-17": [
                _calendar_row("BIG", market_cap="$9,000,000,000", time="time-after-hours"),
                _calendar_row("WMT", market_cap="$2,000,000", eps="$0.75"),
                _calendar_row("NEG", eps="($0.24)", market_cap="", estimates=""),
                _calendar_row("ODD", time="whenever", market_cap="$5,000"),
            ],
        },
        surprises={"WMT": ["1.54", "-6.85", "bad", "2.0"], "BIG": ["3.0"]},
    )
    service = EarningsCalendarService(client=api.client())

    payload = await service.get_week(WEEK_MONDAY, held={"WMT"})

    assert payload["week_start"] == "2026-08-17"
    assert payload["week_end"] == "2026-08-21"
    days = payload["days"]
    assert isinstance(days, list) and len(days) == 5
    monday = days[0]
    assert monday["weekday"] == "MON"
    reports = monday["reports"]
    # Held first, then market cap descending, nulls last.
    assert [row["symbol"] for row in reports] == ["WMT", "BIG", "ODD", "NEG"]
    wmt, big, odd, neg = reports
    assert wmt == {
        "symbol": "WMT",
        "name": "WMT Inc.",
        "session": "bmo",
        "eps_estimate": 0.75,
        "market_cap": 2_000_000.0,
        "estimates": 3,
        "fiscal_quarter": "Jun/2026",
        "held": True,
        # Newest-first from Nasdaq, rendered oldest -> newest; junk row is None.
        "last4q": [True, None, False, True],
        "implied_move_pct": None,
    }
    assert big["session"] == "amc"
    assert big["last4q"] == [True]
    assert neg["eps_estimate"] == -0.24
    assert neg["market_cap"] is None
    # Unrecognized report-time strings fall back to "time not supplied".
    assert odd["session"] == "tns"
    # A detailed row without a market cap flips the ranking-fallback flag.
    assert payload["ranking_fallback"] is True
    assert monday["more"] == 0
    assert monday["total"] == 4
    # Only detailed rows fetch surprise history.
    assert sorted(api.surprise_requests) == ["BIG", "NEG", "ODD", "WMT"]


@pytest.mark.asyncio
async def test_day_overflow_counts_instead_of_rendering() -> None:
    rows = [
        _calendar_row(f"SYM{index}", market_cap=f"${(20 - index) * 1000}") for index in range(10)
    ]
    api = NasdaqAPI(days={"2026-08-17": rows})
    service = EarningsCalendarService(client=api.client())

    payload = await service.get_week(WEEK_MONDAY, held=set())

    monday = _days(payload)[0]
    assert [row["symbol"] for row in monday["reports"]] == [f"SYM{index}" for index in range(7)]
    assert monday["more"] == 3
    assert monday["total"] == 10


@pytest.mark.asyncio
async def test_caches_serve_repeat_weeks_and_survive_outages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = NasdaqAPI(days={"2026-08-17": [_calendar_row("WMT")]}, surprises={"WMT": ["1.0"]})
    service = EarningsCalendarService(client=api.client())
    clock = [10_000.0]
    monkeypatch.setattr(earnings_module, "monotonic", lambda: clock[0])

    first = await service.get_week(WEEK_MONDAY, held=set())
    assert len(api.calendar_requests) == 5
    assert api.surprise_requests == ["WMT"]

    # Warm caches: a second request performs zero HTTP.
    second = await service.get_week(WEEK_MONDAY, held=set())
    assert len(api.calendar_requests) == 5
    assert api.surprise_requests == ["WMT"]
    assert second["days"] == first["days"]

    # Past the TTL with the API down: stale rows keep serving.
    clock[0] += earnings_module.DAY_CACHE_SECONDS + 1
    api.fail = True
    stale = await service.get_week(WEEK_MONDAY, held=set())
    assert _days(stale)[0]["reports"][0]["symbol"] == "WMT"

    # Within the failure cooldown no new attempts are made.
    api.fail = False
    clock[0] += 1
    cooled = await service.get_week(WEEK_MONDAY, held=set())
    assert len(api.calendar_requests) == 5
    assert _days(cooled)[0]["reports"][0]["symbol"] == "WMT"


@pytest.mark.asyncio
async def test_implied_move_scales_atm_iv_for_held_symbols_only() -> None:
    api = NasdaqAPI(
        days={
            "2026-08-17": [
                _calendar_row("WMT", market_cap="$2,000"),
                _calendar_row("BIG", market_cap="$9,000"),
            ]
        }
    )
    service = EarningsCalendarService(client=api.client())
    snapshot_calls: list[tuple[str, str | None]] = []

    class ScriptedOptions:
        async def get_snapshot(
            self, symbol: str, expiration: str | None = None
        ) -> dict[str, object]:
            snapshot_calls.append((symbol, expiration))
            return {
                "expiration": expiration or "2026-08-14",
                "expirations": ["2026-08-14", "2026-08-21", "2026-08-28"],
                "metrics": {"atm_iv": 0.40},
            }

    payload = await service.get_week(WEEK_MONDAY, held={"WMT"}, options_service=ScriptedOptions())

    reports = _days(payload)[0]["reports"]
    by_symbol = {row["symbol"]: row for row in reports}
    # The default expiration predates the report; the first one after it is used:
    # 0.40 * sqrt(4/365) * 100 = 4.2.
    assert snapshot_calls == [("WMT", None), ("WMT", "2026-08-21")]
    assert by_symbol["WMT"]["implied_move_pct"] == 4.2
    assert by_symbol["BIG"]["implied_move_pct"] is None


@pytest.mark.asyncio
async def test_options_failures_leave_implied_move_empty() -> None:
    api = NasdaqAPI(days={"2026-08-17": [_calendar_row("WMT")]})
    service = EarningsCalendarService(client=api.client())

    class BrokenOptions:
        async def get_snapshot(
            self, symbol: str, expiration: str | None = None
        ) -> dict[str, object]:
            raise RuntimeError("options_not_configured")

    payload = await service.get_week(WEEK_MONDAY, held={"WMT"}, options_service=BrokenOptions())

    assert _days(payload)[0]["reports"][0]["implied_move_pct"] is None


# --- /api/earnings route ----------------------------------------------------


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[date, set[str], object]] = []

    async def get_week(
        self, start: date, held: set[str], options_service: object = None
    ) -> dict[str, object]:
        self.calls.append((start, held, options_service))
        return {"week_start": start.isoformat(), "days": []}


@pytest.fixture()
def earnings_app_state() -> Any:
    saved = {
        name: getattr(app.state, name)
        for name in ("earnings_service", "groups", "options_service")
        if hasattr(app.state, name)
    }
    service = RecordingService()
    app.state.earnings_service = service
    app.state.groups = [
        GroupConfig(name="TEST", assets=[AssetConfig(symbol="wmt", type="equity", source="yahoo")])
    ]
    app.state.options_service = SimpleNamespace()
    yield service
    for name in ("earnings_service", "groups", "options_service"):
        if name in saved:
            setattr(app.state, name, saved[name])
        elif hasattr(app.state, name):
            delattr(app.state, name)


def test_route_snaps_start_to_monday_and_passes_held(earnings_app_state: RecordingService) -> None:
    client = TestClient(app)

    response = client.get("/api/earnings", params={"start": "2026-08-19"})

    assert response.status_code == 200
    assert json.loads(response.content)["week_start"] == "2026-08-17"
    (start, held, options_service) = earnings_app_state.calls[0]
    assert start == date(2026, 8, 17)
    assert held == {"WMT"}
    assert options_service is app.state.options_service


def test_route_keeps_explicit_weekend_start_in_its_own_week(
    earnings_app_state: RecordingService,
) -> None:
    client = TestClient(app)

    response = client.get("/api/earnings", params={"start": "2026-08-15"})

    assert response.status_code == 200
    # Saturday belongs to the Aug 10 trading week, not the following one.
    assert earnings_app_state.calls[0][0] == date(2026, 8, 10)


def test_route_rejects_malformed_start(earnings_app_state: RecordingService) -> None:
    client = TestClient(app)

    response = client.get("/api/earnings", params={"start": "not-a-date"})

    assert response.status_code == 422
    assert response.json()["detail"] == "start_invalid"
    assert earnings_app_state.calls == []
