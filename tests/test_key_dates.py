"""Key dates: parse from agent reports, mirror per slug, serve upcoming.

Contract: any section whose heading mentions "calendar" or "key dates"
feeds the board. Hermes-style markdown tables (when-cell, event-cell)
resolve dates against the report date — subheadings pin dates, weekday
and month-day tokens roll forward — and a timezone in the section heading
suffixes bare times. Explicit bullets follow ``- YYYY-MM-DD [time] —
Title [CATEGORY]``. Prose and malformed rows are skipped, never rejected.
POST /api/reports replaces the slug's previous key dates wholesale (a
re-run without a calendar clears them), DELETE /api/reports/{id} drops
the slug's rows, and GET /api/key-dates returns events from the
US-Eastern today forward, soonest first, NULL times after timed prints.

Like test_reports, the app lifespan never runs: settings are stubbed on
app.state with a tmp database path and TestClient is not entered.
"""

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from app import db
from app.main import app
from app.services.key_dates import MAX_EVENTS, KeyDate, parse_key_dates

EASTERN_TODAY = datetime.now(ZoneInfo("America/New_York")).date()


def eastern(days_ahead: int) -> str:
    return (EASTERN_TODAY + timedelta(days=days_ahead)).isoformat()


@pytest.fixture
def configure_app(tmp_path: Path) -> Iterator[Callable[[str], None]]:
    """Install a stub app.state.settings with a tmp database; restore after."""
    had_settings = hasattr(app.state, "settings")
    original = app.state.settings if had_settings else None

    def _configure(edit_token: str) -> None:
        app.state.settings = SimpleNamespace(
            edit_token=edit_token,
            database_path=tmp_path / "key_dates.sqlite3",
        )

    yield _configure

    if had_settings:
        app.state.settings = original
    else:
        del app.state.settings


# --- parser: section detection ---


def test_bullets_outside_a_key_dates_section_are_ignored() -> None:
    body = (
        "## Watch Today\n"
        "- 2026-07-15 08:30 ET — PPI [MACRO]\n"
        "## Key Dates\n"
        "- 2026-07-20 — JPX closed — Marine Day [HOLIDAY]\n"
        "## Positioning\n"
        "- 2026-07-22 — TSLA earnings [EARNINGS]\n"
    )
    events = parse_key_dates(body)
    assert events == [
        KeyDate(date="2026-07-20", time=None, title="JPX closed — Marine Day", category="HOLIDAY")
    ]


def test_heading_match_is_case_insensitive_and_level_agnostic() -> None:
    body = "### UPCOMING KEY DATES\n- 2026-08-01 — FOMC blackout begins\n"
    events = parse_key_dates(body)
    assert len(events) == 1
    assert events[0].category == "EVENT"  # default when no tag


def test_report_without_key_dates_section_yields_nothing() -> None:
    assert parse_key_dates("## Overnight Highlights\n- 2026-07-15 — PPI\n") == []


# --- parser: bullet grammar ---


@pytest.mark.parametrize(
    ("bullet", "expected"),
    [
        pytest.param(
            "- 2026-07-15 08:30 ET — PPI — Producer Price Index (June) [MACRO]",
            KeyDate("2026-07-15", "08:30 ET", "PPI — Producer Price Index (June)", "MACRO"),
            id="time-tz-emdash-category",
        ),
        pytest.param(
            "- **2026-07-17** - US monthly options expiration (opex) [OPEX]",
            KeyDate("2026-07-17", None, "US monthly options expiration (opex)", "OPEX"),
            id="bold-date-hyphen",
        ),
        pytest.param(
            "* 2026-07-22 AMC: TSLA earnings (EARNINGS)",
            KeyDate("2026-07-22", "AMC", "TSLA earnings", "EARNINGS"),
            id="star-bullet-session-token-colon-paren-tag",
        ),
        pytest.param(
            "1. 2026-07-16 22:00 – CN — Retail Sales",
            KeyDate("2026-07-16", "22:00", "CN — Retail Sales", "EVENT"),
            id="numbered-endash-no-category",
        ),
        pytest.param(
            "- 2026-07-16 — ARB unlock — 92.6M ARB (1.4% of circ supply) [crypto]",
            KeyDate("2026-07-16", None, "ARB unlock — 92.6M ARB (1.4% of circ supply)", "CRYPTO"),
            id="lowercase-tag-uppercased-parens-kept-in-title",
        ),
    ],
)
def test_bullet_grammar(bullet: str, expected: KeyDate) -> None:
    assert parse_key_dates(f"## Key Dates\n{bullet}\n") == [expected]


@pytest.mark.parametrize(
    "bullet",
    [
        pytest.param("- 2026-02-31 — impossible date", id="non-calendar-date"),
        pytest.param("- July 15 — PPI print", id="prose-date"),
        pytest.param("- 2026-07-15 PPI without separator", id="missing-separator"),
        pytest.param("- watch the 30Y auction closely", id="plain-prose"),
    ],
)
def test_malformed_bullets_are_skipped_not_fatal(bullet: str) -> None:
    body = f"## Key Dates\n{bullet}\n- 2026-07-18 — Valid event\n"
    events = parse_key_dates(body)
    assert [event.title for event in events] == ["Valid event"]


