"""Weekly earnings calendar: Nasdaq parsing, ranking, caching, and the route."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from starlette.testclient import TestClient

from app.main import app
from app.models import AssetConfig, GroupConfig
from app.services import earnings as earnings_module
from app.services.earnings import EarningsCalendarService, week_start
from app.services.options import MarketDataOptionsService, OptionsDataError

WEEK_MONDAY = date(2026, 8, 17)
EASTERN = ZoneInfo("America/New_York")


@pytest.fixture()
def eastern_now(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[datetime], None]]:
    """Freeze the earnings module's wall clock; call the fixture to move it."""
    current = [datetime(2026, 8, 17, 10, 0, tzinfo=EASTERN)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return current[0].astimezone(tz)

    def move_to(moment: datetime) -> None:
        current[0] = moment

    monkeypatch.setattr(earnings_module, "datetime", Clock)
    yield move_to


def _epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


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
        release_times: dict[str, tuple[int, int]] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.days = days
        self.surprises = surprises or {}
        self.release_times = release_times or {}
        self.fail = fail
        self.calendar_requests: list[str] = []
        self.surprise_requests: list[str] = []
        self.release_time_requests: list[list[str]] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(503)
        path = request.url.path
        if path == "/america/scan":
            body = json.loads(request.content)
            tickers = body["symbols"]["tickers"]
            self.release_time_requests.append(tickers)
            symbols = {ticker.partition(":")[2] for ticker in tickers}
            data = [
                {"s": f"NASDAQ:{symbol}", "d": [symbol, timestamp, timing]}
                for symbol, (timestamp, timing) in self.release_times.items()
                if symbol in symbols
            ]
            return httpx.Response(200, json={"data": data})
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
        release_times={
            "WMT": (_epoch(2026, 8, 17, 11), -1),
            "BIG": (_epoch(2026, 8, 17, 20, 5), 1),
            "ODD": (_epoch(2026, 8, 17, 12), 0),
            "NEG": (_epoch(2026, 11, 30, 12), -1),
        },
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
        "release_at": "2026-08-17T11:00:00Z",
    }
    assert big["session"] == "amc"
    assert big["last4q"] == [True]
    assert big["release_at"] == "2026-08-17T20:05:00Z"
    assert neg["eps_estimate"] == -0.24
    assert neg["market_cap"] is None
    # Unrecognized report-time strings fall back to "time not supplied".
    assert odd["session"] == "tns"
    assert odd["release_at"] is None
    assert neg["release_at"] is None
    # A detailed row without a market cap flips the ranking-fallback flag.
    assert payload["ranking_fallback"] is True
    assert monday["more"] == 0
    assert monday["total"] == 4
    # Only detailed rows fetch surprise history.
    assert sorted(api.surprise_requests) == ["BIG", "NEG", "ODD", "WMT"]
    assert len(api.release_time_requests) == 1


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
    assert len(api.release_time_requests) == 1

    # Warm caches: a second request performs zero HTTP.
    second = await service.get_week(WEEK_MONDAY, held=set())
    assert len(api.calendar_requests) == 5
    assert api.surprise_requests == ["WMT"]
    assert len(api.release_time_requests) == 1
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
    assert len(api.release_time_requests) == 1


