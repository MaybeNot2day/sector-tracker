from pathlib import Path

import pytest

from app.config import Settings, find_group, load_watchlists, save_watchlists
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
            assets=[AssetConfig(symbol="ROBO", type="crypto_perp", source="hyperliquid")],
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


@pytest.mark.parametrize(
    ("username", "password"),
    [("reader", ""), ("", "secret")],
)
def test_read_credentials_require_a_pair(username: str, password: str) -> None:
    with pytest.raises(ValueError, match="must both be configured"):
        Settings(_env_file=None, read_username=username, read_password=password)


@pytest.mark.parametrize(
    ("username", "password"),
    [("", ""), ("reader", "secret")],
)
def test_read_credentials_preserve_public_or_private_mode(username: str, password: str) -> None:
    settings = Settings(_env_file=None, read_username=username, read_password=password)
    assert settings.read_username == username
    assert settings.read_password == password


def test_default_and_legacy_watchlists_use_runtime_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATCHLIST_PATH", raising=False)
    settings = Settings(_env_file=None)
    assert settings.watchlist_path == Path("data/watchlists.yaml")
    assert settings.watchlist_seed_path == Path("config/watchlists.yaml")
    for legacy in ("./config/watchlists.yaml", Path("config/watchlists.yaml")):
        assert Settings(_env_file=None, watchlist_path=legacy).watchlist_path == Path(
            "data/watchlists.yaml"
        )


def test_custom_watchlist_path_is_not_migrated(tmp_path: Path) -> None:
    path = tmp_path / "private" / "watchlists.yaml"
    assert Settings(_env_file=None, watchlist_path=path).watchlist_path == path


def test_basic_username_rejects_separator_and_preserves_utf8_whitespace() -> None:
    with pytest.raises(ValueError, match="READ_USERNAME must not contain"):
        Settings(_env_file=None, read_username="user:name", read_password="secret")
    settings = Settings(_env_file=None, read_username=" Réader ", read_password=" p:äss ")
    assert settings.read_username == " Réader "
    assert settings.read_password == " p:äss "


def test_first_boot_migrates_edited_seed_and_updates_preserve_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.main import ensure_runtime_watchlist

    monkeypatch.chdir(tmp_path)
    seed = tmp_path / "config/watchlists.yaml"
    seed.parent.mkdir()
    save_watchlists(seed, [GroupConfig(name="LEGACY EDITS", assets=[])])
    settings = Settings(
        _env_file=None,
        watchlist_path=Path("data/watchlists.yaml"),
        watchlist_seed_path=seed,
        read_username="",
        read_password="",
    )
    ensure_runtime_watchlist(settings)
    assert load_watchlists(settings.watchlist_path) == [GroupConfig(name="LEGACY EDITS", assets=[])]
    save_watchlists(settings.watchlist_path, [GroupConfig(name="UI EDITS", assets=[])])
    save_watchlists(seed, [GroupConfig(name="NEW REPOSITORY SEED", assets=[])])
    ensure_runtime_watchlist(settings)
    assert load_watchlists(settings.watchlist_path) == [GroupConfig(name="UI EDITS", assets=[])]
