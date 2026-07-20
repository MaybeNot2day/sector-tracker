"""Key-date release enrichment from the TradingView economic calendar.

Contract: /api/key-dates items gain a `release` object (consensus,
previous, actual, surprise, importance, indicator comment) matched by
fuzzy title against calendar rows within one day of the stored event
date. Unmatched or ambiguous events keep release null — a mislisted
event must never borrow a far row's numbers. The service caches the
calendar snapshot with an adaptive TTL that collapses around scheduled
release times, and a failed fetch keeps the previous snapshot behind a
short cooldown. Enrichment failure serves the plain payload, never 500.

The fixture is a trimmed capture of the real endpoint (fetched once,
inlined); tests never touch the network.
"""

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from app import db
from app.main import app
from app.scheduler import _release_changed, _release_state
from app.services.econ_calendar import (
    HOT_AFTER,
    HOT_BEFORE,
    HOT_TTL_SECONDS,
    CalendarRow,
    EconCalendarService,
    _is_hot,
    any_hot_release,
    format_display,
    match_release,
    normalize_calendar_rows,
)

# Trimmed rows from a live GET https://economic-calendar.tradingview.com/
# events capture (2026-07-16): the US Jul-16 print cluster, the EU/GB
# balance-of-trade pair, the Jul-17 EU inflation family, and the Jul-23
# ECB decision + next initial-claims week as far-row decoys.
_FIXTURE_JSON = r"""
[
 {"id": "397867", "title": "Balance of Trade", "indicator": "Balance of Trade",
  "country": "GB", "period": "May", "actual": -1.044, "previous": -7.053,
  "forecast": null, "unit": "\u00a3", "scale": "B", "currency": "GBP",
  "importance": -1,
  "comment": "The UK's trade balance has been in deficit since 1998.",
  "date": "2026-07-16T06:00:00.000Z"},
 {"id": "401104", "title": "Balance of Trade", "indicator": "Balance of Trade",
  "country": "EU", "period": "May", "actual": -7.8, "previous": -1.2,
  "forecast": -1.6, "unit": "\u20ac", "scale": "B", "currency": "EUR",
  "importance": 0,
  "comment": "The Euro Area is one of the world's biggest players in global trade.",
  "date": "2026-07-16T09:00:00.000Z"},
 {"id": "396296", "title": "Initial Jobless Claims", "indicator": "Initial Jobless Claims",
  "country": "US", "period": "Jul/11", "actual": 208, "previous": 216,
  "forecast": 217, "scale": "K", "currency": "USD", "importance": 0,
  "comment": "Initial jobless claims: people filing for unemployment benefits the first time.",
  "date": "2026-07-16T12:30:00.000Z"},
 {"id": "396297", "title": "Jobless Claims 4-week Average",
  "indicator": "Jobless Claims 4-week Average", "country": "US",
  "period": "Jul/11", "actual": 214.25, "previous": 219, "forecast": null,
  "scale": "K", "currency": "USD", "importance": -1,
  "date": "2026-07-16T12:30:00.000Z"},
 {"id": "396298", "title": "Continuing Jobless Claims",
  "indicator": "Continuing Jobless Claims", "country": "US",
  "period": "Jul/04", "actual": 1805, "previous": 1821, "forecast": 1820,
  "scale": "K", "currency": "USD", "importance": -1,
  "comment": "Continuing Jobless Claims refer to unemployed currently receiving benefits.",
  "date": "2026-07-16T12:30:00.000Z"},
 {"id": "398242", "title": "Philadelphia Fed Manufacturing Index",
  "indicator": "Philadelphia Fed Manufacturing Index", "country": "US",
  "period": "Jul", "actual": 41.4, "previous": 10.3, "forecast": 13,
  "currency": "USD", "importance": 0,
  "comment": "Business Outlook Survey of manufacturers in the Third Federal Reserve District.",
  "date": "2026-07-16T12:30:00.000Z"},
 {"id": "398340", "title": "Retail Sales Ex Autos MoM",
  "indicator": "Retail Sales Ex Autos", "country": "US", "period": "Jun",
  "actual": -0.2, "previous": 1, "forecast": -0.1, "unit": "%",
  "currency": "USD", "importance": 0,
  "comment": "Sales of retail goods and services excluding the automobile sector.",
  "date": "2026-07-16T12:30:00.000Z"},
 {"id": "398849", "title": "Retail Sales Ex Gas/Autos MoM",
  "indicator": "Retail Sales Ex Gas and Autos MoM", "country": "US",
  "period": "Jun", "actual": 0.4, "previous": 0.8, "forecast": null,
  "unit": "%", "currency": "USD", "importance": -1,
  "date": "2026-07-16T12:30:00.000Z"},
 {"id": "398870", "title": "Retail Sales YoY", "indicator": "Retail Sales YoY",
  "country": "US", "period": "Jun", "actual": 6.7, "previous": 7.3,
  "forecast": null, "unit": "%", "currency": "USD", "importance": -1,
  "date": "2026-07-16T12:30:00.000Z"},
 {"id": "399061", "title": "Retail Sales MoM", "indicator": "Retail Sales MoM",
  "country": "US", "period": "Jun", "actual": 0.2, "previous": 1,
  "forecast": 0.2, "unit": "%", "currency": "USD", "importance": 1,
  "comment": "Aggregated measure of sales of retail goods and services over a period of a month.",
  "date": "2026-07-16T12:30:00.000Z"},
 {"id": "399320", "title": "Retail Sales Control Group MoM",
  "indicator": "Retail Sales Control Group", "country": "US", "period": "Jun",
  "actual": 0.5, "previous": 0.8, "forecast": 0.5, "unit": "%",
  "currency": "USD", "importance": 0,
  "date": "2026-07-16T12:30:00.000Z"},
 {"id": "401674", "title": "Core Inflation Rate YoY Final",
  "indicator": "Core Inflation Rate", "country": "EU", "period": "Jun",
  "actual": null, "previous": 2.6, "forecast": 2.4, "unit": "%",
  "currency": "EUR", "importance": -1,
  "date": "2026-07-17T09:00:00.000Z"},
 {"id": "403374", "title": "CPI Final", "indicator": "Consumer Price Index CPI",
  "country": "EU", "period": "Jun", "actual": null, "previous": 103.13,
  "forecast": 103.07, "currency": "EUR", "importance": -1,
  "date": "2026-07-17T09:00:00.000Z"},
 {"id": "403431", "title": "Inflation Rate YoY Final", "indicator": "Inflation Rate",
  "country": "EU", "period": "Jun", "actual": null, "previous": 3.2,
  "forecast": 2.8, "unit": "%", "currency": "EUR", "importance": -1,
  "date": "2026-07-17T09:00:00.000Z"},
 {"id": "396308", "title": "Michigan Consumer Sentiment Prel",
  "indicator": "Consumer Confidence", "country": "US", "period": "Jul",
  "actual": null, "previous": 49.5, "forecast": 51, "currency": "USD",
  "importance": 1,
  "date": "2026-07-17T14:00:00.000Z"},
 {"id": "393890", "title": "ECB Interest Rate Decision", "indicator": "Interest Rate",
  "country": "EU", "period": "", "actual": null, "previous": 2.4,
  "forecast": 2.4, "unit": "%", "currency": "EUR", "importance": 1,
  "comment": "Benchmark rate set by the Governing Council of the European Central Bank.",
  "date": "2026-07-23T12:15:00.000Z"},
 {"id": "396346", "title": "Initial Jobless Claims", "indicator": "Initial Jobless Claims",
  "country": "US", "period": "Jul/18", "actual": null, "previous": 208,
  "forecast": null, "scale": "K", "currency": "USD", "importance": 0,
  "date": "2026-07-23T12:30:00.000Z"},
 {"id": "420828", "title": "Fed Williams Speech", "indicator": "Interest Rate",
  "country": "US", "period": "", "actual": null, "previous": null,
  "forecast": null, "importance": 0, "date": "2026-07-15T12:45:00.000Z"},
 {"id": "420855", "title": "ECB Cipollone Speech", "indicator": "Interest Rate",
  "country": "EU", "period": "", "actual": null, "previous": null,
  "forecast": null, "importance": -1, "date": "2026-07-17T10:00:00.000Z"},
 {"id": "404101", "title": "Housing Starts", "indicator": "Housing Starts",
  "country": "US", "period": "Jun", "actual": null, "previous": 1.177,
  "forecast": 1.31, "scale": "M", "currency": "USD", "importance": 1,
  "date": "2026-07-17T12:30:00.000Z"},
 {"id": "404102", "title": "Building Permits Prel", "indicator": "Building Permits",
  "country": "US", "period": "Jun", "actual": null, "previous": 1.41,
  "forecast": 1.4, "scale": "M", "currency": "USD", "importance": 1,
  "date": "2026-07-17T12:30:00.000Z"},
 {"id": "404103", "title": "Industrial Production MoM",
  "indicator": "Industrial Production MoM", "country": "US", "period": "Jun",
  "actual": null, "previous": 0.1, "forecast": 0.2, "unit": "%",
  "currency": "USD", "importance": 0, "date": "2026-07-17T13:15:00.000Z"},
 {"id": "405501", "title": "CB Leading Index MoM", "indicator": "CB Leading Index",
  "country": "US", "period": "Jun", "actual": null, "previous": 0.1,
  "forecast": -0.1, "unit": "%", "currency": "USD", "importance": 0,
  "date": "2026-07-20T14:00:00.000Z"},
 {"id": "405502", "title": "Unemployment Rate", "indicator": "Unemployment Rate",
  "country": "GB", "period": "May", "actual": null, "previous": 4.9,
  "forecast": 5, "unit": "%", "currency": "GBP", "importance": 1,
  "date": "2026-07-21T06:00:00.000Z"},
 {"id": "405503", "title": "Inflation Rate YoY", "indicator": "Inflation Rate",
  "country": "CA", "period": "Jun", "actual": 2.8, "previous": 3.2,
  "forecast": 2.9, "unit": "%", "currency": "CAD", "importance": 1,
  "ticker": "ECONOMICS:CAIRYY", "source": "Statistics Canada",
  "date": "2026-07-20T12:30:00.000Z"},
 {"id": "405505", "title": "Inflation Rate MoM", "indicator": "Inflation Rate",
  "country": "CA", "period": "Jun", "actual": -0.4, "previous": 1.0,
  "forecast": -0.2, "unit": "%", "currency": "CAD", "importance": 0,
  "ticker": "ECONOMICS:CAIRMM", "source": "Statistics Canada",
  "date": "2026-07-20T12:30:00.000Z"},
 {"id": "405504", "title": "Inflation Rate YoY", "indicator": "Inflation Rate",
  "country": "NZ", "period": "Q2", "actual": null, "previous": 2.5,
  "forecast": 2.3, "unit": "%", "currency": "NZD", "importance": 1,
  "date": "2026-07-20T22:45:00.000Z"}
]
"""