def test_duplicate_date_title_pairs_collapse_and_cap_applies() -> None:
    bullets = ["- 2026-07-15 — Same event [MACRO]", "- 2026-07-15 — same EVENT [CRYPTO]"]
    bullets += [f"- 2026-08-01 — Event {i}" for i in range(MAX_EVENTS + 20)]
    events = parse_key_dates("## Key Dates\n" + "\n".join(bullets))
    assert len(events) == MAX_EVENTS
    # First mention wins the duplicate; its category survives.
    assert events[0] == KeyDate("2026-07-15", None, "Same event", "MACRO")


# --- parser: Hermes economic-calendar tables ---


MACRO_BRIEF = """\
## Material Stories — Past 18 Hours

| Feed | Items |
|---|---|
| BloombergMarkets | 30 |

## Economic Calendar (CEST)

### Tuesday, July 14, 2026

| Time (CEST) | Event | Forecast | Implication if Above/Below |
|---|---|---|---|
| **09:30** | UK S&P Global Composite PMI Flash (July) | 49.3 | Below 50 = contraction. |
| **14:30** | **US June CPI (M-o-M)** | **-0.1%** | Hot print = risk-off. |
| **Monday Jul 13** | — | No high-impact releases scheduled | — |

### Wednesday, July 15, 2026

| Time (CEST) | Event | Forecast | Implication |
|---|---|---|---|
| **16:00** | Fed Chair Warsh testimony, Senate Banking | — | Second chance to reprice July. |

## Key Narrative

| Time (CEST) | Event |
|---|---|
| 09:00 | Table outside the calendar section must not feed the board |
"""


def test_hermes_calendar_table_pins_dates_from_subheadings() -> None:
    events = parse_key_dates(MACRO_BRIEF, default_date="2026-07-14")
    assert events == [
        KeyDate("2026-07-14", "09:30 CEST", "UK S&P Global Composite PMI Flash (July)", "MACRO"),
        KeyDate("2026-07-14", "14:30 CEST", "US June CPI (M-o-M)", "MACRO"),
        KeyDate("2026-07-15", "16:00 CEST", "Fed Chair Warsh testimony, Senate Banking", "MACRO"),
    ]
    # Bold markers stripped, header/separator/placeholder rows skipped, and
    # the Key Narrative table (same-level heading closed the section) ignored.


ASIA_CLOSE = """\
### Today's Calendar — CET

_Source note: times converted from UTC._

| CET Time | Event | Region | Implication |
|---:|---|---|---|
| 14:30 | June CPI / Core CPI | US | Above consensus = risk-off. |
| Tue, pre/open | Major bank earnings: JPM, BAC, C | US | Credit quality tone. |
| 11:00 Wed | Euro Area industrial production | Euro Area | Weak = ECB caution. |
| Wed Asia session, exact time not verified | China Q2 GDP / activity data | China | — |
| Thu Jul 16 14:30 | US June Retail Sales | US | Consumer read. |

### Key Narrative

Prose follows.
"""


def test_asia_close_weekday_and_month_day_tokens_resolve_forward() -> None:
    # Report date 2026-07-14 is a Tuesday.
    events = parse_key_dates(ASIA_CLOSE, default_date="2026-07-14")
    assert events == [
        KeyDate("2026-07-14", "14:30 CET", "June CPI / Core CPI", "MACRO"),
        KeyDate("2026-07-14", None, "Major bank earnings: JPM, BAC, C", "EARNINGS"),
        KeyDate("2026-07-15", "11:00 CET", "Euro Area industrial production", "MACRO"),
        KeyDate("2026-07-15", None, "China Q2 GDP / activity data", "MACRO"),
        KeyDate("2026-07-16", "14:30 CET", "US June Retail Sales", "MACRO"),
    ]
    # "Tue" on the report's own weekday = today; "exact time not verified"
    # containing the word "time" must not read as a table header; the
    # month-day token wins over its (possibly wrong) weekday prefix.


def test_yearless_month_day_rolls_into_next_year() -> None:
    body = "## Economic Calendar\n\n| When | Event |\n|---|---|\n| Jan 5 09:00 | FOMC minutes |\n"
    events = parse_key_dates(body, default_date="2026-12-30")
    assert events == [KeyDate("2027-01-05", "09:00", "FOMC minutes", "MACRO")]


def test_table_rows_without_anchor_need_explicit_dates() -> None:
    body = (
        "## Economic Calendar\n\n"
        "| When | Event |\n|---|---|\n"
        "| 14:30 Wed | Dropped without an anchor |\n"
        "| 2026-07-15 14:30 | Kept via explicit ISO date |\n"
    )
    events = parse_key_dates(body)
    assert events == [KeyDate("2026-07-15", "14:30", "Kept via explicit ISO date", "MACRO")]


