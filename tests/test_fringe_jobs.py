"""Unit tests for the Fringe risk-stats notepad job and weekly review job.

Spec-loaded from scripts/ like the stop-monitor tests; only the pure
computation helpers are exercised against a fake /api/fringe payload —
no HTTP, no subprocess.
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


_load("vault_report_uploader")
stats = _load("fringe_stats_notepad")
review = _load("fringe_weekly_review")


def _closed(
    ident: int, realized: float, closed: str = "2026-08-14", **overrides: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": ident,
        "ticker": "AMD",
        "direction": "long",
        "closed": closed,
        "realized_usd": realized,
    }
    base.update(overrides)
    return base


def _modes(closed_count: int, win_rate: float, streak: int, expectancy: float) -> dict[str, Any]:
    modes: dict[str, Any] = stats.compute_risk_modes(
        {
            "closed_count": closed_count,
            "win_rate_pct": win_rate,
            "expectancy_usd": expectancy,
            "losing_streak": streak,
        }
    )
    return modes


def test_weekly_report_rejects_insecure_remote_url() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        review.post_report("http://board.test", "sekrit", "2026-08-20", "body")


def test_streak_counts_consecutive_losses_newest_first() -> None:
    closed = [
        _closed(1, 50.0, "2026-08-10"),
        _closed(2, -10.0, "2026-08-12"),
        _closed(3, -20.0, "2026-08-13"),
        _closed(4, -30.0, "2026-08-14"),
    ]
    assert stats.compute_rolling_stats(closed)["losing_streak"] == 3


def test_streak_uses_id_to_break_same_day_ties() -> None:
    closed = [
        _closed(7, 100.0, "2026-08-14"),
        _closed(8, -5.0, "2026-08-14"),
    ]
    assert stats.compute_rolling_stats(closed)["losing_streak"] == 1
    assert stats.compute_rolling_stats(list(reversed(closed)))["losing_streak"] == 1


def test_zero_realized_breaks_the_streak() -> None:
    closed = [
        _closed(1, -10.0, "2026-08-11"),
        _closed(2, 0.0, "2026-08-12"),
        _closed(3, -10.0, "2026-08-13"),
        _closed(4, -10.0, "2026-08-14"),
    ]
    assert stats.compute_rolling_stats(closed)["losing_streak"] == 2


def test_rolling_win_rate_expectancy_and_profit_factor() -> None:
    closed = [
        _closed(1, 100.0),
        _closed(2, -50.0),
        _closed(3, 50.0),
        _closed(4, -100.0),
    ]
    result = stats.compute_rolling_stats(closed)
    assert result["closed_count"] == 4
    assert result["wins"] == 2
    assert result["win_rate_pct"] == 50.0
    assert result["expectancy_usd"] == 0.0
    assert result["profit_factor"] == 1.0
    assert result["avg_win_usd"] == 75.0
    assert result["avg_loss_usd"] == -75.0


def test_profit_factor_is_inf_without_losses() -> None:
    result = stats.compute_rolling_stats([_closed(1, 10.0)])
    assert result["profit_factor"] == float("inf")
    assert stats.format_profit_factor(result["profit_factor"]) == "inf"


def test_rolling_stats_ignore_unsized_closes() -> None:
    closed = [
        _closed(1, 100.0, "2026-08-12"),
        _closed(2, -40.0, "2026-08-13"),
        _closed(3, 0.0, "2026-08-14", realized_usd=None),
    ]
    result = stats.compute_rolling_stats(closed)
    assert result["closed_count"] == 2
    assert result["win_rate_pct"] == 50.0
    assert result["expectancy_usd"] == 30.0
    assert result["losing_streak"] == 1


def test_direction_buckets() -> None:
    closed = [
        _closed(1, 10.0, direction="long"),
        _closed(2, -5.0, direction="long"),
        _closed(3, 20.0, direction="short"),
    ]
    buckets = stats.compute_buckets(closed, stats.direction_bucket)
    assert buckets["long"] == {"count": 2, "wins": 1, "realized_usd": 5.0}
    assert buckets["short"] == {"count": 1, "wins": 1, "realized_usd": 20.0}


def test_asset_buckets_split_crypto_from_equity_futures() -> None:
    closed = [
        _closed(1, 10.0, ticker="BTC"),
        _closed(2, -5.0, ticker="HYPE"),
        _closed(3, 20.0, ticker="AMD"),
        _closed(4, -8.0, ticker="ES=F"),
    ]
    buckets = stats.compute_buckets(closed, stats.asset_bucket)
    assert buckets["crypto"] == {"count": 2, "wins": 1, "realized_usd": 5.0}
    assert buckets["equity/futures"] == {"count": 2, "wins": 1, "realized_usd": 12.0}


def test_giveback_math_for_a_long_and_a_short() -> None:
    open_items = [
        {
            "direction": "long",
            "entry_price": 100.0,
            "last": 110.0,
            "stop_price": 95.0,
            "size_notional": 1000.0,
        },
        {
            "direction": "short",
            "entry_price": 100.0,
            "last": 90.0,
            "stop_price": 105.0,
            "size_notional": 1000.0,
        },
        {  # missing entry price: skipped, never divides by zero
            "direction": "long",
            "entry_price": None,
            "last": 1.0,
            "stop_price": 0.5,
            "size_notional": 10.0,
        },
    ]
    giveback, pct = stats.compute_giveback(open_items, 10000.0)
    assert giveback == pytest.approx(300.0)
    assert pct == pytest.approx(3.0)


def test_half_size_starts_at_streak_three() -> None:
    assert _modes(6, 50.0, 2, 10.0)["half_size"] is False
    assert _modes(6, 50.0, 3, 10.0)["half_size"] is True


def test_no_new_opens_starts_at_streak_five() -> None:
    assert _modes(6, 50.0, 4, 10.0)["no_new_opens"] is False
    assert _modes(6, 50.0, 5, 10.0)["no_new_opens"] is True


def test_calibration_cap_needs_five_closed_below_35pct() -> None:
    assert _modes(4, 20.0, 0, -5.0)["calibration_cap"] is False
    assert _modes(5, 20.0, 0, -5.0)["calibration_cap"] is True
    assert _modes(5, 35.0, 0, -5.0)["calibration_cap"] is False


def test_expectancy_veto_needs_eight_closed() -> None:
    assert _modes(7, 40.0, 0, -1.0)["no_new_opens"] is False
    assert _modes(8, 40.0, 0, -1.0)["no_new_opens"] is True
    assert _modes(8, 40.0, 0, 0.0)["no_new_opens"] is False


def test_stats_block_shape_and_mode_labels() -> None:
    payload = {
        "summary": {"portfolio": {"equity": 10000.0}},
        "open": [],
        "closed": [
            _closed(0, -10.0, "2026-08-10"),
            _closed(1, 10.0, "2026-08-11"),
            _closed(2, -10.0, "2026-08-12"),
            _closed(3, -10.0, "2026-08-13"),
            _closed(4, -10.0, "2026-08-14"),
        ],
    }
    block = stats.build_stats_block(payload, "2026-08-14")
    lines = block.splitlines()
    assert lines[0] == "FRINGE RISK MODE — 2026-08-14"
    assert "- Mode: CALIBRATION_CAP · Breaker: HALF_SIZE" in lines[1]
    assert any(line.startswith("- Rolling: 5 closed · win rate 20.0%") for line in lines)
    assert any(line.startswith("- Direction: long ") for line in lines)
    assert any(line.startswith("- Asset: crypto ") for line in lines)
    assert any(line.startswith("- Open risk: giveback-to-stops ") for line in lines)


def test_review_markdown_covers_every_required_topic() -> None:
    payload = {
        "summary": {"portfolio": {"equity": 10250.0, "return_pct": 2.5}},
        "open": [
            {
                "direction": "long",
                "entry_price": 100.0,
                "last": 110.0,
                "stop_price": 95.0,
                "size_notional": 1000.0,
            }
        ],
        "closed": [
            _closed(1, 120.0, "2026-08-12", ticker="BTC"),
            _closed(2, -40.0, "2026-08-13", ticker="AMD", direction="short"),
            _closed(3, 60.0, "2026-07-01", ticker="SOL"),  # outside the 7-day window
        ],
    }
    today = date(2026, 8, 14)
    markdown, body = review.compose_review(payload, today)
    assert markdown.startswith("---\n")
    assert f"date: {today.isoformat()}" in markdown
    assert "type: research" in markdown
    assert "tags: [fringe, weekly-review, market-brief]" in markdown
    assert f"# Fringe Weekly Review — {today.isoformat()}" in body
    assert "---" not in body  # board body ships without frontmatter
    bullets = [line for line in body.splitlines() if line.startswith("- ")]
    assert 5 <= len(bullets) <= 7
    assert all(len(bullet.split()) <= 25 for bullet in bullets)
    joined = "\n".join(bullets)
    assert "Equity $10,250.00, total return +2.50%" in joined
    assert "This week: 2 closed, 1 win, net +$80.00." in joined
    assert "win rate" in joined and "profit factor" in joined and "expectancy" in joined
    assert "Best BTC +$120.00; worst AMD -$40.00." in joined
    assert "Risk mode NORMAL, breaker OFF" in joined
    assert "giveback-to-stops +$150.00, 1.5% of equity" in joined


def test_review_handles_an_empty_week() -> None:
    payload = {
        "summary": {"portfolio": {"equity": 10000.0, "return_pct": 0.0}},
        "open": [],
        "closed": [_closed(1, -20.0, "2026-07-01")],
    }
    _, body = review.compose_review(payload, date(2026, 8, 14))
    assert "No closed trades this week; best/worst not applicable." in body
