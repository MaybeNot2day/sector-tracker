#!/usr/bin/env python3
"""Intraday barrier monitor for the Fringe paper book: stops AND targets.

Every timer tick (5 minutes, 24/7 — crypto never closes) it reads the open
book from /api/fringe and compares each position's mark against its declared
stop and target — the same two numbers its Kelly size was computed from. A
barrier must hold on two consecutive ticks (bad-tick filter); the second
tick closes the position through POST /api/fringe/{id}/close — the board
re-marks at its own fresh price, so gaps close with honest slippage — and
announces the close through the Hermes gateway. Stops cut losers; targets
harvest winners; re-opening past a target is a fresh, re-sized bet in the
next brief.

Stops are not static: a trailing ratchet tightens the working stop as the
mark moves in the position's favor. At +1R the stop lifts to breakeven;
beyond that it trails 1R behind the mark (+2R locks +1R of profit, and so
on). The ratchet only ever tightens, and a trailed stop that breaches on
two consecutive ticks closes the position as auto-trail.

Positions without a declared stop cannot be stop-enforced; those get one
alert per day when the mark sits 10% or more against entry.

Config: ~/.config/sector-tracker/uploader.env (BOARD_URL, EDIT_TOKEN,
ALERT_TARGET). State: ~/.local/state/sector-tracker/stop-monitor.json.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
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
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as tmp:
        json.dump(state, tmp, indent=1, sort_keys=True)
    os.replace(tmp.name, path)


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


def send_alert(target: str, message: str) -> bool:
    try:
        result = subprocess.run(
            [str(HERMES_BIN), "send", "--to", target, "--quiet", message],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"alert failed: {exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        log(f"alert failed: {detail or f'exit {result.returncode}'}")
        return False
    return True


def stop_breached(direction: str, last: float, stop: float) -> bool:
    return last <= stop if direction != "short" else last >= stop


def target_reached(direction: str, last: float, target_price: float) -> bool:
    return last >= target_price if direction != "short" else last <= target_price


def trail_stop(
    direction: str,
    entry: float,
    declared_stop: float,
    last: float,
    previous: float | None,
) -> float:
    """Working stop after the R-multiple ratchet; only ever tightens."""
    is_long = direction != "short"
    risk = entry - declared_stop if is_long else declared_stop - entry
    base = previous if previous is not None else declared_stop
    if risk <= 0:  # degenerate geometry: no R math, keep the tighter stop
        return max(declared_stop, base) if is_long else min(declared_stop, base)
    favorable_r = (last - entry if is_long else entry - last) / risk
    if favorable_r < 1.0:  # not yet at breakeven: trail stays put
        return base
    candidate = (
        entry + (favorable_r - 1.0) * risk if is_long else entry - (favorable_r - 1.0) * risk
    )
    return max(candidate, base, declared_stop) if is_long else min(candidate, base, declared_stop)


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
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
        log(f"book unavailable ({exc}); retrying next tick")
        return 0

    open_ideas = book.get("open") or []
    state = load_state()
    counts = state.get("breach", {}) if isinstance(state.get("breach"), dict) else {}
    alerted = state.get("alerted", {}) if isinstance(state.get("alerted"), dict) else {}
    trailed = state.get("trail", {}) if isinstance(state.get("trail"), dict) else {}
    today = datetime.now(UTC).date().isoformat()
    next_counts: dict[str, int] = {}
    next_trail: dict[str, float] = {}
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
        target_price = idea.get("target_price")
        entry = idea.get("entry_price")
        pct = idea.get("unrealized_pct")
        if not isinstance(last, int | float):
            continue

        declared = float(stop) if isinstance(stop, int | float) else None
        trail_level: float | None = None
        if declared is not None and isinstance(entry, int | float):
            stored = trailed.get(key)
            level = trail_stop(
                direction,
                float(entry),
                declared,
                float(last),
                float(stored) if isinstance(stored, int | float) else None,
            )
            tightened = level > declared if direction != "short" else level < declared
            if tightened:  # only a ratcheted trail is worth persisting
                trail_level = level
                next_trail[key] = level

        working_stop = trail_level if trail_level is not None else declared
        barriers: list[tuple[str, float]] = []
        if working_stop is not None and stop_breached(direction, float(last), working_stop):
            barriers.append(("trail" if trail_level is not None else "stop", working_stop))
        if isinstance(target_price, int | float) and target_reached(
            direction, float(last), float(target_price)
        ):
            barriers.append(("target", float(target_price)))

        fired = False
        for kind, level in barriers[:1]:  # inverted geometry: stop wins
            skey = f"{kind}:{key}"
            streak = int(counts.get(skey, 0)) + 1
            if streak < BREACH_TICKS:
                next_counts[skey] = streak
                log(f"{skey}: {level} touched at {last} (tick {streak}/{BREACH_TICKS})")
                continue
            if kind == "trail":
                reason = (
                    f"auto-trail: {direction} trailed stop ${level:g} breached at "
                    f"${float(last):g} on two consecutive 5m marks "
                    f"(declared stop ${declared if declared is not None else level:g})"
                )
            else:
                verb = "breached" if kind == "stop" else "reached"
                reason = (
                    f"auto-{kind}: {direction} {kind} ${level:g} {verb} at ${float(last):g} "
                    f"on two consecutive 5m marks"
                )
            try:
                result = close_position(base_url, token, int(str(idea_id)), reason)
            except (
                OSError,
                ValueError,
                urllib.error.URLError,
                http.client.HTTPException,
            ) as exc:
                next_counts[skey] = streak  # keep armed; retry next tick
                log(f"{skey}: close failed ({exc}); retrying next tick")
                continue
            item = result.get("closed") or {}
            closed += 1
            fired = True
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
            log(f"{skey}: closed ({summary})")
            headline = (
                f"Trail stop: {direction.upper()} {ticker} closed — trailed stop "
                f"${level:g} breached (mark ${float(last):g})"
                if kind == "trail"
                else f"Auto-stop: {direction.upper()} {ticker} closed — declared stop "
                f"${level:g} breached (mark ${float(last):g})"
                if kind == "stop"
                else f"Target hit: {direction.upper()} {ticker} harvested — declared "
                f"target ${level:g} reached (mark ${float(last):g})"
            )
            follow = (
                "The agent will review it in the next Fringe brief."
                if kind in ("stop", "trail")
                else "If the move has legs, the agent can re-open a fresh, re-sized bet "
                "in the next brief."
            )
            send_alert(target, f"{headline}. {summary}. {follow} {base_url}/#view=fringe")
        if fired or barriers:
            continue

        if (
            not isinstance(stop, int | float)
            and isinstance(pct, int | float)
            and float(pct) <= BIG_MOVE_ALERT_PCT
            and alerted.get(key) != today
        ):
            # No declared stop: nothing to enforce, but a double-digit adverse
            # move should never pass silently. One alert per day.
            if send_alert(
                target,
                f"Fringe book: {direction.upper()} {ticker} is {float(pct):+.2f}% "
                f"against entry and has NO declared stop — unenforceable. "
                f"Consider a manual review. {base_url}/#view=fringe",
            ):
                alerted[key] = today

    save_state({"breach": next_counts, "alerted": alerted, "trail": next_trail})
    log(f"tick done: {len(open_ideas)} open, {closed} auto-closed")
    return 0


def log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} UTC] {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(run())
