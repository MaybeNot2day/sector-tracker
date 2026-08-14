#!/usr/bin/env python3
"""Nightly off-box backup of the market board database.

Pulls a consistent SQLite snapshot from the board's token-gated /api/backup
endpoint (VACUUM INTO on the droplet), verifies it actually opens and
contains the irreplaceable tables, gzips it into the Syncthing-mirrored
vault (droplet -> hermes box -> Mac: three copies), and prunes old
snapshots. A failed night alerts through the Hermes gateway — a backup that
silently stops existing is the whole failure mode this guards against.

Config: ~/.config/sector-tracker/uploader.env (BOARD_URL, EDIT_TOKEN,
ALERT_TARGET, BACKUP_DIR, BACKUP_KEEP).
"""

from __future__ import annotations

import gzip
import http.client
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from vault_report_uploader import load_config

DEFAULT_BACKUP_DIR = Path.home() / "hermes-research/.board-backups"
DEFAULT_KEEP = 14
REQUIRED_TABLES = {"reports", "fringe_ideas", "fringe_equity_history", "key_dates"}
HERMES_BIN = Path.home() / ".local/bin/hermes"
REQUEST_TIMEOUT = 180


def fetch_snapshot(base_url: str, token: str) -> bytes:
    request = urllib.request.Request(base_url + "/api/backup", headers={"X-Edit-Token": token})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # nosec B310
        return bytes(response.read())


def verify_snapshot(path: Path) -> str | None:
    """Open the snapshot and prove it is a healthy board database."""
    try:
        conn = sqlite3.connect(path)
        try:
            (integrity,) = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity != "ok":
                return f"integrity_check: {integrity}"
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            missing = REQUIRED_TABLES - tables
            if missing:
                return f"missing tables: {', '.join(sorted(missing))}"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"unreadable snapshot: {exc}"
    return None


def rotate(backup_dir: Path, keep: int) -> int:
    snapshots = sorted(backup_dir.glob("board-*.sqlite3.gz"))
    stale = snapshots[:-keep] if keep > 0 else snapshots
    for path in stale:
        path.unlink()
    return len(stale)


def send_alert(target: str, message: str) -> None:
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
        return
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        log(f"alert failed: {detail or f'exit {result.returncode}'}")


def run() -> int:
    config = load_config()
    base_url = config.get("BOARD_URL", "").rstrip("/")
    token = config.get("EDIT_TOKEN", "")
    target = config.get("ALERT_TARGET", "telegram")
    backup_dir = Path(config.get("BACKUP_DIR") or DEFAULT_BACKUP_DIR)
    try:
        keep = int(config.get("BACKUP_KEEP", str(DEFAULT_KEEP)))
    except ValueError:
        keep = DEFAULT_KEEP
    if not base_url or not token:
        log("missing BOARD_URL/EDIT_TOKEN; nothing to do")
        return 2
    stamp = datetime.now(UTC).date().isoformat()
    final_path = backup_dir / f"board-{stamp}.sqlite3.gz"

    temp_path: Path | None = None
    try:
        payload = fetch_snapshot(base_url, token)
        backup_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".board-snapshot.",
            suffix=".sqlite3",
            dir=backup_dir,
        ) as raw:
            raw.write(payload)
            raw.flush()
            problem = verify_snapshot(Path(raw.name))
        if problem is not None:
            raise RuntimeError(problem)
        with tempfile.NamedTemporaryFile(
            prefix=f".{final_path.name}.",
            suffix=".tmp",
            dir=backup_dir,
            delete=False,
        ) as tmp:
            temp_path = Path(tmp.name)
            with gzip.GzipFile(fileobj=tmp, mode="wb", compresslevel=6) as archive:
                archive.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        # The verified bytes are compressed completely before the atomic cutover.
        os.replace(temp_path, final_path)
        temp_path = None
    except (
        OSError,
        ValueError,
        RuntimeError,
        urllib.error.URLError,
        http.client.HTTPException,
    ) as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        log(f"backup failed: {exc}")
        send_alert(
            target,
            f"Board backup FAILED for {stamp}: {exc}. The Fringe ledger has no "
            f"fresh off-box copy tonight — investigate before it matters.",
        )
        return 1

    removed = rotate(backup_dir, keep)
    kept = len(list(backup_dir.glob("board-*.sqlite3.gz")))
    log(
        f"backup ok: {final_path.name} "
        f"({len(payload) / 1e6:.1f} MB raw, {final_path.stat().st_size / 1e6:.1f} MB gz), "
        f"{kept} kept, {removed} pruned"
    )
    return 0


def log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} UTC] {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(run())
