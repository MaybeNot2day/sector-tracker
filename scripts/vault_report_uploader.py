#!/usr/bin/env python3
"""Auto-upload dated vault reports to the market board.

Watches an Obsidian vault directory for files named `YYYY-MM-DD <Title>.md`
(the naming convention Hermes cron jobs use) and POSTs new or changed ones
to the board's /api/reports endpoint. Designed to run under a macOS
LaunchAgent: WatchPaths triggers a pass instantly when a report file drops,
and a StartInterval sweep catches in-place edits that don't touch the
directory entry.

Stdlib only and Python 3.9 compatible so it runs on macOS system python3.

Config lives in ~/.config/sector-tracker/uploader.env (KEY=VALUE lines):

    BOARD_URL=http://167.172.160.215:8787
    EDIT_TOKEN=...
    VAULT_DIR=/Users/you/Desktop/Main/HERMES RESEARCH   # optional
    MAX_AGE_DAYS=30                                     # optional

Modes:
    (default)   one pass: upload new/changed dated files, update state
    --baseline  record current file hashes WITHOUT uploading (install step)
    --dry-run   show what would upload, change nothing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

CONFIG_PATH = Path.home() / ".config/sector-tracker/uploader.env"
STATE_PATH = Path.home() / ".local/state/sector-tracker/vault-uploads.json"
DATED_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}) (.+)\.md$")
# A file whose mtime is this fresh may still be mid-write; settle briefly.
SETTLE_SECONDS = 3.0
REQUEST_TIMEOUT = 20


def load_config(path: Path = CONFIG_PATH) -> dict[str, str]:
    config: dict[str, str] = {}
    if not path.exists():
        return config
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        # Tolerate shell-style quoting (EDIT_TOKEN='abc') pasted from .env files.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        config[key.strip()] = value
    return config


def parse_report_name(filename: str) -> tuple[str, str] | None:
    """`2026-07-10 Biotech Pharma Brief.md` -> ("2026-07-10", "Biotech Pharma Brief")."""
    match = DATED_NAME.match(filename)
    if not match:
        return None
    date_text, title = match.group(1), match.group(2).strip()
    if not title:
        return None
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        return None
    return date_text, title


def within_age(date_text: str, max_age_days: int, today: date | None = None) -> bool:
    """Only recent reports upload; ancient notes touched by vault tooling stay put."""
    parsed = datetime.strptime(date_text, "%Y-%m-%d").date()
    reference = today or date.today()
    if parsed > reference + timedelta(days=2):  # tolerate small clock/timezone skew
        return False
    return reference - parsed <= timedelta(days=max_age_days)


def scan_vault(vault: Path, max_age_days: int) -> list[tuple[Path, str, str]]:
    """Dated report files in the vault root -> [(path, date, title)], name-sorted."""
    found: list[tuple[Path, str, str]] = []
    for entry in sorted(vault.iterdir()):
        if not entry.is_file():
            continue
        parsed = parse_report_name(entry.name)
        if parsed is None:
            continue
        date_text, title = parsed
        if not within_age(date_text, max_age_days):
            continue
        found.append((entry, date_text, title))
    return found


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def load_state(path: Path = STATE_PATH) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def save_state(state: dict[str, str], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def post_report(base_url: str, token: str, title: str, date_text: str, body: str) -> dict:
    payload = json.dumps({"title": title, "date": date_text, "body": body}).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/reports",
        data=payload,
        headers={"Content-Type": "application/json", "X-Edit-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true", help="record hashes, upload nothing")
    parser.add_argument("--dry-run", action="store_true", help="log actions, change nothing")
    args = parser.parse_args(argv)

    config = load_config()
    base_url = config.get("BOARD_URL", "")
    token = config.get("EDIT_TOKEN", "")
    vault = Path(config.get("VAULT_DIR") or Path.home() / "Desktop/Main/HERMES RESEARCH")
    max_age_days = int(config.get("MAX_AGE_DAYS", "30"))

    if not base_url or not token:
        log(f"missing BOARD_URL/EDIT_TOKEN in {CONFIG_PATH}; nothing to do")
        return 2
    if not vault.is_dir():
        log(f"vault directory not found: {vault}")
        return 2

    state = load_state()
    reports = scan_vault(vault, max_age_days)
    uploaded = failed = 0

    for path, date_text, title in reports:
        try:
            if time.time() - path.stat().st_mtime < SETTLE_SECONDS:
                time.sleep(SETTLE_SECONDS)  # writer may still be flushing
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            log(f"skip {path.name}: unreadable ({exc})")
            continue
        if not body.strip():
            continue  # empty shells re-check on the next pass
        digest = content_hash(body)
        if state.get(path.name) == digest:
            continue
        if args.baseline:
            state[path.name] = digest
            log(f"baseline {path.name}")
            continue
        if args.dry_run:
            log(f"would upload {path.name} ({date_text} · {title})")
            continue
        try:
            result = post_report(base_url, token, title, date_text, body)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            failed += 1
            log(f"upload failed {path.name}: {exc}")
            continue  # hash stays unrecorded -> retried on the next pass
        state[path.name] = digest
        uploaded += 1
        log(f"uploaded {path.name} -> id={result.get('id')} slug={result.get('slug')}")

    if not args.dry_run:
        save_state(state)
    log(f"pass done: {len(reports)} candidates, {uploaded} uploaded, {failed} failed")
    return 1 if failed else 0


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(run())