FIXTURE: list[dict[str, Any]] = json.loads(_FIXTURE_JSON)
ROWS = normalize_calendar_rows(FIXTURE)

EASTERN_TODAY = datetime.now(ZoneInfo("America/New_York")).date()


def eastern(days_ahead: int) -> str:
    return (EASTERN_TODAY + timedelta(days=days_ahead)).isoformat()


def matched_title(event: dict[str, object]) -> str | None:
    release = match_release(event, ROWS)
    return str(release["matched_title"]) if release else None


# --- matching: agent phrasing lands on the right calendar row ---


@pytest.mark.parametrize(
    ("date", "title", "expected"),
    [
        ("2026-07-16", "US Initial Jobless Claims", "Initial Jobless Claims"),
        (
            "2026-07-16",
            "Philly Fed Manufacturing Index, July",
            "Philadelphia Fed Manufacturing Index",
        ),
        (
            "2026-07-16",
            "US Philadelphia Fed Manufacturing Index (July)",
            "Philadelphia Fed Manufacturing Index",
        ),
        ("2026-07-16", "Retail Sales ex-auto, June", "Retail Sales Ex Autos MoM"),
        ("2026-07-16", "US Retail Sales (June, m/m)", "Retail Sales MoM"),
        ("2026-07-16", "Eurozone Trade Balance (May, s.a.)", "Balance of Trade"),
        # Bare "Jobless Claims" means the weekly initial print; an explicit
        # initial/continuing pairing resolves to initial (higher importance).
        ("2026-07-16", "Initial / Continuing Jobless Claims", "Initial Jobless Claims"),
        # Agent CPI phrasing maps onto TradingView's Inflation Rate family,
        # not the literal index-level "CPI Final" row.
        ("2026-07-17", "Eurozone CPI (June, y/y)", "Inflation Rate YoY Final"),
        (
            "2026-07-17",
            "UMich Consumer Sentiment (July, prel.)",
            "Michigan Consumer Sentiment Prel",
        ),
        # TradingView abbreviates the Conference Board as "CB".
        (
            "2026-07-20",
            "Conference Board US Leading Economic Index, June",
            "CB Leading Index MoM",
        ),
        # Bundle names resolve to the headline series of the report.
        ("2026-07-21", "UK labour market, May", "Unemployment Rate"),
        # Canada is fetched and cued like the majors.
        ("2026-07-20", "Canada CPI, June", "Inflation Rate YoY"),
        # Non-economic rail entries must stay unenriched.
        ("2026-07-16", "Team offsite", None),
        ("2026-07-16", "TSLA earnings", None),
    ],
)
def test_matching_acceptance_cases(date: str, title: str, expected: str | None) -> None:
    assert matched_title({"date": date, "title": title, "time": None}) == expected


