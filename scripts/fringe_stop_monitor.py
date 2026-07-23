#!/usr/bin/env python3
"""Intraday auto-stop monitor for the Fringe paper book.

Every timer tick (5 minutes, 24/7 — crypto never closes) it reads the open
book from /api/fringe and compares each position's mark against its declared
stop. A breach must persist for two consecutive ticks (bad-tick filter); the
second tick closes the position through POST /api/fringe/{id}/close — the
board re-marks at its own fresh price, so gaps close with honest slippage —
and announces the close through the Hermes gateway.

Positions without a declared stop cannot be enforced; those get one alert
per day when the mark sits 10% or more against entry.

Config: ~/.config/sector-tracker/uploader.env (BOARD_URL, EDIT_TOKEN,
ALERT_TARGET). State: ~/.local/state/sector-tracker/stop-monitor.json.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from vault_report_uploader import load_config

STATE_PATH = Path.home() / ".local/state/sector-tracker/stop-monitor.json"
HERMES_BIN = Path.home() / ".local/bin/hermes"
BREACH_TICKS = 2  # consecutive 5-minute marks; filters single bad prints
BIG_MOVE_ALERT_PCT = -10.0  # stopless positions: alert-only threshold
REQUEST_TIMEOUT = 20


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else {}


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def fetch_book(base_url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url + "/api/fringe", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # nosec B310
        return cast(dict[str, Any], json.loads(response.read().decode("utf-8")))


def close_position(base_url: str, token: str, idea_id: int, reason: str) -> dict[str, Any]:
    payload = json.dumps({"reason": reason}).encode("utf-8")
    request = urllib.request.Request(
        base_url + f"/api/fringe/{idea_id}/close",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "X-Edit-Token": token},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # nosec B310
        return cast(dict[str, Any], json.loads(response.read().decode("utf-8")))


def send_alert(target: str, message: str) -> None:
    result = subprocess.run(
        [str(HERMES_BIN), "send", "--to", target, "--quiet", message],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        log(f"alert failed: {detail or f'exit {result.returncode}'}")


def stop_breached(direction: str, last: float, stop: float) -> bool:
    return last <= stop if direction != "short" else last >= stop


def signed_usd(value: float) -> str:
    return f"{'-' if value < 0 else '+'}${abs(value):,.2f}"


def run() -> int:
    config = load_config()
    base_url = config.get("BOARD_URL", "").rstrip("/")
    token = config.get("EDIT_TOKEN", "")
    target = config.get("ALERT_TARGET", "telegram")
    if not base_url or not token:
        log("missing BOARD_URL/EDIT_TOKEN; nothing to do")
        return 2

    try:
        book = fetch_book(base_url)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        log(f"book unavailable ({exc}); retrying next tick")
        return 0

    open_ideas = book.get("open") or []
    state = load_state()
    counts = state.get("breach", {}) if isinstance(state.get("breach"), dict) else {}
    alerted = state.get("alerted", {}) if isinstance(state.get("alerted"), dict) else {}
    today = datetime.now(UTC).date().isoformat()
    next_counts: dict[str, int] = {}
    closed = 0

    for idea in open_ideas:
        if not isinstance(idea, dict):
            continue
        idea_id = idea.get("id")
        ticker = str(idea.get("ticker") or "?")
        direction = str(idea.get("direction") or "long")
        key = f"{ticker}:{direction}:{idea_id}"
        last = idea.get("last")
        stop = idea.get("stop_price")
        pct = idea.get("unrealized_pct")

        if isinstance(last, int | float) and isinstance(stop, int | float):
            if not stop_breached(direction, float(last), float(stop)):
                continue  # count resets by omission from next_counts
            streak = int(counts.get(key, 0)) + 1
            if streak < BREACH_TICKS:
                next_counts[key] = streak
                log(f"{key}: stop {stop} breached at {last} (tick {streak}/{BREACH_TICKS})")
                continue
            reason = (
                f"auto-stop: {direction} stop ${stop:g} breached at ${float(last):g} "
                f"on two consecutive 5m marks"
            )
            try:
                result = close_position(base_url, token, int(str(idea_id)), reason)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                next_counts[key] = streak  # keep armed; retry next tick
                log(f"{key}: close failed ({exc}); retrying next tick")
                continue
            item = result.get("closed") or {}
            closed += 1
            exit_price = item.get("exit_price")
            usd = item.get("realized_usd")
            realized_pct = item.get("realized_pct")
            summary = " · ".join(
                part
                for part in (
                    f"exit {exit_price}" if exit_price is not None else "",
                    f"{realized_pct:+.2f}%" if isinstance(realized_pct, int | float) else "",
                    signed_usd(float(usd)) if isinstance(usd, int | float) else "",
                )
                if part
            )
            log(f"{key}: closed ({summary})")
            send_alert(
                target,
                f"Auto-stop: {direction.upper()} {ticker} closed — declared stop "
                f"${stop:g} breached (mark ${float(last):g}). {summary}. "
                f"The agent will review it in the next Fringe brief. {base_url}/#view=fringe",
            )
        elif (
            isinstance(pct, int | float)
            and float(pct) <= BIG_MOVE_ALERT_PCT
            and alerted.get(key) != today
        ):
            # No declared stop: nothing to enforce, but a double-digit adverse
            # move should never pass silently. One alert per day.
            alerted[key] = today
            send_alert(
                target,
                f"Fringe book: {direction.upper()} {ticker} is {float(pct):+.2f}% "
                f"against entry and has NO declared stop — unenforceable. "
                f"Consider a manual review. {base_url}/#view=fringe",
            )

    save_state({"breach": next_counts, "alerted": alerted})
    log(f"tick done: {len(open_ideas)} open, {closed} auto-closed")
    return 0


def log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} UTC] {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(run())
