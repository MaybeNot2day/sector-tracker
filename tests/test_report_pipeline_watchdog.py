"""Behavioral tests for the Hermes report delivery watchdog."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_UPLOADER_SPEC = importlib.util.spec_from_file_location(
    "vault_report_uploader", _SCRIPTS / "vault_report_uploader.py"
)
assert _UPLOADER_SPEC is not None and _UPLOADER_SPEC.loader is not None
uploader = importlib.util.module_from_spec(_UPLOADER_SPEC)
sys.modules["vault_report_uploader"] = uploader
_UPLOADER_SPEC.loader.exec_module(uploader)

_WATCHDOG_SPEC = importlib.util.spec_from_file_location(
    "report_pipeline_watchdog", _SCRIPTS / "report_pipeline_watchdog.py"
)
assert _WATCHDOG_SPEC is not None and _WATCHDOG_SPEC.loader is not None
watchdog = importlib.util.module_from_spec(_WATCHDOG_SPEC)
sys.modules["report_pipeline_watchdog"] = watchdog
_WATCHDOG_SPEC.loader.exec_module(watchdog)

DATE_TEXT = "2026-07-22"
NOW = datetime(2026, 7, 22, 12, 30, tzinfo=UTC)


def _body(title: str) -> str:
    frontmatter = (
        f"---\ndate: {DATE_TEXT}\ntype: research\n"
        "tags: [daily-brief, market-brief]\nstatus: draft\n---\n"
    )
    sections = {
        "AI Semis Morning Brief": (
            "## AI Semis Morning Brief\nMarket analysis.\n"
            "---FEED-STATUS---\n## Feed Status\n| Feed | Status |\n"
        ),
        "Biotech Pharma Brief": (
            "## Biotech Pharma Brief\nQuiet but complete.\n"
            "---FEED-STATUS---\n## Feed Status\n| Feed | Status |\n"
        ),
        "US Asia Close": (
            "## Executive Tape Read\nAnalysis.\n## Today's Calendar\n"
            "| Time | Event |\n---FEED-STATUS---\n## Feed Status\n"
        ),
        "Macro Tape Brief": "## Macro Tape Brief\nAnalysis.\n## Feed Status\n",
        "Fringe Corner": (
            "## Fringe Corner\n"
            "- HOLD LONG AMD - thesis intact [horizon: 2w]\n"
            "## Rationale\nEvidence.\n"
        ),
    }
    return frontmatter + sections[title]


def _write_due_reports(vault: Path, titles: list[str]) -> dict[str, str]:
    bodies = {title: _body(title) for title in titles}
    for title, body in bodies.items():
        (vault / f"{DATE_TEXT} {title}.md").write_text(body, encoding="utf-8")
    return bodies


def _dashboard_stub(
    bodies: dict[str, str], calls: list[str]
) -> Any:
    ids = {title: index for index, title in enumerate(bodies, start=1)}

    def get_json(url: str) -> dict[str, Any]:
        calls.append(url)
        if url.endswith("/api/reports?limit=20"):
            return {
                "reports": [
                    {"id": ids[title], "title": title, "date": DATE_TEXT}
                    for title in bodies
                ]
            }
        if url.endswith("/api/fringe"):
            return {
                "summary": {"open_count": 1},
                "open": [{"ticker": "AMD", "last_mentioned": DATE_TEXT}],
            }
        report_id = int(url.rsplit("/", 1)[-1])
        title = next(title for title, item_id in ids.items() if item_id == report_id)
        return {"body": bodies[title]}

    return get_json


def test_audit_accepts_complete_end_to_end_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    titles = [stage.title for stage in watchdog.STAGES]
    bodies = _write_due_reports(vault, titles)
    upload_state = {
        f"{DATE_TEXT} {title}.md": uploader.content_hash(body)
        for title, body in bodies.items()
    }
    calls: list[str] = []
    monkeypatch.setattr(
        watchdog,
        "load_config",
        lambda: {
            "BOARD_URL": "https://board.test",
            "VAULT_DIR": str(vault),
        },
    )
    monkeypatch.setattr(watchdog, "load_state", lambda: upload_state)
    monkeypatch.setattr(
        watchdog,
        "_run_uploader",
        lambda: pytest.fail("uploader should not run for matching hashes"),
    )
    monkeypatch.setattr(watchdog, "_get_json", _dashboard_stub(bodies, calls))

    assert watchdog.audit_pipeline(NOW) == []
    assert calls[0].endswith("/api/reports?limit=20")
    assert calls[-1].endswith("/api/fringe")
    assert len(calls) == len(titles) + 2


def test_audit_repairs_an_unuploaded_valid_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    titles = ["AI Semis Morning Brief", "Biotech Pharma Brief"]
    bodies = _write_due_reports(vault, titles)
    upload_state: dict[str, str] = {}
    repairs: list[bool] = []

    def repair() -> None:
        repairs.append(True)
        upload_state.update(
            {
                f"{DATE_TEXT} {title}.md": uploader.content_hash(body)
                for title, body in bodies.items()
            }
        )
        return None

    monkeypatch.setattr(
        watchdog,
        "load_config",
        lambda: {
            "BOARD_URL": "https://board.test",
            "VAULT_DIR": str(vault),
        },
    )
    monkeypatch.setattr(watchdog, "load_state", lambda: upload_state)
    monkeypatch.setattr(watchdog, "_run_uploader", repair)
    monkeypatch.setattr(watchdog, "_get_json", _dashboard_stub(bodies, []))

    early = datetime(2026, 7, 22, 7, 30, tzinfo=UTC)
    assert watchdog.audit_pipeline(early) == []
    assert repairs == [True]


def test_audit_reports_missing_due_file_without_suppressing_other_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bodies = _write_due_reports(vault, ["AI Semis Morning Brief"])
    upload_state = {
        f"{DATE_TEXT} AI Semis Morning Brief.md": uploader.content_hash(
            bodies["AI Semis Morning Brief"]
        )
    }
    monkeypatch.setattr(
        watchdog,
        "load_config",
        lambda: {
            "BOARD_URL": "https://board.test",
            "VAULT_DIR": str(vault),
        },
    )
    monkeypatch.setattr(watchdog, "load_state", lambda: upload_state)
    monkeypatch.setattr(watchdog, "_get_json", _dashboard_stub(bodies, []))

    early = datetime(2026, 7, 22, 7, 30, tzinfo=UTC)
    issues = watchdog.audit_pipeline(early)

    assert len(issues) == 1
    assert issues[0].startswith("Biotech Pharma Brief: vault file missing/unreadable")


def test_alerts_are_edge_triggered_and_send_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "watchdog.json"
    sent: list[tuple[str, str, str]] = []

    def send(target: str, subject: str, message: str) -> None:
        sent.append((target, subject, message))
        return None

    monkeypatch.setattr(watchdog, "_send_alert", send)
    issues = ["Macro Tape Brief: vault file missing"]

    assert watchdog.reconcile_alerts(issues, DATE_TEXT, "telegram", state_path) is None
    assert watchdog.reconcile_alerts(issues, DATE_TEXT, "telegram", state_path) is None
    assert watchdog.reconcile_alerts([], DATE_TEXT, "telegram", state_path) is None

    assert len(sent) == 2
    assert sent[0][1] == "[Sector Tracker pipeline]"
    assert sent[1][1] == "[Sector Tracker pipeline recovered]"