def test_quarterly_cpi_skips_monthly_rows_and_lands_on_the_quarter_print() -> None:
    # "CPI (YoY, Q2)" must not enrich from Canada's monthly June print that
    # shares the "Inflation Rate YoY" title; the quarter hint pins it to the
    # NZ Q2 row (and would return null if no quarterly row existed).
    release = match_release(
        {"date": "2026-07-20", "title": "CPI (YoY, Q2)", "time": None}, ROWS
    )
    assert release is not None
    assert release["period"] == "Q2"


def test_mislisted_event_beyond_one_day_stays_unenriched() -> None:
    # The board lists the ECB decision on Jul 16; the real row is Jul 23.
    # The ±1 day hard rule must leave it null rather than grab the far row.
    event = {"date": "2026-07-16", "title": "ECB Interest Rate Decision"}
    assert match_release(event, ROWS) is None
    # Listed one day off, the same title matches.
    assert (
        matched_title({"date": "2026-07-22", "title": "ECB Interest Rate Decision", "time": None})
        == "ECB Interest Rate Decision"
    )


def test_rate_decision_never_enriches_from_a_nearby_speech_row() -> None:
    # Speech rows carry the underlying series as their indicator ("Interest
    # Rate"), which would otherwise outscore nothing and steal the match.
    ecb_next_day = {"date": "2026-07-17", "title": "ECB Interest Rate Decision", "time": None}
    assert matched_title(ecb_next_day) is None
    assert matched_title({"date": "2026-07-15", "title": "FOMC decision", "time": None}) is None


