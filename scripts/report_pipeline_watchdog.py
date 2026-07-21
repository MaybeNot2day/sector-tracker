#!/usr/bin/env python3
"""Verify each weekday report reached the vault, uploader, dashboard, and Fringe ledger."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, cast

from vault_report_uploader import (
    CONFIG_PATH,
    content_hash,
    load_config,
    load_state,
    validate_report_body,
)

ALERT_STATE_PATH = (
    Path.home() / ".local/state/sector-tracker/report-pipeline-watchdog.json"
)
CONTRACT_EFFECTIVE_DATE = date(2026, 7, 22)
REQUEST_TIMEOUT = 20


@dataclass(frozen=True)
class Stage:
    title: str
    deadline: time


STAGES = (
    Stage("AI Semis Morning Brief", time(7, 20)),
    Stage("Biotech Pharma Brief", time(7, 20)),
    Stage("US Asia Close", time(9, 20)),
    Stage("Macro Tape Brief", time(11, 50)),
    Stage("Fringe Corner", time(12, 20)),
)


def _get_json(url: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("dashboard watchdog requires an HTTPS BOARD_URL")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # nosec B310
        return cast(dict[str, Any], json.loads(response.read().decode("utf-8")))


def _save_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, encoding="utf-8", delete=False
    ) as tmp:
        json.dump(payload, tmp, indent=1, sort_keys=True)
    os.replace(tmp.name, path)


def _load_alert_state(path: Path = ALERT_STATE_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, Any], payload) if isinstance(payload, dict) else {}


def _run_uploader() -> str | None:
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "start",
            "sector-tracker-uploader.service",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout).strip()
    return f"uploader repair failed ({detail or f'exit {result.returncode}'})"


def audit_pipeline(now: datetime | None = None) -> list[str]:
    current = now or datetime.now(UTC)
    if current.weekday() >= 5:
        return []
    due = [stage for stage in STAGES if current.time() >= stage.deadline]
    if not due:
        return []

    config = load_config()
    base_url = config.get("BOARD_URL", "").rstrip("/")
    vault = Path(config.get("VAULT_DIR") or Path.home() / "hermes-research")
    date_text = current.date().isoformat()
    issues: list[str] = []
    bodies: dict[str, str] = {}

    if not base_url:
        return [f"missing BOARD_URL in {CONFIG_PATH}"]
    if not vault.is_dir():
        return [f"vault directory not found: {vault}"]

    upload_state = load_state()
    needs_uploader = False
    for stage in due:
        filename = f"{date_text} {stage.title}.md"
        path = vault / filename
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(f"{stage.title}: vault file missing/unreadable ({exc})")
            continue
        if not body.strip():
            issues.append(f"{stage.title}: vault file is empty")
            continue
        if current.date() >= CONTRACT_EFFECTIVE_DATE:
            violation = validate_report_body(stage.title, date_text, body)
            if violation:
                issues.append(f"{stage.title}: invalid report contract ({violation})")
                continue
        bodies[stage.title] = body
        if upload_state.get(filename) != content_hash(body):
            needs_uploader = True

    if needs_uploader:
        repair_error = _run_uploader()
        if repair_error:
            issues.append(repair_error)
        upload_state = load_state()

    for stage in due:
        body = bodies.get(stage.title)
        if body is None:
            continue
        filename = f"{date_text} {stage.title}.md"
        if upload_state.get(filename) != content_hash(body):
            issues.append(f"{stage.title}: uploader state does not match vault content")

    try:
        listing = _get_json(base_url + "/api/reports?limit=20")
        reports = listing.get("reports", [])
        latest_by_title = {
            str(item.get("title")): item for item in reports if isinstance(item, dict)
        }
    except (OSError, ValueError, urllib.error.URLError) as exc:
        issues.append(f"dashboard report listing unavailable ({exc})")
        latest_by_title = {}

    for stage in due:
        body = bodies.get(stage.title)
        if body is None or not latest_by_title:
            continue
        report = latest_by_title.get(stage.title)
        if report is None:
            issues.append(f"{stage.title}: absent from dashboard report listing")
            continue
        report_date = str(report.get("date") or "")
        if report_date < date_text:
            issues.append(
                f"{stage.title}: dashboard is stale ({report_date or 'missing date'})"
            )
            continue
        # A newer report can legitimately replace an earlier slug. The uploader
        # hash above proves this day's file was accepted before replacement.
        if report_date > date_text:
            continue
        try:
            detail = _get_json(base_url + f"/api/reports/{int(report['id'])}")
        except (KeyError, TypeError, ValueError, OSError, urllib.error.URLError) as exc:
            issues.append(f"{stage.title}: dashboard detail unavailable ({exc})")
            continue
        if detail.get("body") != body:
            issues.append(f"{stage.title}: dashboard body differs from vault content")

    if any(stage.title == "Fringe Corner" for stage in due):
        try:
            fringe = _get_json(base_url + "/api/fringe")
            open_ideas = fringe.get("open", [])
            summary = fringe.get("summary", {})
            if not isinstance(open_ideas, list) or not isinstance(summary, dict):
                raise ValueError("malformed Fringe payload")
            if summary.get("open_count") != len(open_ideas):
                issues.append("Fringe Corner: ledger open_count is inconsistent")
            stale_mentions = [
                str(idea.get("ticker") or "?")
                for idea in open_ideas
                if isinstance(idea, dict)
                and str(idea.get("last_mentioned") or "") < date_text
            ]
            if stale_mentions:
                issues.append(
                    "Fringe Corner: open ideas not managed today ("
                    + ", ".join(stale_mentions)
                    + ")"
                )
        except (OSError, ValueError, urllib.error.URLError) as exc:
            issues.append(f"Fringe Corner: ledger unavailable ({exc})")

    return sorted(set(issues))


def _send_alert(target: str, subject: str, message: str) -> str | None:
    result = subprocess.run(
        [
            str(Path.home() / ".local/bin/hermes"),
            "send",
            "--to",
            target,
            "--subject",
            subject,
            "--quiet",
            message,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout).strip()
    return detail or f"exit {result.returncode}"


def reconcile_alerts(
    issues: list[str],
    date_text: str,
    target: str,
    state_path: Path = ALERT_STATE_PATH,
) -> str | None:
    previous = _load_alert_state(state_path)
    previous_issues = previous.get("issues", []) if previous.get("date") == date_text else []
    if issues != previous_issues:
        if issues:
            message = "Daily report delivery check failed:\n- " + "\n- ".join(issues)
            error = _send_alert(target, "[Sector Tracker pipeline]", message)
        elif previous_issues:
            error = _send_alert(
                target,
                "[Sector Tracker pipeline recovered]",
                "All due reports, dashboard uploads, and Fringe ledger checks now pass.",
            )
        else:
            error = None
        if error:
            return f"alert delivery failed ({error})"
    _save_json({"date": date_text, "issues": issues}, state_path)
    return None


def main() -> int:
    current = datetime.now(UTC)
    issues = audit_pipeline(current)
    config = load_config()
    alert_error = reconcile_alerts(
        issues,
        current.date().isoformat(),
        config.get("ALERT_TARGET", "telegram"),
    )
    if alert_error:
        issues.append(alert_error)
    stamp = current.strftime("%Y-%m-%d %H:%M:%S UTC")
    if issues:
        for issue in issues:
            print(f"[{stamp}] {issue}", file=sys.stderr)
        return 1
    print(f"[{stamp}] all due report pipeline checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
