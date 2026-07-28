"""Agent reports: ingest, list previews, read, delete.

Contract: POST /api/reports upserts one markdown report keyed by (slug, date)
— a same-day re-run replaces, a new day appends. The slug defaults to the
slugified title, a missing date defaults to today (UTC), and unslugifiable
input is rejected with 422 "report_slug_invalid". GET /api/reports returns
metadata plus a frontmatter-stripped 220-char preview, newest date first;
GET /api/reports/{id} returns the verbatim body. Both mutation routes are
gated by X-Edit-Token; reads stay open.

The app lifespan starts network pollers, so these tests never run it: the
TestClient is not entered as a context manager and settings are stubbed
directly on app.state with a tmp database path (mirrors test_edit_token).
"""

import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app import db
from app.main import app

TOKEN = "s3cret-edit-token"


@pytest.fixture
def configure_app(tmp_path: Path) -> Iterator[Callable[[str], None]]:
    """Install a stub app.state.settings with a tmp database; restore after."""
    had_settings = hasattr(app.state, "settings")
    original = app.state.settings if had_settings else None

    def _configure(edit_token: str) -> None:
        app.state.settings = SimpleNamespace(
            edit_token=edit_token,
            database_path=tmp_path / "reports.sqlite3",
        )

    yield _configure

    if had_settings:
        app.state.settings = original
    else:
        del app.state.settings


# --- edit-token gate on both mutation routes; reads stay open ---

GATED_REQUESTS = [
    pytest.param("POST", "/api/reports", {"title": "Flows", "body": "text"}, id="create-report"),
    pytest.param("DELETE", "/api/reports/1", None, id="delete-report"),
]


@pytest.mark.parametrize("header", [None, "wrong-token"], ids=["missing-header", "wrong-token"])
@pytest.mark.parametrize(("method", "path", "body"), GATED_REQUESTS)
def test_report_mutations_reject_bad_token_and_persist_nothing(
    configure_app: Callable[[str], None],
    method: str,
    path: str,
    body: dict[str, str] | None,
    header: str | None,
) -> None:
    configure_app(TOKEN)
    client = TestClient(app)
    headers = {"X-Edit-Token": header} if header else {}

    response = client.request(method, path, json=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "edit_token_required"
    # Reads stay open with a token configured, and the rejected mutation left no row.
    listing = client.get("/api/reports")
    assert listing.status_code == 200
    assert listing.json() == {"reports": [], "has_more": False, "filters": []}


def test_report_mutations_accept_exact_token(
    configure_app: Callable[[str], None],
) -> None:
    configure_app(TOKEN)
    client = TestClient(app)

    created = client.post(
        "/api/reports",
        json={"title": "Flows", "body": "text", "date": "2026-07-09"},
        headers={"X-Edit-Token": TOKEN},
    )
    assert created.status_code == 200

    deleted = client.delete(
        f"/api/reports/{created.json()['id']}",
        headers={"X-Edit-Token": TOKEN},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "deleted"}


# --- POST /api/reports: response shape, slug derivation, date default ---


def test_create_report_returns_id_slug_and_date(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)

    response = client.post(
        "/api/reports",
        json={"title": "Morning Flows: BTC & ETH", "body": "text", "date": "2026-07-09"},
    )

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"id", "slug", "date", "key_dates", "fringe_actions"}
    assert isinstance(data["id"], int)
    assert data["slug"] == "morning-flows-btc-eth"
    assert data["date"] == "2026-07-09"