def test_speech_events_match_speech_rows() -> None:
    assert (
        matched_title({"date": "2026-07-17", "title": "ECB Cipollone speaks", "time": None})
        == "ECB Cipollone Speech"
    )


def test_attribution_suffix_is_stripped_before_scoring() -> None:
    # Hermes table titles carry "— <source>" attribution; those tokens
    # diluted the overlap below threshold on the production board.
    assert (
        matched_title(
            {
                "date": "2026-07-17",
                "title": "Industrial Production MoM, Jun — Federal Reserve",
                "time": "15:15 CET",
            }
        )
        == "Industrial Production MoM"
    )
    assert (
        matched_title(
            {
                "date": "2026-07-17",
                "title": "Euro Area CPI Final, Jun — Eurostat / TE",
                "time": "11:00 CET",
            }
        )
        == "Inflation Rate YoY Final"
    )


def test_slash_combined_titles_match_a_twin_print() -> None:
    # "A / B" events score each side alone; the combined token set matches
    # neither row well enough. Either twin is a correct answer; the exact
    # pick is a scoring tiebreak, so only membership is pinned.
    assert matched_title(
        {
            "date": "2026-07-17",
            "title": "Building Permits / Housing Starts, Jun — Census",
            "time": "14:30 CET",
        }
    ) in {"Building Permits Prel", "Housing Starts"}


