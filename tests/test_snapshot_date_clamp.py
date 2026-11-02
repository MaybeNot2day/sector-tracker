from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from app import db
from app.services import daily_board
from app.services.daily_board import DailyBoardService

# A regular Friday session (US trading day).
_FRIDAY = datetime(2026, 10, 23, 18, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily_board, "_utc_now", lambda: _FRIDAY)


def _overview(as_of: str) -> dict[str, object]:
    return {
        "as_of": as_of,
        "regime": {"label": "RISK-ON / BROAD", "tone": "risk_on"},
        "universe": {"total": 5, "quoted": 5, "advance_pct": 80.0},
        "themes": [],
        "rotation": {},
    }


def _service(database: Path) -> DailyBoardService:
    service = DailyBoardService(database)
    # Neutralize the per-process write throttle so the date clamp alone
    # decides whether a row lands.
    service._last_snapshot_write = -1e9
    return service


def test_past_as_of_neither_overwrites_nor_writes(tmp_path: Path) -> None:
    # The regression: during a provider outage as_of is the timestamp of the
    # last successful fetch, and keying by it overwrote a PAST day's
    # persisted snapshot with degraded metrics.
    database = tmp_path / "board.sqlite3"
    stale = _FRIDAY - timedelta(days=7)
    stale_date = stale.date().isoformat()
    db.save_board_snapshot(database, stale_date, {"score": 71})

    _service(database)._maybe_snapshot(_overview(stale.isoformat()))

    assert db.load_board_snapshots(database, limit=10) == [{"score": 71, "date": stale_date}]


def test_current_as_of_writes_condensed_snapshot_under_today(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    now = _FRIDAY

    _service(database)._maybe_snapshot(_overview(now.isoformat()))

    snapshots = db.load_board_snapshots(database, limit=10)
    assert len(snapshots) == 1
    row = snapshots[0]
    universe = cast(dict[str, object], row["universe"])
    assert row["date"] == now.date().isoformat()
    assert row["as_of"] == now.isoformat()
    assert row["regime"] == {"label": "RISK-ON / BROAD", "tone": "risk_on"}
    assert universe["quoted"] == 5
    assert universe["advance_pct"] == 80.0
    assert row["themes"] == []


@pytest.mark.parametrize(
    "weekend",
    [datetime(2026, 10, 24, 15, 0, tzinfo=UTC), datetime(2026, 10, 25, 15, 0, tzinfo=UTC)],
    ids=["saturday", "sunday"],
)
def test_weekend_writes_no_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, weekend: datetime
) -> None:
    # Prod stored Sat 10-24 and Sun 10-25 rows that were copies of Friday.
    database = tmp_path / "board.sqlite3"
    monkeypatch.setattr(daily_board, "_utc_now", lambda: weekend)

    _service(database)._maybe_snapshot(_overview(weekend.isoformat()))

    assert db.load_board_snapshots(database, limit=10) == []
