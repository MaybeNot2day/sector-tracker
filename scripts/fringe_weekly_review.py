#!/usr/bin/env python3
"""Friday self-review for the Fringe paper book, delivered like every other report.

Pulls /api/fringe, composes the compact weekly review (equity and return,
this week's closes, cumulative calibration, best/worst trade, current risk
mode and breakers, open giveback-to-stops), writes it to the vault as a
dated note, and posts the body to the board. Risk-mode math is shared with
fringe_stats_notepad so the review and the notepad never disagree.

Config: ~/.config/sector-tracker/uploader.env (BOARD_URL, EDIT_TOKEN;
VAULT_DIR optional, default ~/hermes-research).
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fringe_stats_notepad import (
    compute_giveback,
    compute_risk_modes,
    compute_rolling_stats,
    fetch_book,
    format_profit_factor,
    mode_labels,
    signed_usd,
)
from vault_report_uploader import load_config

DEFAULT_VAULT_DIR = Path.home() / "hermes-research"
REPORT_TITLE = "Fringe Weekly Review"
REQUEST_TIMEOUT = 20


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_closed_day(item: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat(str(item.get("closed") or "")[:10])
    except ValueError:
        return None


def week_trades(closed: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Trades closed inside the trailing 7-day window ending today."""
    start = today - timedelta(days=7)
    return [
        item
        for item in closed
        if (day := parse_closed_day(item)) is not None and start <= day <= today
    ]


def review_bullets(payload: dict[str, Any], today: date) -> list[str]:
    closed = [item for item in payload.get("closed", []) if isinstance(item, dict)]
    open_items = [item for item in payload.get("open", []) if isinstance(item, dict)]
    portfolio = payload.get("summary", {}).get("portfolio", {})
    equity = _num(portfolio.get("equity")) or 0.0
    return_pct = _num(portfolio.get("return_pct")) or 0.0
    stats = compute_rolling_stats(closed)
    mode, breaker = mode_labels(compute_risk_modes(stats))
    week = week_trades(closed, today)
    week_wins = sum(1 for item in week if (_num(item.get("realized_usd")) or 0.0) > 0)
    week_net = sum(_num(item.get("realized_usd")) or 0.0 for item in week)
    giveback_usd, giveback_pct = compute_giveback(open_items, equity)
    bullets = [
        f"Equity ${equity:,.2f}, total return {return_pct:+.2f}% since inception.",
        f"This week: {len(week)} closed, {week_wins} win{'s' if week_wins != 1 else ''}, "
        f"net {signed_usd(week_net)}.",
        f"Cumulative: {stats['win_rate_pct']:.1f}% win rate, profit factor "
        f"{format_profit_factor(stats['profit_factor'])}, "
        f"expectancy {signed_usd(stats['expectancy_usd'])} per trade.",
    ]
    if week:
        best = max(week, key=lambda item: _num(item.get("realized_usd")) or 0.0)
        worst = min(week, key=lambda item: _num(item.get("realized_usd")) or 0.0)
        bullets.append(
            f"Best {best.get('ticker')} {signed_usd(_num(best.get('realized_usd')) or 0.0)}; "
            f"worst {worst.get('ticker')} {signed_usd(_num(worst.get('realized_usd')) or 0.0)}."
        )
    else:
        bullets.append("No closed trades this week; best/worst not applicable.")
    bullets.append(f"Risk mode {mode}, breaker {breaker}; losing streak {stats['losing_streak']}.")
    bullets.append(
        f"Open giveback-to-stops {signed_usd(giveback_usd)}, {giveback_pct:.1f}% of equity."
    )
    return bullets


def compose_review(payload: dict[str, Any], today: date) -> tuple[str, str]:
    """(frontmatter + body) for the vault, body alone for the board."""
    stamp = today.isoformat()
    frontmatter = (
        "---\n"
        f"date: {stamp}\n"
        "type: research\n"
        "tags: [fringe, weekly-review, market-brief]\n"
        "status: draft\n"
        "---\n"
    )
    body = f"# {REPORT_TITLE} — {stamp}\n\n"
    body += "\n".join(f"- {bullet}" for bullet in review_bullets(payload, today)) + "\n"
    return frontmatter + body, body


def post_report(base_url: str, token: str, date_text: str, body: str) -> int:
    payload = json.dumps({"title": REPORT_TITLE, "body": body, "date": date_text}).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/api/reports",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "X-Edit-Token": token},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # nosec B310
        return int(response.status)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="print the review markdown; write nothing"
    )
    args = parser.parse_args(argv)
    config = load_config()
    base_url = config.get("BOARD_URL", "").rstrip("/")
    token = config.get("EDIT_TOKEN", "")
    vault = Path(config.get("VAULT_DIR") or DEFAULT_VAULT_DIR)
    if not base_url or not token:
        log("missing BOARD_URL/EDIT_TOKEN; nothing to do")
        return 2
    try:
        book = fetch_book(base_url)
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
        log(f"book unavailable: {exc}")
        return 1
    today = datetime.now(UTC).date()
    markdown, body = compose_review(book, today)
    if args.dry_run:
        print(markdown, end="")
        return 0
    failed = False
    path = vault / f"{today.isoformat()} {REPORT_TITLE}.md"
    try:
        vault.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        log(f"wrote {path}")
    except OSError as exc:
        log(f"vault write failed: {exc}")
        failed = True
    try:
        status = post_report(base_url, token, today.isoformat(), body)
        log(f"board response: HTTP {status}")
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
        log(f"board post failed: {exc}")
        failed = True
    return 1 if failed else 0


def log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} UTC] {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(run())
