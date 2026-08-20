"""Behavioral tests for the intraday Fringe auto-stop monitor.

Spec-loaded from scripts/ like the uploader tests; the network boundary
(fetch/close/alert) is replaced with recorders, the two-tick breach filter
and per-day alert dedupe run against a tmp state file.
"""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_UPLOADER_SPEC = importlib.util.spec_from_file_location(
    "vault_report_uploader", _SCRIPTS / "vault_report_uploader.py"
)
assert _UPLOADER_SPEC is not None and _UPLOADER_SPEC.loader is not None
_uploader = importlib.util.module_from_spec(_UPLOADER_SPEC)
sys.modules.setdefault("vault_report_uploader", _uploader)
_UPLOADER_SPEC.loader.exec_module(_uploader)

_MONITOR_SPEC = importlib.util.spec_from_file_location(
    "fringe_stop_monitor", _SCRIPTS / "fringe_stop_monitor.py"
)
assert _MONITOR_SPEC is not None and _MONITOR_SPEC.loader is not None
monitor = importlib.util.module_from_spec(_MONITOR_SPEC)
_MONITOR_SPEC.loader.exec_module(monitor)


def _idea(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 9,
        "ticker": "AMD",
        "direction": "long",
        "last": 440.0,
        "stop_price": 450.0,
        "unrealized_pct": -12.0,
        "size_notional": 1750.0,
    }
    base.update(overrides)
    return base


def test_close_rejects_insecure_remote_url() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        monitor.close_position("http://board.test", "sekrit", 9, "stop")


@pytest.fixture()
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state_path = tmp_path / "stop-monitor.json"
    calls: dict[str, list[Any]] = {"closes": [], "alerts": []}
    book: dict[str, Any] = {"open": []}

    monkeypatch.setattr(monitor.load_state, "__defaults__", (state_path,))
    monkeypatch.setattr(monitor.save_state, "__defaults__", (state_path,))
    monkeypatch.setattr(
        monitor,
        "load_config",
        lambda: {
            "BOARD_URL": "https://board.test",
            "EDIT_TOKEN": "sekrit",
            "ALERT_TARGET": "telegram:42",
        },
    )
    monkeypatch.setattr(monitor, "fetch_book", lambda base_url: book)

    def fake_alert(target: str, message: str) -> bool:
        calls["alerts"].append(message)
        return True

    monkeypatch.setattr(monitor, "send_alert", fake_alert)

    def fake_close(base_url: str, token: str, idea_id: int, reason: str) -> dict[str, Any]:
        calls["closes"].append((idea_id, reason))
        return {
            "closed": {
                "exit_price": 441.2,
                "realized_pct": -11.76,
                "realized_usd": -205.8,
            }
        }

    monkeypatch.setattr(monitor, "close_position", fake_close)
    return {"book": book, "calls": calls, "state": state_path}


def test_breach_requires_two_consecutive_ticks(wired: dict[str, Any]) -> None:
    wired["book"]["open"] = [_idea()]

    assert monitor.run() == 0  # tick 1: armed, no close
    assert wired["calls"]["closes"] == []
    assert json.loads(wired["state"].read_text())["breach"] == {"stop:AMD:long:9": 1}

    assert monitor.run() == 0  # tick 2: enforced
    assert len(wired["calls"]["closes"]) == 1
    idea_id, reason = wired["calls"]["closes"][0]
    assert idea_id == 9
    assert reason.startswith("auto-stop: long stop $450 breached at $440")
    assert "AMD" in wired["calls"]["alerts"][0]
    assert json.loads(wired["state"].read_text())["breach"] == {}


def test_recovered_mark_resets_the_streak(wired: dict[str, Any]) -> None:
    wired["book"]["open"] = [_idea()]
    assert monitor.run() == 0  # tick 1: breached once

    wired["book"]["open"] = [_idea(last=455.0)]  # wick recovered
    assert monitor.run() == 0
    assert json.loads(wired["state"].read_text())["breach"] == {}

    wired["book"]["open"] = [_idea()]  # breaches again: streak restarts at 1
    assert monitor.run() == 0
    assert wired["calls"]["closes"] == []


def test_short_breach_direction_and_intact_positions() -> None:
    assert monitor.stop_breached("long", 440.0, 450.0) is True
    assert monitor.stop_breached("long", 460.0, 450.0) is False
    assert monitor.stop_breached("short", 160.5, 160.0) is True
    assert monitor.stop_breached("short", 150.0, 160.0) is False