def test_country_inference_excludes_foreign_same_title_rows() -> None:
    # GB and EU both print Balance of Trade on Jul 16; "Eurozone" in the
    # stored title must pin the EU row (the actual proves which one won).
    release = match_release(
        {"date": "2026-07-16", "title": "Eurozone Trade Balance (May, s.a.)", "time": None}, ROWS
    )
    assert release is not None
    assert release["actual"] == "-7.8 \u20acB"


def test_plain_retail_sales_mom_beats_ex_autos_and_yoy_variants() -> None:
    release = match_release(
        {"date": "2026-07-16", "title": "US Retail Sales (June, m/m)", "time": "14:30 CEST"}, ROWS
    )
    assert release is not None
    assert release["matched_title"] == "Retail Sales MoM"
    assert release["importance"] == 1



def test_explicit_cpi_frequency_exposes_exact_series_and_source() -> None:
    yearly = match_release(
        {
            "date": "2026-07-20",
            "title": "CPI (YoY, June)",
            "time": "14:30 CET",
        },
        ROWS,
    )
    monthly = match_release(
        {
            "date": "2026-07-20",
            "title": "Canada CPI (MoM, June)",
            "time": "14:30 CET",
        },
        ROWS,
    )

    assert yearly is not None
    assert yearly["matched_title"] == "Inflation Rate YoY"
    assert yearly["country"] == "CA"
    assert yearly["actual"] == "2.8%"
    assert yearly["forecast"] == "2.9%"
    assert yearly["previous"] == "3.2%"
    assert yearly["source"] == "Statistics Canada"
    assert yearly["series_url"] == (
        "https://www.tradingview.com/symbols/ECONOMICS-CAIRYY/"
    )
    assert monthly is not None
    assert monthly["matched_title"] == "Inflation Rate MoM"
    assert monthly["actual"] == "-0.4%"
    assert monthly["forecast"] == "-0.2%"
    assert monthly["previous"] == "1%"

# --- release payload: contract fields, display strings, surprise ---


def test_release_payload_contract_fields() -> None:
    release = match_release(
        {"date": "2026-07-16", "title": "Retail Sales ex-auto, June", "time": None}, ROWS
    )
    assert release == {
        "time_utc": "2026-07-16T12:30:00Z",
        "period": "Jun",
        "country": "US",
        "actual": "-0.2%",
        "forecast": "-0.1%",
        "previous": "1%",
        "surprise": -0.1,
        "importance": 0,
        "comment": "Sales of retail goods and services excluding the automobile sector.",
        "matched_title": "Retail Sales Ex Autos MoM",
        "source": None,
        "series_url": None,
    }


def test_unreleased_event_has_null_actual_and_surprise() -> None:
    release = match_release(
        {"date": "2026-07-23", "title": "US Initial Jobless Claims", "time": None}, ROWS
    )
    assert release is not None
    assert release["actual"] is None
    assert release["surprise"] is None
    assert release["previous"] == "208K"


