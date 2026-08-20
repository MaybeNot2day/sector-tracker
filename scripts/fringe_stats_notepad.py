#!/usr/bin/env python3
"""Fringe risk-mode stats, stamped into the Fringe cron job's durable notepad.

Runs before the daily Fringe brief: pulls /api/fringe, computes the rolling
calibration (win rate, expectancy, profit factor, losing streak, direction
and asset buckets, open giveback-to-stops) and writes a compact risk-mode
block into the job's notepad so the trading agent sees its own track record
before composing new ideas. Breakers: HALF_SIZE at a 3-loss streak,
NO_NEW_OPENS at 5 (or 8+ closed with negative expectancy), CALIBRATION_CAP
below a 35% win rate once 5+ trades have closed.

Config: ~/.config/sector-tracker/uploader.env (BOARD_URL; HERMES_BIN and
FRINGE_JOB_ID optional). Notepad: `hermes cron notepad <job> set <key> <value>`.
"""

from __future__ import annotations

import argparse
import http.client
import json
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from vault_report_uploader import load_config

HERMES_BIN = Path.home() / ".local/bin/hermes"
FRINGE_JOB_ID = "fa74bf34781c"
CRYPTO_TICKERS = frozenset({"BTC", "ETH", "SOL", "HYPE", "DOGE", "XRP", "LINK", "AVAX"})
REQUEST_TIMEOUT = 20
NOTEPAD_TIMEOUT = 30


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_book(base_url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url + "/api/fringe", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # nosec B310
        return cast(dict[str, Any], json.loads(response.read().decode("utf-8")))


def signed_usd(value: float) -> str:
    return f"{'-' if value < 0 else '+'}${abs(value):,.2f}"


def format_profit_factor(value: float) -> str:
    return "inf" if value == float("inf") else f"{value:.2f}"


def _close_order_key(item: dict[str, Any]) -> tuple[str, float]:
    ident = item.get("id")
    return (str(item.get("closed") or ""), float(ident) if isinstance(ident, (int, float)) else 0.0)


def compute_rolling_stats(closed: list[dict[str, Any]]) -> dict[str, Any]:
    """Risk statistics over sized closes, matching the server's sizing gates."""
    sized: list[tuple[dict[str, Any], float]] = []
    for item in closed:
        value = _num(item.get("realized_usd"))
        if value is not None:
            sized.append((item, value))
    realized = [value for _, value in sized]
    closed_count = len(sized)
    win_values = [value for value in realized if value > 0]
    loss_values = [value for value in realized if value < 0]
    gross_win = sum(win_values)
    gross_loss = sum(loss_values)
    streak = 0
    for _, value in sorted(sized, key=lambda pair: _close_order_key(pair[0]), reverse=True):
        if value < 0:
            streak += 1
        else:
            break
    return {
        "closed_count": closed_count,
        "wins": len(win_values),
        "win_rate_pct": (100.0 * len(win_values) / closed_count) if closed_count else 0.0,
        "expectancy_usd": (sum(realized) / closed_count) if closed_count else 0.0,
        "profit_factor": (gross_win / abs(gross_loss)) if gross_loss else float("inf"),
        "avg_win_usd": (gross_win / len(win_values)) if win_values else 0.0,
        "avg_loss_usd": (gross_loss / len(loss_values)) if loss_values else 0.0,
        "losing_streak": streak,
    }


def direction_bucket(item: dict[str, Any]) -> str:
    return "short" if str(item.get("direction") or "").lower() == "short" else "long"


def asset_bucket(item: dict[str, Any]) -> str:
    return "crypto" if str(item.get("ticker") or "").upper() in CRYPTO_TICKERS else "equity/futures"


def compute_buckets(
    closed: list[dict[str, Any]], classifier: Callable[[dict[str, Any]], str]
) -> dict[str, dict[str, Any]]:
    """Per-bucket trade count, wins, and summed realized USD."""
    buckets: dict[str, dict[str, Any]] = {}
    for item in closed:
        bucket = buckets.setdefault(classifier(item), {"count": 0, "wins": 0, "realized_usd": 0.0})
        bucket["count"] += 1
        value = _num(item.get("realized_usd")) or 0.0
        if value > 0:
            bucket["wins"] += 1
        bucket["realized_usd"] += value
    return buckets


def compute_giveback(open_items: list[dict[str, Any]], equity: float) -> tuple[float, float]:
    """USD surrendered if every open position went straight to its stop, plus % of equity."""
    total = 0.0
    for item in open_items:
        entry = _num(item.get("entry_price"))
        last = _num(item.get("last"))
        stop = _num(item.get("stop_price"))
        size = _num(item.get("size_notional"))
        if entry is None or last is None or stop is None or size is None or entry == 0:
            continue
        qty = size / entry
        if str(item.get("direction") or "").lower() == "short":
            total += qty * (stop - last)
        else:
            total += qty * (last - stop)
    return total, (100.0 * total / equity) if equity else 0.0


def compute_risk_modes(stats: dict[str, Any]) -> dict[str, bool]:
    """Calibration cap and circuit breakers from the rolling stats."""
    return {
        "calibration_cap": stats["closed_count"] >= 5 and stats["win_rate_pct"] < 35.0,
        "half_size": stats["losing_streak"] >= 3,
        "no_new_opens": stats["losing_streak"] >= 5
        or (stats["closed_count"] >= 8 and stats["expectancy_usd"] < 0),
    }


def mode_labels(modes: dict[str, bool]) -> tuple[str, str]:
    mode = "CALIBRATION_CAP" if modes["calibration_cap"] else "NORMAL"
    if modes["no_new_opens"]:
        breaker = "NO_NEW_OPENS"
    elif modes["half_size"]:
        breaker = "HALF_SIZE"
    else:
        breaker = "OFF"
    return mode, breaker


def format_stats_block(
    today: str,
    stats: dict[str, Any],
    direction: dict[str, dict[str, Any]],
    asset: dict[str, dict[str, Any]],
    giveback_usd: float,
    giveback_pct: float,
    modes: dict[str, bool],
) -> str:
    empty = {"count": 0, "wins": 0, "realized_usd": 0.0}
    long = direction.get("long", empty)
    short = direction.get("short", empty)
    crypto = asset.get("crypto", empty)
    other = asset.get("equity/futures", empty)
    mode, breaker = mode_labels(modes)
    lines = [
        f"FRINGE RISK MODE — {today}",
        f"- Mode: {mode} · Breaker: {breaker}",
        f"- Rolling: {stats['closed_count']} closed · win rate {stats['win_rate_pct']:.1f}% · "
        f"expectancy {signed_usd(stats['expectancy_usd'])} · "
        f"profit factor {format_profit_factor(stats['profit_factor'])}",
        f"- Direction: long {signed_usd(long['realized_usd'])} ({long['wins']}/{long['count']}) · "
        f"short {signed_usd(short['realized_usd'])} ({short['wins']}/{short['count']})",
        f"- Asset: crypto {signed_usd(crypto['realized_usd'])} "
        f"({crypto['wins']}/{crypto['count']}) · "
        f"equity/futures {signed_usd(other['realized_usd'])} "
        f"({other['wins']}/{other['count']})",
        f"- Open risk: giveback-to-stops {signed_usd(giveback_usd)} "
        f"= {giveback_pct:.1f}% of equity",
        "- While CALIBRATION_CAP: conf cap 55, risk <= 0.75% equity per OPEN, "
        "planned RR >= 2 required",
        "- While NO_NEW_OPENS: manage the open book only; new OPENs are sized to zero",
    ]
    return "\n".join(lines)


def build_stats_block(payload: dict[str, Any], today: str) -> str:
    closed = [item for item in payload.get("closed", []) if isinstance(item, dict)]
    open_items = [item for item in payload.get("open", []) if isinstance(item, dict)]
    portfolio = payload.get("summary", {}).get("portfolio", {})
    equity = _num(portfolio.get("equity")) or 0.0
    stats = compute_rolling_stats(closed)
    direction = compute_buckets(closed, direction_bucket)
    asset = compute_buckets(closed, asset_bucket)
    giveback_usd, giveback_pct = compute_giveback(open_items, equity)
    modes = compute_risk_modes(stats)
    return format_stats_block(today, stats, direction, asset, giveback_usd, giveback_pct, modes)


def notepad_set(hermes_bin: str, job_id: str, key: str, value: str) -> bool:
    try:
        result = subprocess.run(
            [hermes_bin, "cron", "notepad", job_id, "set", key, value],
            capture_output=True,
            text=True,
            timeout=NOTEPAD_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"notepad set {key} failed: {exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        log(f"notepad set {key} failed: {detail or f'exit {result.returncode}'}")
        return False
    return True


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="print the notepad block; write nothing"
    )
    args = parser.parse_args(argv)
    config = load_config()
    base_url = config.get("BOARD_URL", "").rstrip("/")
    hermes_bin = config.get("HERMES_BIN") or str(HERMES_BIN)
    job_id = config.get("FRINGE_JOB_ID") or FRINGE_JOB_ID
    if not base_url:
        log("missing BOARD_URL; nothing to do")
        return 2
    try:
        book = fetch_book(base_url)
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
        log(f"book unavailable: {exc}")
        return 1
    today = datetime.now(UTC).date().isoformat()
    block = build_stats_block(book, today)
    if args.dry_run:
        print(block)
        return 0
    ok = notepad_set(hermes_bin, job_id, "fringe_stats", block)
    ok = notepad_set(hermes_bin, job_id, "fringe_stats_date", today) and ok
    if not ok:
        return 1
    log(f"notepad updated for Fringe job {job_id}")
    return 0


def log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} UTC] {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(run())