def test_stopless_big_move_alerts_once_per_day(wired: dict[str, Any]) -> None:
    wired["book"]["open"] = [
        _idea(id=1, ticker="AAPL", direction="short", stop_price=None, unrealized_pct=-11.3)
    ]

    assert monitor.run() == 0
    assert monitor.run() == 0  # second tick: deduped
    assert len(wired["calls"]["alerts"]) == 1
    assert "NO declared stop" in wired["calls"]["alerts"][0]
    assert wired["calls"]["closes"] == []

    # A mild adverse move never alerts.
    wired["book"]["open"] = [_idea(id=2, ticker="CEG", stop_price=None, unrealized_pct=-4.0)]
    assert monitor.run() == 0
    assert len(wired["calls"]["alerts"]) == 1


def test_stopless_alert_retries_after_delivery_failure(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    wired["book"]["open"] = [
        _idea(id=1, ticker="AAPL", direction="short", stop_price=None, unrealized_pct=-11.3)
    ]
    attempts: list[str] = []

    def flaky_alert(target: str, message: str) -> bool:
        attempts.append(message)
        return len(attempts) > 1

    monkeypatch.setattr(monitor, "send_alert", flaky_alert)

    assert monitor.run() == 0
    assert monitor.run() == 0
    assert monitor.run() == 0
    assert len(attempts) == 2


def test_target_hit_harvests_after_two_ticks(wired: dict[str, Any]) -> None:
    winner = _idea(
        id=6,
        ticker="AMD",
        stop_price=430.0,
        target_price=580.0,
        last=584.5,
        unrealized_pct=16.1,
    )
    wired["book"]["open"] = [winner]

    assert monitor.run() == 0  # tick 1: armed
    assert wired["calls"]["closes"] == []
    assert json.loads(wired["state"].read_text())["breach"] == {"target:AMD:long:6": 1}

    assert monitor.run() == 0  # tick 2: harvested
    (call,) = wired["calls"]["closes"]
    assert call[0] == 6
    assert call[1].startswith("auto-target: long target $580 reached at $584.5")
    assert "Target hit" in wired["calls"]["alerts"][0]
    assert "re-open a fresh" in wired["calls"]["alerts"][0]


def test_short_target_direction() -> None:
    assert monitor.target_reached("long", 584.5, 580.0) is True
    assert monitor.target_reached("long", 575.0, 580.0) is False
    assert monitor.target_reached("short", 139.0, 140.0) is True
    assert monitor.target_reached("short", 145.0, 140.0) is False


def test_intact_position_with_both_barriers_stays_open(wired: dict[str, Any]) -> None:
    wired["book"]["open"] = [
        _idea(last=500.0, stop_price=450.0, target_price=580.0, unrealized_pct=-0.7)
    ]
    assert monitor.run() == 0
    assert wired["calls"]["closes"] == []
    assert wired["calls"]["alerts"] == []
    assert json.loads(wired["state"].read_text())["breach"] == {}


def test_trail_stop_pure_ratchet() -> None:
    # Long: entry 500, declared stop 450, risk 50.
    assert monitor.trail_stop("long", 500.0, 450.0, 520.0, None) == 450.0  # +0.4R: untouched
    assert monitor.trail_stop("long", 500.0, 450.0, 550.0, None) == 500.0  # +1R: breakeven
    assert monitor.trail_stop("long", 500.0, 450.0, 600.0, None) == 550.0  # +2R: locks +1R
    assert monitor.trail_stop("long", 500.0, 450.0, 570.0, 550.0) == 550.0  # never loosens
    assert monitor.trail_stop("long", 500.0, 520.0, 600.0, None) == 520.0  # risk <= 0
    # Short mirror: entry 100, declared stop 110, risk 10.
    assert monitor.trail_stop("short", 100.0, 110.0, 95.0, None) == 110.0  # +0.5R: untouched
    assert monitor.trail_stop("short", 100.0, 110.0, 90.0, None) == 100.0  # +1R: breakeven
    assert monitor.trail_stop("short", 100.0, 110.0, 80.0, None) == 90.0  # +2R: locks +1R
    assert monitor.trail_stop("short", 100.0, 110.0, 85.0, 90.0) == 90.0  # never loosens


def test_trail_below_one_r_leaves_declared_stop_untouched(wired: dict[str, Any]) -> None:
    wired["book"]["open"] = [
        _idea(entry_price=500.0, stop_price=450.0, last=520.0, unrealized_pct=4.0)  # +0.4R
    ]

    assert monitor.run() == 0
    assert wired["calls"]["closes"] == []
    assert json.loads(wired["state"].read_text())["trail"] == {}


def test_trail_close_after_two_breach_ticks(wired: dict[str, Any]) -> None:
    wired["book"]["open"] = [
        _idea(entry_price=500.0, stop_price=450.0, last=600.0, unrealized_pct=20.0)  # +2R
    ]

    assert monitor.run() == 0  # trail ratchets to 550, no breach
    assert wired["calls"]["closes"] == []
    assert json.loads(wired["state"].read_text())["trail"] == {"AMD:long:9": 550.0}

    # Mark slips below the trail but holds above the declared stop.
    wired["book"]["open"] = [
        _idea(entry_price=500.0, stop_price=450.0, last=545.0, unrealized_pct=9.0)
    ]
    assert monitor.run() == 0  # tick 1: trail breach armed, declared stop intact
    assert wired["calls"]["closes"] == []
    assert json.loads(wired["state"].read_text())["breach"] == {"trail:AMD:long:9": 1}

    assert monitor.run() == 0  # tick 2: closed as auto-trail
    (call,) = wired["calls"]["closes"]
    assert call[0] == 9
    assert call[1].startswith("auto-trail: long trailed stop $550 breached at $545")
    assert "(declared stop $450)" in call[1]
    assert "Trail stop" in wired["calls"]["alerts"][0]
    assert json.loads(wired["state"].read_text())["breach"] == {}


def test_trail_ratchet_never_loosens(wired: dict[str, Any]) -> None:
    wired["book"]["open"] = [_idea(entry_price=500.0, stop_price=450.0, last=650.0)]
    assert monitor.run() == 0  # +3R: trail locks entry + 2R = 600
    assert json.loads(wired["state"].read_text())["trail"] == {"AMD:long:9": 600.0}

    wired["book"]["open"] = [_idea(entry_price=500.0, stop_price=450.0, last=610.0)]
    assert monitor.run() == 0  # +2.2R: candidate 560 loses to the stored 600
    assert json.loads(wired["state"].read_text())["trail"] == {"AMD:long:9": 600.0}
    assert wired["calls"]["closes"] == []


def test_short_trail_close_mirror(wired: dict[str, Any]) -> None:
    wired["book"]["open"] = [
        _idea(
            direction="short",
            entry_price=100.0,
            stop_price=110.0,
            last=80.0,
            unrealized_pct=20.0,
        )  # +2R: trail ratchets to 90
    ]
    assert monitor.run() == 0
    assert json.loads(wired["state"].read_text())["trail"] == {"AMD:short:9": 90.0}

    wired["book"]["open"] = [
        _idea(
            direction="short",
            entry_price=100.0,
            stop_price=110.0,
            last=92.0,
            unrealized_pct=8.0,
        )  # above the trail, below the declared stop
    ]
    assert monitor.run() == 0  # tick 1: armed
    assert wired["calls"]["closes"] == []
    assert monitor.run() == 0  # tick 2: closed
    (call,) = wired["calls"]["closes"]
    assert call[1].startswith("auto-trail: short trailed stop $90 breached at $92")
    assert "(declared stop $110)" in call[1]


def test_trail_entries_pruned_when_positions_leave_the_book(wired: dict[str, Any]) -> None:
    wired["book"]["open"] = [
        _idea(id=9, entry_price=500.0, stop_price=450.0, last=600.0),  # trail 550
        _idea(id=7, ticker="CEG", entry_price=200.0, stop_price=180.0, last=260.0),  # trail 240
    ]
    assert monitor.run() == 0
    assert json.loads(wired["state"].read_text())["trail"] == {
        "AMD:long:9": 550.0,
        "CEG:long:7": 240.0,
    }

    wired["book"]["open"] = [_idea(id=9, entry_price=500.0, stop_price=450.0, last=600.0)]
    assert monitor.run() == 0
    assert json.loads(wired["state"].read_text())["trail"] == {"AMD:long:9": 550.0}
