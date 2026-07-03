import json
import sqlite3
from pathlib import Path

from app import db


def test_snapshot_round_trip_adds_date_key(tmp_path: Path) -> None:
    database = tmp_path / "test.sqlite3"

    db.save_board_snapshot(database, "2026-07-01", {"regime": "RISK_ON", "score": 71})

    assert db.load_board_snapshots(database, limit=5) == [
        {"regime": "RISK_ON", "score": 71, "date": "2026-07-01"}
    ]


def test_snapshot_upsert_replaces_payload_for_same_date(tmp_path: Path) -> None:
    database = tmp_path / "test.sqlite3"

    db.save_board_snapshot(database, "2026-07-01", {"score": 40})
    db.save_board_snapshot(database, "2026-07-01", {"score": 82})

    assert db.load_board_snapshots(database, limit=10) == [
        {"score": 82, "date": "2026-07-01"}
    ]


def test_snapshot_limit_keeps_most_recent_dates_oldest_first(tmp_path: Path) -> None:
    database = tmp_path / "test.sqlite3"
    # Insertion order deliberately differs from date order.
    db.save_board_snapshot(database, "2026-07-02", {"score": 2})
    db.save_board_snapshot(database, "2026-07-03", {"score": 3})
    db.save_board_snapshot(database, "2026-07-01", {"score": 1})

    snapshots = db.load_board_snapshots(database, limit=2)

    assert [item["date"] for item in snapshots] == ["2026-07-02", "2026-07-03"]
    assert [item["score"] for item in snapshots] == [2, 3]


def test_snapshot_load_skips_malformed_rows(tmp_path: Path) -> None:
    database = tmp_path / "test.sqlite3"
    db.save_board_snapshot(database, "2026-07-01", {"score": 1})

    conn = sqlite3.connect(database)
    with conn:
        conn.execute(
            "INSERT INTO board_snapshots (snapshot_date, created_at, payload)"
            " VALUES (?, ?, ?)",
            ("2026-07-02", "2026-07-02T00:00:00+00:00", "{not json"),
        )
        # Valid JSON but not an object: also skipped.
        conn.execute(
            "INSERT INTO board_snapshots (snapshot_date, created_at, payload)"
            " VALUES (?, ?, ?)",
            ("2026-07-03", "2026-07-03T00:00:00+00:00", json.dumps([1, 2])),
        )
    conn.close()
    db.save_board_snapshot(database, "2026-07-04", {"score": 4})

    assert db.load_board_snapshots(database, limit=10) == [
        {"score": 1, "date": "2026-07-01"},
        {"score": 4, "date": "2026-07-04"},
    ]
