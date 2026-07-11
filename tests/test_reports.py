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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
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
    assert listing.json() == {"reports": []}


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
    assert set(data) == {"id", "slug", "date"}
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
    assert client.get("/api/reports").json() == {"reports": []}


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


# --- upsert semantics keyed by (slug, date) ---


def test_upsert_same_slug_and_date_replaces_body_and_title(
    configure_app: Callable[[str], None],
) -> None:
    configure_app("")
    client = TestClient(app)
    key = {"slug": "hermes-flows", "date": "2026-07-09"}

    first = client.post("/api/reports", json={"title": "Flows v1", "body": "old body", **key})
    second = client.post("/api/reports", json={"title": "Flows v2", "body": "new body", **key})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]

    reports = client.get("/api/reports").json()["reports"]
    assert len(reports) == 1
    assert reports[0]["title"] == "Flows v2"

    detail = client.get(f"/api/reports/{first.json()['id']}").json()
    assert detail["title"] == "Flows v2"
    assert detail["body"] == "new body"


def test_same_slug_on_new_date_creates_second_report(
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

    assert second.json()["id"] != first.json()["id"]
    reports = client.get("/api/reports").json()["reports"]
    assert [item["date"] for item in reports] == ["2026-07-09", "2026-07-08"]
    assert client.get(f"/api/reports/{first.json()['id']}").json()["body"] == "monday"
    assert client.get(f"/api/reports/{second.json()['id']}").json()["body"] == "tuesday"


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
    assert client.get("/api/reports").json() == {"reports": []}

    again = client.delete(f"/api/reports/{report_id}")
    assert again.status_code == 404
    assert again.json()["detail"] == "report_not_found"


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
