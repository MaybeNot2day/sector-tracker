from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import db
from app.models import AssetConfig, Bar, GroupConfig, Quote
from app.services.daily_board import DailyBoardService


def test_daily_board_builds_regime_breadth_and_theme_ranking(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    groups = [
        GroupConfig(
            name="ETF_MACRO",
            assets=[AssetConfig(symbol="SPY", type="etf", source="yahoo")],
        ),
        GroupConfig(
            name="TECH",
            assets=[AssetConfig(symbol="NVDA", type="equity", source="yahoo")],
        ),
    ]
    db.save_bars(database, _rising_bars("SPY") + _rising_bars("NVDA"))
    grouped_quotes = {
        "ETF_MACRO": [_quote("SPY", "etf", 126.0, 124.0)],
        "TECH": [_quote("NVDA", "equity", 128.0, 124.0)],
    }

    payload = DailyBoardService(database).build(groups, grouped_quotes)

    assert payload["regime"]["label"] == "RISK-ON / BROAD"  # type: ignore[index]
    assert payload["universe"]["above_200dma_pct"] == 100.0  # type: ignore[index]
    assert payload["universe"]["history_count"] == 2  # type: ignore[index]
    assert payload["themes"][0]["name"] == "TECH"  # type: ignore[index]
    assert len(payload["benchmarks"]) == 1  # type: ignore[arg-type]


def _rising_bars(symbol: str) -> list[Bar]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    bars: list[Bar] = []
    for index in range(210):
        close = 100 + index * 0.1
        bars.append(
            Bar(
                symbol=symbol,
                provider="yahoo",
                interval="1d",
                timestamp=start + timedelta(days=index),
                open=close - 0.5,
                high=close + 1,
                low=close - 1,
                close=close,
            )
        )
    return bars


def _quote(symbol: str, asset_type: str, last: float, previous_close: float) -> Quote:
    return Quote.from_last_and_prev_close(
        symbol=symbol,
        asset_type=asset_type,  # type: ignore[arg-type]
        provider="yahoo",
        last=last,
        previous_close=previous_close,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