def test_surprise_is_actual_minus_forecast_in_display_units() -> None:
    claims = match_release(
        {"date": "2026-07-16", "title": "US Initial Jobless Claims", "time": None}, ROWS
    )
    assert claims is not None
    assert claims["actual"] == "208K"
    assert claims["forecast"] == "217K"
    assert claims["surprise"] == -9


@pytest.mark.parametrize(
    ("value", "unit", "scale", "expected"),
    [
        (208, None, "K", "208K"),
        (-0.2, "%", None, "-0.2%"),
        (1, "%", None, "1%"),
        (-7.8, "\u20ac", "B", "-7.8 \u20acB"),
        (-1.044, "\u00a3", "B", "-1.044 \u00a3B"),
        (41.4, None, None, "41.4"),
        (None, "%", None, None),
    ],
)
def test_format_display(
    value: float | None, unit: str | None, scale: str | None, expected: str | None
) -> None:
    assert format_display(value, unit=unit, scale=scale) == expected


# --- hot window and adaptive TTL ---


def _row(date: datetime, actual: float | None) -> CalendarRow:
    return normalize_calendar_rows(
        [{"title": "Initial Jobless Claims", "country": "US", "importance": 0,
          "actual": actual, "date": date.strftime("%Y-%m-%dT%H:%M:%S.000Z")}]
    )[0]


def test_hot_window_boundaries() -> None:
    release_time = datetime(2026, 7, 16, 12, 30, tzinfo=UTC)
    assert _is_hot(release_time, None, release_time - HOT_BEFORE)
    assert _is_hot(release_time, None, release_time + HOT_AFTER)
    assert not _is_hot(release_time, None, release_time - HOT_BEFORE - timedelta(seconds=1))
    assert not _is_hot(release_time, None, release_time + HOT_AFTER + timedelta(seconds=1))
    # A printed actual ends the window immediately.
    assert not _is_hot(release_time, 208.0, release_time)


def test_ttl_collapses_while_a_cached_row_is_hot() -> None:
    service = EconCalendarService(cache_seconds=300)
    now = datetime.now(UTC)
    service._rows = [_row(now + timedelta(seconds=60), actual=None)]
    assert service._ttl_seconds() == HOT_TTL_SECONDS

    # Outside the 2-minute pre-window: back to the base TTL.
    service._rows = [_row(now + timedelta(minutes=10), actual=None)]
    assert service._ttl_seconds() == 300.0

    # In-window but already printed: not hot.
    service._rows = [_row(now + timedelta(seconds=60), actual=208.0)]
    assert service._ttl_seconds() == 300.0


def test_any_hot_release_reads_matched_items() -> None:
    now = datetime(2026, 7, 16, 12, 29, tzinfo=UTC)
    hot_item = {"release": {"time_utc": "2026-07-16T12:30:00Z", "actual": None}}
    printed = {"release": {"time_utc": "2026-07-16T12:30:00Z", "actual": "208K"}}
    unmatched = {"release": None}
    assert any_hot_release([unmatched, hot_item], now=now)
    assert not any_hot_release([unmatched, printed], now=now)
    assert not any_hot_release([], now=now)


# --- service: failure keeps the last snapshot behind a cooldown ---


async def test_failed_fetch_keeps_previous_snapshot_and_cools_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EconCalendarService(cache_seconds=300)
    calls = 0

    async def fetch_ok() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return FIXTURE

    monkeypatch.setattr(service, "_fetch", fetch_ok)
    await service.refresh()
    assert calls == 1 and len(service._rows) == len(ROWS)

    # Expire the cache, then break the endpoint: the snapshot must survive.
    service._fetched = time.monotonic() - 301

    async def fetch_boom() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        raise RuntimeError("calendar down")

    monkeypatch.setattr(service, "_fetch", fetch_boom)
    await service.refresh()
    assert len(service._rows) == len(ROWS)

    # Enrichment still works off the stale snapshot during the cooldown,
    # without re-hitting the endpoint on every call.
    items: list[dict[str, object]] = [
        {"date": "2026-07-16", "title": "US Initial Jobless Claims", "time": None}
    ]
    await service.enrich(items)
    release = items[0]["release"]
    assert isinstance(release, dict) and release["actual"] == "208K"
    assert calls == 2