def test_create_report_defaults_missing_date_to_today_utc(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    before = datetime.now(UTC).date().isoformat()

    response = client.post("/api/reports", json={"title": "Flows", "body": "text"})

    after = datetime.now(UTC).date().isoformat()
    assert response.status_code == 200
    assert response.json()["date"] in {before, after}
    [item] = client.get("/api/reports").json()["reports"]
    assert item["date"] == response.json()["date"]


@pytest.mark.parametrize(
    ("slug", "title", "expected"),
    [
        pytest.param(None, "Morning Flows: BTC & ETH!", "morning-flows-btc-eth", id="from-title"),
        pytest.param("Hermes_Daily Flows", "Ignored Title", "hermes-daily-flows", id="explicit"),
        pytest.param(None, "a" * 70, "a" * 64, id="truncated-to-64"),
    ],
)
def test_report_slug_is_normalized(
    configure_app: Callable[[str], None],
    slug: str | None,
    title: str,
    expected: str,
) -> None:
    configure_app("")
    client = TestClient(app)
    payload: dict[str, str] = {"title": title, "body": "text", "date": "2026-07-09"}
    if slug is not None:
        payload["slug"] = slug

    response = client.post("/api/reports", json=payload)

    assert response.status_code == 200
    assert response.json()["slug"] == expected


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"title": "!!!", "body": "text"}, id="unslugifiable-title"),
        pytest.param({"title": "Fine Title", "body": "text", "slug": "!!!"}, id="bad-slug"),
    ],
)
def test_create_report_rejects_unslugifiable_input(
    configure_app: Callable[[str], None],
    payload: dict[str, str],
) -> None:
    configure_app("")
    client = TestClient(app)

    response = client.post("/api/reports", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"] == "report_slug_invalid"
    assert client.get("/api/reports").json()["reports"] == []


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"body": "text"}, id="missing-title"),
        pytest.param({"title": "", "body": "text"}, id="empty-title"),
        pytest.param({"title": "t" * 201, "body": "text"}, id="title-too-long"),
        pytest.param({"title": "Flows", "body": ""}, id="empty-body"),
        pytest.param({"title": "Flows", "body": "x" * 500_001}, id="body-too-long"),
        pytest.param({"title": "Flows", "body": "text", "date": "07/09/2026"}, id="bad-date"),
        pytest.param(
            {"title": "Flows", "body": "text", "date": "2025-02-31"}, id="non-calendar-date"
        ),
        pytest.param({"title": "Flows", "body": "text", "slug": ""}, id="empty-slug"),
        pytest.param({"title": "Flows", "body": "text", "slug": "s" * 65}, id="slug-too-long"),
    ],
)
def test_create_report_rejects_malformed_payload(
    configure_app: Callable[[str], None],
    payload: dict[str, str],
) -> None:
    configure_app("")
    client = TestClient(app)

    response = client.post("/api/reports", json=payload)

    assert response.status_code == 422



