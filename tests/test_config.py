from pathlib import Path

import pytest

from app.config import find_group, load_watchlists, save_watchlists
from app.models import AssetConfig, GroupConfig


def test_load_watchlists_parses_groups(tmp_path: Path) -> None:
    path = tmp_path / "watchlists.yaml"
    path.write_text(
        """
groups:
  - name: TEST
    assets:
      - { symbol: aapl, type: equity, source: yahoo, exchange: NASDAQ, name: Apple }
""".strip()
    )

    groups = load_watchlists(path)

    assert len(groups) == 1
    assert groups[0].name == "TEST"
    assert groups[0].assets[0].symbol == "AAPL"
    assert groups[0].assets[0].source == "yahoo"


def test_load_watchlists_rejects_missing_groups(tmp_path: Path) -> None:
    path = tmp_path / "watchlists.yaml"
    path.write_text("assets: []")

    with pytest.raises(ValueError, match="groups"):
        load_watchlists(path)


def test_save_watchlists_round_trips_empty_group_and_assets(tmp_path: Path) -> None:
    path = tmp_path / "watchlists.yaml"
    groups = [
        GroupConfig(
            name="NEW_SECTOR",
            assets=[
                AssetConfig(
                    symbol="SPY",
                    type="etf",
                    source="yahoo",
                    exchange="NYSEARCA",
                    name="S&P 500 ETF",
                )
            ],
        ),
        GroupConfig(name="EMPTY", assets=[]),
    ]

    save_watchlists(path, groups)
    loaded = load_watchlists(path)

    assert loaded == groups
    assert find_group(loaded, "new_sector") == groups[0]