@pytest.mark.asyncio
async def test_failed_surprise_fetch_is_not_cached() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=_surprise_payload(["1.0"]))

    service = EarningsCalendarService(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert await service._surprise_histories(["WMT"]) == {"WMT": []}
    assert "WMT" not in service._surprise_cache

    assert await service._surprise_histories(["WMT"]) == {"WMT": [True]}
    assert attempts == 2


class ScriptedOptions:
    """Options service double: listed expirations and a flat ATM IV."""

    def __init__(
        self,
        expirations: list[str],
        *,
        atm_iv: float = 0.40,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.expirations = expirations
        self.atm_iv = atm_iv
        self.errors = errors or {}
        self.expiration_requests: list[str] = []
        self.chain_requests: list[tuple[str, str | None]] = []

    async def get_expirations(self, symbol: str) -> list[str]:
        self.expiration_requests.append(symbol)
        if symbol in self.errors:
            raise self.errors[symbol]
        return list(self.expirations)

    async def get_snapshot(self, symbol: str, expiration: str | None = None) -> dict[str, object]:
        self.chain_requests.append((symbol, expiration))
        return {
            "expiration": expiration or self.expirations[0],
            "expirations": list(self.expirations),
            "metrics": {"atm_iv": self.atm_iv},
        }


def _moves(payload: dict[str, object]) -> dict[str, float | None]:
    return {
        row["symbol"]: row["implied_move_pct"] for day in _days(payload) for row in day["reports"]
    }


@pytest.mark.asyncio
async def test_implied_move_fetches_only_the_report_expiration_for_held_symbols(
    eastern_now: Callable[[datetime], None],
) -> None:
    api = NasdaqAPI(
        days={
            "2026-08-17": [
                _calendar_row("WMT", market_cap="$2,000"),
                _calendar_row("BIG", market_cap="$9,000"),
            ]
        }
    )
    service = EarningsCalendarService(client=api.client())
    options = ScriptedOptions(["2026-08-14", "2026-08-21", "2026-08-28"])

    payload = await service.get_week(WEEK_MONDAY, held={"WMT"}, options_service=options)

    # Straight to the first expiration after the report — no default chain:
    # 0.40 * sqrt(4/365) * 100 = 4.2 (Monday 10:00 ET -> Friday expiry).
    assert options.chain_requests == [("WMT", "2026-08-21")]
    assert _moves(payload) == {"WMT": 4.2, "BIG": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("time", "expiration", "move"),
    [
        # Before the open: Thursday's session reacts, Thursday's expiry prices
        # it, one day from Wednesday: 0.40 * sqrt(1/365) * 100.
        ("time-pre-market", "2026-08-20", 2.1),
        # After the close (or unknown): Friday's session reacts; two days out.
        ("time-after-hours", "2026-08-21", 3.0),
        ("time-not-supplied", "2026-08-21", 3.0),
    ],
)
async def test_implied_move_covers_the_reaction_session_from_today(
    eastern_now: Callable[[datetime], None], time: str, expiration: str, move: float
) -> None:
    eastern_now(datetime(2026, 8, 19, 10, 0, tzinfo=EASTERN))  # Wednesday
    api = NasdaqAPI(days={"2026-08-20": [_calendar_row("WMT", time=time)]})
    service = EarningsCalendarService(client=api.client())
    options = ScriptedOptions(["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-28"])

    payload = await service.get_week(WEEK_MONDAY, held={"WMT"}, options_service=options)

    assert options.chain_requests == [("WMT", expiration)]
    assert _moves(payload) == {"WMT": move}


@pytest.mark.asyncio
async def test_implied_moves_skip_reports_already_out(
    eastern_now: Callable[[datetime], None],
) -> None:
    eastern_now(datetime(2026, 8, 19, 18, 0, tzinfo=EASTERN))  # Wednesday after the close
    api = NasdaqAPI(
        days={
            "2026-08-17": [_calendar_row("MON", time="time-after-hours")],
            "2026-08-19": [
                _calendar_row("PRE", time="time-pre-market"),
                _calendar_row("POST", time="time-after-hours"),
            ],
            "2026-08-20": [_calendar_row("THU")],
        }
    )
    service = EarningsCalendarService(client=api.client())
    options = ScriptedOptions(["2026-08-21", "2026-08-28"])

    payload = await service.get_week(
        WEEK_MONDAY, held={"MON", "PRE", "POST", "THU"}, options_service=options
    )

    assert sorted(options.expiration_requests) == ["POST", "THU"]
    moves = _moves(payload)
    assert moves["MON"] is None and moves["PRE"] is None
    assert moves["POST"] is not None and moves["THU"] is not None


@pytest.mark.asyncio
async def test_implied_moves_and_their_absence_are_cached_per_report(
    eastern_now: Callable[[datetime], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [10_000.0]
    monkeypatch.setattr(earnings_module, "monotonic", lambda: clock[0])
    api = NasdaqAPI(days={"2026-08-18": [_calendar_row("WMT"), _calendar_row("NOP")]})
    service = EarningsCalendarService(client=api.client())
    options = ScriptedOptions(
        ["2026-08-21"],
        errors={"NOP": OptionsDataError("options_expirations_unavailable", status_code=404)},
    )

    first = await service.get_week(WEEK_MONDAY, held={"WMT", "NOP"}, options_service=options)
    assert sorted(options.expiration_requests) == ["NOP", "WMT"]

    # Hours later (well past the 30-minute warm cadence): no MarketData calls.
    clock[0] += 4 * 3600
    second = await service.get_week(WEEK_MONDAY, held={"WMT", "NOP"}, options_service=options)
    assert sorted(options.expiration_requests) == ["NOP", "WMT"]
    assert len(options.chain_requests) == 1
    assert _moves(second) == _moves(first)
    assert _moves(second)["WMT"] is not None and _moves(second)["NOP"] is None

    clock[0] += earnings_module.IMPLIED_MOVE_CACHE_SECONDS
    await service.get_week(WEEK_MONDAY, held={"WMT", "NOP"}, options_service=options)
    assert sorted(options.expiration_requests) == ["NOP", "NOP", "WMT", "WMT"]


@pytest.mark.asyncio
async def test_warm_loop_spends_a_small_daily_marketdata_budget(
    eastern_now: Callable[[datetime], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A day of 30-minute warm passes over a busy held week stays within budget."""
    symbols = [f"S{index:02d}" for index in range(25)]
    api = NasdaqAPI(
        days={
            (WEEK_MONDAY + timedelta(days=offset)).isoformat(): [
                _calendar_row(symbol) for symbol in symbols[offset * 5 : offset * 5 + 5]
            ]
            for offset in range(5)
        }
    )
    service = EarningsCalendarService(client=api.client())
    requests: list[str] = []

    def marketdata(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if "/expirations/" in request.url.path:
            return httpx.Response(200, json={"s": "ok", "expirations": ["2026-08-21"]})
        return httpx.Response(
            200,
            json={
                "side": ["call", "put"],
                "strike": [100, 100],
                "openInterest": [10, 10],
                "gamma": [0.01, 0.01],
                "iv": [0.40, 0.40],
                "underlyingPrice": [100, 100],
            },
        )

    options = MarketDataOptionsService(
        "token", client=httpx.AsyncClient(transport=httpx.MockTransport(marketdata))
    )
    clock = [10_000.0]
    monkeypatch.setattr(earnings_module, "monotonic", lambda: clock[0])
    # One MarketData credit day: 09:30 ET Monday to 09:30 ET Tuesday.
    start = datetime(2026, 8, 17, 9, 30, tzinfo=EASTERN)
    try:
        for warm_pass in range(48):
            eastern_now(start + timedelta(minutes=30 * warm_pass))
            payload = await service.get_week_cached(WEEK_MONDAY, set(symbols), options)
            clock[0] += 30 * 60 + 1
        assert 0 < len(requests) <= earnings_module.IMPLIED_MOVE_DAILY_REQUESTS
        assert any(move is not None for move in _moves(payload).values())

        # The next credit day brings a fresh budget for the remaining reports.
        spent = len(requests)
        eastern_now(start + timedelta(days=1, minutes=1))
        await service.get_week_cached(WEEK_MONDAY, set(symbols), options)
        assert spent < len(requests) <= spent + earnings_module.IMPLIED_MOVE_DAILY_REQUESTS
    finally:
        await options.aclose()


@pytest.mark.asyncio
async def test_rate_limited_options_are_logged_and_pause_lookups(
    eastern_now: Callable[[datetime], None], caplog: pytest.LogCaptureFixture
) -> None:
    api = NasdaqAPI(days={"2026-08-18": [_calendar_row(s) for s in ("AAA", "BBB", "CCC")]})
    service = EarningsCalendarService(client=api.client())
    limited = OptionsDataError("marketdata_rate_limited")
    options = ScriptedOptions(
        ["2026-08-21"], errors={symbol: limited for symbol in ("AAA", "BBB", "CCC")}
    )

    with caplog.at_level(logging.WARNING, logger=earnings_module.__name__):
        payload = await service.get_week(
            WEEK_MONDAY, held={"AAA", "BBB", "CCC"}, options_service=options
        )
        await service.get_week(WEEK_MONDAY, held={"AAA", "BBB", "CCC"}, options_service=options)

    # The quota is token-wide: one failed lookup stops the pass and the next.
    assert len(options.expiration_requests) == 1
    assert any("marketdata_rate_limited" in record.getMessage() for record in caplog.records)
    assert set(_moves(payload).values()) == {None}


@pytest.mark.asyncio
async def test_options_failures_leave_implied_move_empty(
    eastern_now: Callable[[datetime], None],
) -> None:
    api = NasdaqAPI(days={"2026-08-17": [_calendar_row("WMT")]})
    service = EarningsCalendarService(client=api.client())
    options = ScriptedOptions(["2026-08-21"], errors={"WMT": RuntimeError("boom")})

    payload = await service.get_week(WEEK_MONDAY, held={"WMT"}, options_service=options)

    assert options.expiration_requests == ["WMT"]
    assert _days(payload)[0]["reports"][0]["implied_move_pct"] is None


@pytest.mark.asyncio
async def test_past_days_keep_the_session_and_release_time_learned_earlier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = NasdaqAPI(
        days={"2026-08-17": [_calendar_row("WMT", time="time-pre-market")]},
        release_times={"WMT": (_epoch(2026, 8, 17, 11), -1)},
    )
    service = EarningsCalendarService(client=api.client())
    clock = [10_000.0]
    monkeypatch.setattr(earnings_module, "monotonic", lambda: clock[0])
    await service.get_week(WEEK_MONDAY, held=set())

    # Once the day is past Nasdaq answers "time not supplied" and TradingView
    # has moved on to the next quarter's release.
    clock[0] += earnings_module.DAY_CACHE_SECONDS + 1
    api.days = {"2026-08-17": [_calendar_row("WMT", time="time-not-supplied")]}
    api.release_times = {"WMT": (_epoch(2026, 11, 17, 11), -1)}
    payload = await service.get_week(WEEK_MONDAY, held=set())

    assert len(api.calendar_requests) == 10
    wmt = _days(payload)[0]["reports"][0]
    assert wmt["session"] == "bmo"
    assert wmt["release_at"] == "2026-08-17T11:00:00Z"


@pytest.mark.asyncio
async def test_payload_cache_serves_repeat_reads_per_held_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = NasdaqAPI(
        days={
            "2026-08-17": [
                _calendar_row("BIG", market_cap="$9,000,000,000"),
                _calendar_row("WMT"),
            ]
        }
    )
    service = EarningsCalendarService(client=api.client())
    clock = [10_000.0]
    monkeypatch.setattr(earnings_module, "monotonic", lambda: clock[0])

    first = await service.get_week_cached(WEEK_MONDAY, held=set())
    assert [row["symbol"] for row in _days(first)[0]["reports"]] == ["BIG", "WMT"]
    assert len(api.calendar_requests) == 5

    # Inside the TTL the composed payload answers with zero provider work.
    assert await service.get_week_cached(WEEK_MONDAY, held=set()) is first

    # A watchlist edit re-ranks at once, riding the warm day caches.
    edited = await service.get_week_cached(WEEK_MONDAY, held={"WMT"})
    assert [row["symbol"] for row in _days(edited)[0]["reports"]] == ["WMT", "BIG"]
    assert len(api.calendar_requests) == 5

    clock[0] += earnings_module.PAYLOAD_CACHE_SECONDS + 1
    third = await service.get_week_cached(WEEK_MONDAY, held=set())
    assert third is not first
    # The recompute rides the still-warm day caches — zero new HTTP.
    assert len(api.calendar_requests) == 5


# --- /api/earnings route ----------------------------------------------------


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[date, set[str], object]] = []

    async def get_week_cached(
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


def test_route_default_week_follows_the_eastern_date(
    earnings_app_state: RecordingService, eastern_now: Callable[[datetime], None]
) -> None:
    # Friday 20:30 ET is already Saturday in UTC; after-close reporters of
    # this week must stay on screen.
    eastern_now(datetime(2026, 8, 21, 20, 30, tzinfo=EASTERN))

    response = TestClient(app).get("/api/earnings")

    assert response.status_code == 200
    assert earnings_app_state.calls[0][0] == date(2026, 8, 17)


def test_route_rejects_malformed_start(earnings_app_state: RecordingService) -> None:
    client = TestClient(app)

    response = client.get("/api/earnings", params={"start": "not-a-date"})

    assert response.status_code == 422
    assert response.json()["detail"] == "start_invalid"
    assert earnings_app_state.calls == []
