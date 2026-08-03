"""Backup chain: the /api/backup snapshot endpoint and the nightly puller."""

import gzip
import importlib.util
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from app import db
from app.main import app

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_UPLOADER_SPEC = importlib.util.spec_from_file_location(
    "vault_report_uploader", _SCRIPTS / "vault_report_uploader.py"
)
assert _UPLOADER_SPEC is not None and _UPLOADER_SPEC.loader is not None
_uploader = importlib.util.module_from_spec(_UPLOADER_SPEC)
sys.modules.setdefault("vault_report_uploader", _uploader)
_UPLOADER_SPEC.loader.exec_module(_uploader)

_BACKUP_SPEC = importlib.util.spec_from_file_location("board_backup", _SCRIPTS / "board_backup.py")
assert _BACKUP_SPEC is not None and _BACKUP_SPEC.loader is not None
backup = importlib.util.module_from_spec(_BACKUP_SPEC)
_BACKUP_SPEC.loader.exec_module(backup)


@pytest.fixture()
def board_app(tmp_path: Path) -> Iterator[Path]:
    had = hasattr(app.state, "settings")
    saved = app.state.settings if had else None
    path = tmp_path / "board.sqlite3"
    app.state.settings = SimpleNamespace(edit_token="sekrit", database_path=path)
    db.apply_fringe_actions(
        path,
        slug="fringe",
        report_date="2026-07-23",
        actions=[("open", "AMD", "long", "thesis", None, "$580", 60.0, "$450")],
    )
    yield path
    if had:
        app.state.settings = saved
    elif hasattr(app.state, "settings"):
        delattr(app.state, "settings")


def test_backup_endpoint_streams_verifiable_snapshot(board_app: Path, tmp_path: Path) -> None:
    client = TestClient(app)
    assert client.get("/api/backup").status_code == 401  # token required

    response = client.get("/api/backup", headers={"X-Edit-Token": "sekrit"})
    assert response.status_code == 200
    assert response.content.startswith(b"SQLite format 3")

    snapshot = tmp_path / "snapshot.sqlite3"
    snapshot.write_bytes(response.content)
    assert backup.verify_snapshot(snapshot) is None
    conn = sqlite3.connect(snapshot)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM fringe_ideas").fetchone()
    finally:
        conn.close()
    assert count == 1


def test_nightly_run_verifies_compresses_and_rotates(
    board_app: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(app)
    payload = client.get("/api/backup", headers={"X-Edit-Token": "sekrit"}).content
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for day in range(1, 5):  # stale snapshots beyond keep=3 get pruned
        (backup_dir / f"board-2026-06-0{day}.sqlite3.gz").write_bytes(b"old")
    alerts: list[str] = []
    monkeypatch.setattr(backup, "fetch_snapshot", lambda base, token: payload)
    monkeypatch.setattr(backup, "send_alert", lambda target, msg: alerts.append(msg))
    monkeypatch.setattr(
        backup,
        "load_config",
        lambda: {
            "BOARD_URL": "https://board.test",
            "EDIT_TOKEN": "sekrit",
            "BACKUP_DIR": str(backup_dir),
            "BACKUP_KEEP": "3",
        },
    )

    assert backup.run() == 0
    today = datetime.now(UTC).date().isoformat()
    archive = backup_dir / f"board-{today}.sqlite3.gz"
    assert archive.exists()
    assert gzip.decompress(archive.read_bytes()).startswith(b"SQLite format 3")
    kept = sorted(p.name for p in backup_dir.glob("board-*.sqlite3.gz"))
    assert len(kept) == 3 and kept[-1] == archive.name
    assert not list(backup_dir.glob("*.partial"))
    assert alerts == []


def test_corrupt_snapshot_fails_loudly_and_leaves_no_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_dir = tmp_path / "backups"
    alerts: list[Any] = []
    monkeypatch.setattr(backup, "fetch_snapshot", lambda base, token: b"not a database")
    monkeypatch.setattr(backup, "send_alert", lambda target, msg: alerts.append(msg))
    monkeypatch.setattr(
        backup,
        "load_config",
        lambda: {
            "BOARD_URL": "https://board.test",
            "EDIT_TOKEN": "sekrit",
            "BACKUP_DIR": str(backup_dir),
        },
    )

    assert backup.run() == 1
    assert len(alerts) == 1 and "FAILED" in alerts[0]
    assert not list(backup_dir.glob("board-*"))


def test_failed_replacement_preserves_existing_same_day_archive(
    board_app: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(app)
    payload = client.get("/api/backup", headers={"X-Edit-Token": "sekrit"}).content
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    today = datetime.now(UTC).date().isoformat()
    archive = backup_dir / f"board-{today}.sqlite3.gz"
    original = b"known-good-existing-archive"
    archive.write_bytes(original)
    alerts: list[str] = []

    monkeypatch.setattr(backup, "fetch_snapshot", lambda base, token: payload)
    monkeypatch.setattr(backup, "send_alert", lambda target, msg: alerts.append(msg))
    monkeypatch.setattr(
        backup,
        "load_config",
        lambda: {
            "BOARD_URL": "https://board.test",
            "EDIT_TOKEN": "sekrit",
            "BACKUP_DIR": str(backup_dir),
        },
    )

    class BrokenGzip:
        def __init__(self, **kwargs: object) -> None:
            raise OSError("disk full")

    monkeypatch.setattr(backup.gzip, "GzipFile", BrokenGzip)

    assert backup.run() == 1
    assert archive.read_bytes() == original
    assert not list(backup_dir.glob("*.tmp"))
    assert len(alerts) == 1 and "FAILED" in alerts[0]