async def test_cache_serves_within_ttl_without_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EconCalendarService(cache_seconds=300)
    calls = 0

    async def fetch_ok() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return FIXTURE

    monkeypatch.setattr(service, "_fetch", fetch_ok)
    await service.refresh()
    await service.refresh()
    assert calls == 1


# --- scheduler broadcast trigger ---


def _items(*releases: dict[str, object] | None) -> list[dict[str, Any]]:
    return [{"id": index, "release": release} for index, release in enumerate(releases)]


def test_broadcast_triggers_on_actual_print_and_matched_set_change() -> None:
    pending = {"matched_title": "Initial Jobless Claims", "actual": None}
    printed = {"matched_title": "Initial Jobless Claims", "actual": "208K"}

    before = _release_state(_items(pending))
    # null -> value on the same match: broadcast.
    assert _release_changed(before, _release_state(_items(printed)))
    # No change: stay quiet.
    assert not _release_changed(before, _release_state(_items(pending)))
    # A newly matched item: broadcast.
    assert _release_changed(before, _release_state(_items(pending, printed)))
    # A value that was already present is not a transition.
    assert not _release_changed(
        _release_state(_items(printed)), _release_state(_items(printed))
    )


# --- route: enrichment attached, outage degrades to the plain payload ---


class StubCalendarService:
    def __init__(self, release: dict[str, object] | None) -> None:
        self.release = release

    async def enrich(self, items: list[dict[str, object]]) -> None:
        for item in items:
            item["release"] = self.release


class BrokenCalendarService:
    async def enrich(self, items: list[dict[str, object]]) -> None:
        raise RuntimeError("calendar down")


@pytest.fixture
def key_dates_app(tmp_path: Path) -> Iterator[Any]:
    """Stub settings + a seeded key_dates row; restore app.state after."""
    had_settings = hasattr(app.state, "settings")
    original = app.state.settings if had_settings else None
    database_path = tmp_path / "board.sqlite3"
    app.state.settings = SimpleNamespace(edit_token="", database_path=database_path)
    db.init_db(database_path)
    db.replace_key_dates(
        database_path,
        slug="macro-brief",
        events=[(eastern(1), "08:30 ET", "US Initial Jobless Claims", "MACRO")],
    )

    yield app.state

    if had_settings:
        app.state.settings = original
    else:
        del app.state.settings
    if hasattr(app.state, "econ_calendar_service"):
        del app.state.econ_calendar_service


def test_route_attaches_release_from_service(key_dates_app: Any) -> None:
    release = {
        "time_utc": "2026-07-16T12:30:00Z",
        "period": "Jul/11",
        "actual": "208K",
        "forecast": "217K",
        "previous": "216K",
        "surprise": -9,
        "importance": 0,
        "comment": None,
        "matched_title": "Initial Jobless Claims",
    }
    key_dates_app.econ_calendar_service = StubCalendarService(release)
    payload = TestClient(app).get("/api/key-dates").json()
    (item,) = payload["key_dates"]
    assert item["title"] == "US Initial Jobless Claims"
    assert item["release"] == release


def test_route_serves_plain_payload_when_enrichment_fails(key_dates_app: Any) -> None:
    key_dates_app.econ_calendar_service = BrokenCalendarService()
    response = TestClient(app).get("/api/key-dates")
    assert response.status_code == 200
    (item,) = response.json()["key_dates"]
    assert item["title"] == "US Initial Jobless Claims"
    assert item["release"] is None


def test_route_serves_plain_payload_without_a_service(key_dates_app: Any) -> None:
    # Lifespan never ran (unit-test style): the key must still be present.
    (item,) = TestClient(app).get("/api/key-dates").json()["key_dates"]
    assert item["release"] is None
