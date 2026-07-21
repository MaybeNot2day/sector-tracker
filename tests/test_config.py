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


def test_default_watchlist_has_unique_symbols() -> None:
    groups = load_watchlists(Path("config/watchlists.yaml"))
    symbols = [asset.symbol for group in groups for asset in group.assets]

    assert len(symbols) == len(set(symbols))


def test_save_watchlists_rejects_conflicting_duplicate_symbol_identities(
    tmp_path: Path,
) -> None:
    path = tmp_path / "watchlists.yaml"
    groups = [
        GroupConfig(
            name="EQUITIES",
            assets=[AssetConfig(symbol="ROBO", type="etf", source="yahoo")],
        ),
        GroupConfig(
            name="CRYPTO",
            assets=[AssetConfig(symbol="ROBO", type="crypto_perp", source="lighter")],
        ),
    ]

    with pytest.raises(ValueError, match="conflicting type/source/exchange"):
        save_watchlists(path, groups)

    assert not path.exists()


def test_save_watchlists_allows_identical_symbol_identity_across_groups(
    tmp_path: Path,
) -> None:
    path = tmp_path / "watchlists.yaml"
    shared = AssetConfig(symbol="SPY", type="etf", source="yahoo", exchange="NYSEARCA")
    groups = [
        GroupConfig(name="BENCHMARKS", assets=[shared]),
        GroupConfig(name="CORE", assets=[shared]),
    ]

    save_watchlists(path, groups)

    assert load_watchlists(path) == groups