def test_untagged_titles_infer_category() -> None:
    body = (
        "## Key Dates\n"
        "- 2026-07-22 AMC — TSLA earnings\n"
        "- 2026-07-17 — US monthly options expiration (opex)\n"
        "- 2026-07-20 — JPX closed — Marine Day\n"
        "- 2026-07-15 — STRK unlock — 226.4M STRK\n"
    )
    assert [event.category for event in parse_key_dates(body)] == [
        "EARNINGS",
        "OPEX",
        "HOLIDAY",
        "CRYPTO",
    ]


# --- DB: replace-per-slug mirrors the newest report ---


def test_replace_key_dates_upserts_cross_slug_collisions(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    db.replace_key_dates(
        path, slug="macro-brief", events=[("2026-07-15", "08:30 ET", "PPI", "MACRO")]
    )
    # A second brief mentions the same real-world event: one calendar row,
    # newest mention owns it.
    db.replace_key_dates(
        path, slug="us-open-brief", events=[("2026-07-15", "08:30", "PPI", "EVENT")]
    )
    rows = db.load_key_dates(path, start="2026-01-01", end="2026-12-31", limit=10)
    assert len(rows) == 1
    assert rows[0]["time"] == "08:30"
    assert rows[0]["source_slug"] == "us-open-brief"
    # The first slug re-runs without the event: the shared row now belongs
    # to the other slug and must survive.
    db.replace_key_dates(path, slug="macro-brief", events=[])
    rows = db.load_key_dates(path, start="2026-01-01", end="2026-12-31", limit=10)
    assert len(rows) == 1


def test_load_key_dates_orders_timed_prints_before_all_day_items(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    db.replace_key_dates(
        path,
        slug="brief",
        events=[
            ("2026-07-16", None, "ARB unlock", "CRYPTO"),
            ("2026-07-16", "08:30 ET", "Retail Sales", "MACRO"),
            ("2026-07-15", None, "STRK unlock", "CRYPTO"),
        ],
    )
    rows = db.load_key_dates(path, start="2026-07-15", end="2026-08-15", limit=10)
    assert [(row["date"], row["title"]) for row in rows] == [
        ("2026-07-15", "STRK unlock"),
        ("2026-07-16", "Retail Sales"),
        ("2026-07-16", "ARB unlock"),
    ]


# --- API: ingest feeds the calendar; the newest report per slug wins ---


REPORT_BODY = (
    "Overnight tape was quiet.\n\n"
    "## Key Dates\n\n"
    f"- {eastern(1)} 08:30 ET — PPI — Producer Price Index [MACRO]\n"
    f"- {eastern(3)} — US monthly options expiration [OPEX]\n"
    f"- {eastern(-2)} — CPI (already printed) [MACRO]\n"
)


def test_ingest_report_feeds_upcoming_key_dates(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)

    created = client.post("/api/reports", json={"title": "Macro Tape Brief", "body": REPORT_BODY})
    assert created.status_code == 200
    assert created.json()["key_dates"] == 3

    payload = client.get("/api/key-dates").json()
    assert payload["as_of"] == EASTERN_TODAY.isoformat()
    # The past print is stored but not served.
    assert [
        (item["date"], item["title"], item["time"], item["category"])
        for item in payload["key_dates"]
    ] == [
        (eastern(1), "PPI — Producer Price Index", "08:30 ET", "MACRO"),
        (eastern(3), "US monthly options expiration", None, "OPEX"),
    ]
    assert all(item["source_slug"] == "macro-tape-brief" for item in payload["key_dates"])


def test_reingest_replaces_slug_rows_and_days_window_filters(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    client.post("/api/reports", json={"title": "Macro Tape Brief", "body": REPORT_BODY})

    # Next day's brief drops PPI/opex, adds a near FOMC and a far-out event.
    new_body = (
        "## Key Dates\n"
        f"- {eastern(2)} — FOMC decision [MACRO]\n"
        f"- {eastern(200)} — Treasury refunding announcement\n"
    )
    client.post("/api/reports", json={"title": "Macro Tape Brief", "body": new_body})

    # Default 90-day window: replaced rows are gone, the far event is out of range.
    titles = [item["title"] for item in client.get("/api/key-dates").json()["key_dates"]]
    assert titles == ["FOMC decision"]

    wide = client.get("/api/key-dates?days=365").json()["key_dates"]
    assert [item["title"] for item in wide] == [
        "FOMC decision",
        "Treasury refunding announcement",
    ]


def test_deleting_a_report_clears_its_calendar_rows(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    created = client.post("/api/reports", json={"title": "Macro Tape Brief", "body": REPORT_BODY})

    deleted = client.delete(f"/api/reports/{created.json()['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/key-dates").json()["key_dates"] == []


def test_report_without_section_clears_previous_key_dates(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    client.post("/api/reports", json={"title": "Macro Tape Brief", "body": REPORT_BODY})

    client.post(
        "/api/reports",
        json={"title": "Macro Tape Brief", "body": "Quiet day; nothing scheduled."},
    )

    assert client.get("/api/key-dates").json()["key_dates"] == []
