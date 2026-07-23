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
    monkeypatch.setattr(monitor, "send_alert", lambda target, msg: calls["alerts"].append(msg))

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
    assert json.loads(wired["state"].read_text())["breach"] == {"AMD:long:9": 1}

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
    wired["book"]["open"] = [
        _idea(id=2, ticker="CEG", stop_price=None, unrealized_pct=-4.0)
    ]
    assert monitor.run() == 0
    assert len(wired["calls"]["alerts"]) == 1