def test_report_date_allows_next_session_but_rejects_far_future(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    today = datetime.now(UTC).date()

    accepted = client.post(
        "/api/reports",
        json={
            "title": "Next Session",
            "body": "overnight brief",
            "date": (today + timedelta(days=1)).isoformat(),
        },
    )
    rejected = client.post(
        "/api/reports",
        json={
            "title": "Future Typo",
            "body": "bad date",
            "date": (today + timedelta(days=2)).isoformat(),
        },
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 422
    assert "report date is too far in the future" in rejected.text

# --- upsert semantics keyed by (slug, date) ---


def test_upsert_same_slug_and_date_replaces_body_and_title(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    key = {"slug": "hermes-flows", "date": "2026-07-09"}

    first = client.post("/api/reports", json={"title": "Flows v1", "body": "old body", **key})
    first_created = client.get("/api/reports").json()["reports"][0]["created_at"]
    second = client.post("/api/reports", json={"title": "Flows v2", "body": "new body", **key})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]

    reports = client.get("/api/reports").json()["reports"]
    assert len(reports) == 1
    assert reports[0]["title"] == "Flows v2"
    # The card timestamp is "first landed", not "last repaired": same-day
    # replacements (watchdog repairs, cron re-runs) keep the original stamp.
    assert reports[0]["created_at"] == first_created

    detail = client.get(f"/api/reports/{first.json()['id']}").json()
    assert detail["title"] == "Flows v2"
    assert detail["body"] == "new body"


def test_same_slug_on_new_date_keeps_prior_days_readable(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)

    first = client.post(
        "/api/reports",
        json={"title": "Flows", "body": "monday", "slug": "hermes-flows", "date": "2026-07-08"},
    )
    second = client.post(
        "/api/reports",
        json={"title": "Flows", "body": "tuesday", "slug": "hermes-flows", "date": "2026-07-09"},
    )

    # History is retained newest-first: the library pages back through the
    # archive by id, so yesterday's brief must stay listed and readable.
    reports = client.get("/api/reports").json()["reports"]
    assert [(item["slug"], item["date"]) for item in reports] == [
        ("hermes-flows", "2026-07-09"),
        ("hermes-flows", "2026-07-08"),
    ]
    assert client.get(f"/api/reports/{second.json()['id']}").json()["body"] == "tuesday"
    assert client.get(f"/api/reports/{first.json()['id']}").json()["body"] == "monday"


def test_library_paginates_and_filters_by_slug(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    for day in range(1, 6):
        date = f"2026-07-{day:02d}"
        for slug, title in (("macro-tape", "Macro Tape"), ("fringe-corner", "Fringe Corner")):
            client.post(
                "/api/reports",
                json={"title": title, "body": f"{title} {date}", "slug": slug, "date": date},
            )

    # Facets list every distinct slug regardless of the current page.
    page = client.get("/api/reports?limit=3").json()
    assert [f["slug"] for f in page["filters"]] == ["fringe-corner", "macro-tape"]
    assert len(page["reports"]) == 3
    assert page["has_more"] is True

    # Offset pages continue where the previous page stopped, newest first.
    rest = client.get("/api/reports?limit=200&offset=3").json()
    assert len(rest["reports"]) == 7
    assert rest["has_more"] is False
    dates = [item["date"] for item in page["reports"] + rest["reports"]]
    assert dates == sorted(dates, reverse=True)

    # Slug filter narrows to one brief's history without disturbing facets.
    filtered = client.get("/api/reports?slug=macro-tape").json()
    assert {item["slug"] for item in filtered["reports"]} == {"macro-tape"}
    assert len(filtered["reports"]) == 5
    assert filtered["has_more"] is False
    assert [f["slug"] for f in filtered["filters"]] == ["fringe-corner", "macro-tape"]


def test_older_date_is_archived_without_driving_projections(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)

    client.post(
        "/api/reports",
        json={"title": "Flows", "body": "tuesday", "slug": "hermes-flows", "date": "2026-07-09"},
    )
    # A vault backfill (or a late edit to an older file) uploads with its old
    # date: it lands in the archive under that date but the newest brief keeps
    # the top slot, and projections (key dates, fringe book) never replay from
    # it — its calendar line must not become a key-dates row.
    client.post(
        "/api/reports",
        json={
            "title": "Flows",
            "body": "monday edit\n2027-01-15 - Backfilled Ghost Event\n",
            "slug": "hermes-flows",
            "date": "2026-07-08",
        },
    )

    reports = client.get("/api/reports").json()["reports"]
    assert [(item["slug"], item["date"]) for item in reports] == [
        ("hermes-flows", "2026-07-09"),
        ("hermes-flows", "2026-07-08"),
    ]
    assert client.get("/api/key-dates").json()["key_dates"] == []


def test_concurrent_report_ingests_leave_newest_projections_in_control(
    configure_app: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_app("")
    path = app.state.settings.database_path
    db.init_db(path)
    barrier = threading.Barrier(2)
    original_save = db._save_report

    def synchronized_save(
        conn: sqlite3.Connection,
        *,
        slug: str,
        report_date: str,
        title: str,
        body: str,
    ) -> tuple[int, bool]:
        barrier.wait(timeout=5)
        if report_date == "2026-07-08":
            time.sleep(0.05)
        return original_save(
            conn,
            slug=slug,
            report_date=report_date,
            title=title,
            body=body,
        )

    monkeypatch.setattr(db, "_save_report", synchronized_save)

    def ingest(report_date: str, event_title: str) -> None:
        db.ingest_report(
            path,
            slug="hermes-flows",
            report_date=report_date,
            title="Flows",
            body=event_title,
            events=[("2026-08-01", None, event_title, "macro")],
            fringe_actions=None,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(ingest, "2026-07-08", "Older event"),
            pool.submit(ingest, "2026-07-09", "Newest event"),
        ]
        for future in futures:
            future.result(timeout=10)

    with sqlite3.connect(path) as conn:
        titles = [
            str(row[0])
            for row in conn.execute(
                "SELECT title FROM key_dates WHERE source_slug = ?",
                ("hermes-flows",),
            )
        ]
    assert titles == ["Newest event"]


# --- GET /api/reports: ordering, item shape, limit, previews ---


def _seed_report(client: TestClient, *, date: str, body: str = "text") -> int:
    response = client.post(
        "/api/reports",
        json={"title": f"Report {date}", "body": body, "slug": f"report-{date}", "date": date},
    )
    assert response.status_code == 200
    return int(response.json()["id"])


def test_list_orders_reports_newest_date_first(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    for date in ("2026-07-08", "2026-07-10", "2026-07-09"):
        _seed_report(client, date=date)

    reports = client.get("/api/reports").json()["reports"]

    assert [item["date"] for item in reports] == ["2026-07-10", "2026-07-09", "2026-07-08"]
    # List items carry metadata plus preview; the full body stays on the detail route.
    assert set(reports[0]) == {"id", "slug", "date", "title", "created_at", "preview"}


def test_list_limit_caps_results_to_newest(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    for date in ("2026-07-08", "2026-07-09", "2026-07-10"):
        _seed_report(client, date=date)

    reports = client.get("/api/reports", params={"limit": 1}).json()["reports"]

    assert [item["date"] for item in reports] == ["2026-07-10"]
    assert client.get("/api/reports", params={"limit": 0}).status_code == 422
    assert client.get("/api/reports", params={"limit": 201}).status_code == 422


def test_list_preview_strips_frontmatter_and_markdown_noise(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    body = (
        "---\n"
        "kind: hermes-flows\n"
        "secret: do-not-leak\n"
        "---\n"
        "## Net **flows**\n"
        "\n"
        "- BTC saw `strong` spot inflows\n"
        "> risk _tight_\n"
    )
    client.post("/api/reports", json={"title": "Flows", "body": body, "date": "2026-07-09"})

    [item] = client.get("/api/reports").json()["reports"]

    assert item["preview"] == "Net flows - BTC saw strong spot inflows risk tight"
    assert "do-not-leak" not in item["preview"]


def test_list_preview_truncates_to_220_chars_with_ellipsis(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    client.post(
        "/api/reports",
        json={"title": "Long", "body": "x" * 221, "slug": "long", "date": "2026-07-09"},
    )
    client.post(
        "/api/reports",
        json={"title": "Exact", "body": "y" * 220, "slug": "exact", "date": "2026-07-09"},
    )

    previews = {
        item["slug"]: item["preview"] for item in client.get("/api/reports").json()["reports"]
    }

    assert previews["long"] == "x" * 220 + "…"
    assert previews["exact"] == "y" * 220


def test_list_bounds_large_report_body_while_detail_remains_verbatim(
    configure_app: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_app("")
    client = TestClient(app)
    prefix = (
        "---\n"
        "kind: large-report\n"
        "---\n"
        "# Meaningful **preview**\n"
    )
    body = prefix + "x" * (500_000 - len(prefix))
    created = client.post(
        "/api/reports",
        json={"title": "Large", "body": body, "date": "2026-07-09"},
    )
    report_id = int(created.json()["id"])
    statements: list[str] = []
    original_connect = db._connect

    @contextmanager
    def traced_connect(path: Path) -> Iterator[sqlite3.Connection]:
        with original_connect(path) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(db, "_connect", traced_connect)

    [item] = client.get("/api/reports").json()["reports"]
    list_statements = list(statements)
    detail = client.get(f"/api/reports/{report_id}").json()

    expected_text = "Meaningful preview " + "x" * 500_000
    assert item["preview"] == expected_text[:220] + "…"
    assert any(
        "SUBSTR(BODY, 1, 16384) AS BODY" in statement.upper()
        for statement in list_statements
    )
    assert detail["body"] == body


# --- GET /api/reports/{id} and DELETE /api/reports/{id} ---


def test_read_report_returns_verbatim_body_and_cleaned_title(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    body = "---\nkind: hermes\n---\n# Kept **verbatim** in the reader\n"
    created = client.post(
        "/api/reports",
        json={
            "title": "  Hermes   Flows  ",
            "body": body,
            "slug": "hermes-flows",
            "date": "2026-07-09",
        },
    )

    detail = client.get(f"/api/reports/{created.json()['id']}")

    assert detail.status_code == 200
    data = detail.json()
    assert set(data) == {"id", "slug", "date", "title", "created_at", "body"}
    # Frontmatter stripping applies to list previews only; the reader gets the raw markdown.
    assert data["body"] == body
    assert data["title"] == "Hermes Flows"
    assert data["slug"] == "hermes-flows"
    assert data["date"] == "2026-07-09"


def test_read_unknown_report_returns_404(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)

    response = client.get("/api/reports/999")

    assert response.status_code == 404
    assert response.json()["detail"] == "report_not_found"


def test_delete_removes_report_and_unknown_delete_404s(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    report_id = _seed_report(client, date="2026-07-09")

    deleted = client.delete(f"/api/reports/{report_id}")

    assert deleted.status_code == 200
    assert deleted.json() == {"status": "deleted"}
    assert client.get(f"/api/reports/{report_id}").status_code == 404
    assert client.get("/api/reports").json()["reports"] == []

    again = client.delete(f"/api/reports/{report_id}")
    assert again.status_code == 404
    assert again.json()["detail"] == "report_not_found"


def test_deleting_archived_report_does_not_clear_current_key_dates(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    older = client.post(
        "/api/reports",
        json={
            "title": "Macro Tape",
            "slug": "macro-tape",
            "date": "2026-07-08",
            "body": "## Key Dates\n- 2026-08-20 - Older Event\n",
        },
    )
    newer = client.post(
        "/api/reports",
        json={
            "title": "Macro Tape",
            "slug": "macro-tape",
            "date": "2026-07-09",
            "body": "## Key Dates\n- 2026-08-21 - Current Event\n",
        },
    )
    assert older.status_code == newer.status_code == 200
    assert [item["title"] for item in client.get("/api/key-dates").json()["key_dates"]] == [
        "Current Event"
    ]

    deleted = client.delete(f"/api/reports/{older.json()['id']}")

    assert deleted.status_code == 200
    assert [item["title"] for item in client.get("/api/key-dates").json()["key_dates"]] == [
        "Current Event"
    ]


def test_report_ingest_rolls_back_when_a_projection_fails(
    configure_app: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_app("")

    def fail_projection(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("forced calendar projection failure")

    monkeypatch.setattr(db, "_replace_key_dates", fail_projection)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/reports",
        json={
            "title": "Macro Tape Brief",
            "date": "2026-07-21",
            "body": "## Key Dates\n- 2026-07-22 08:30 ET — CPI [MACRO]",
        },
    )

    assert response.status_code == 500
    assert db.load_reports(app.state.settings.database_path, 10)["reports"] == []


# --- db helper: the 40-line frontmatter scan window ---


@pytest.mark.parametrize(
    ("body", "stripped"),
    [
        pytest.param("# heading\ntext", False, id="no-frontmatter"),
        pytest.param("---\nkind: x\n---\ncontent", True, id="closed-block"),
        pytest.param("---\nkind: x\nno closing", False, id="unclosed-block"),
        pytest.param("---\n" + "k: v\n" * 38 + "---\ncontent", True, id="closing-at-line-39"),
        pytest.param("---\n" + "k: v\n" * 39 + "---\ncontent", False, id="closing-at-line-40"),
    ],
)
def test_strip_frontmatter_scans_only_the_first_40_lines(body: str, stripped: bool) -> None:
    result = db._strip_frontmatter(body)

    if stripped:
        assert result == "content"
    else:
        assert result == body
