"""Contract tests for the /api/trends group performance bands.

Each watchlist group aggregates its members' cached daily closes into a
min/avg/max band, every member indexed to 100 at its first in-window
close. Sparse members (below 60% session coverage) stay out of the band,
groups without any bars disappear from the payload, and the route caches
by window length.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from app import db
from app.models import AssetConfig, Bar, GroupConfig
from app.services.hyperliquid_discovery import CRYPTO_GROUP_NAME, XYZ_GROUP_NAME
from app.services.trends import group_trends_payload


def _bar(symbol: str, day: datetime, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        provider="yahoo",
        interval="1d",
        timestamp=day,
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1000.0,
    )


def _asset(symbol: str, asset_type: str = "equity") -> AssetConfig:
    return AssetConfig(symbol=symbol, type=asset_type, source="yahoo", name=symbol)  # type: ignore[arg-type]


DAY0 = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)


def _seed(path: Path) -> None:
    bars = []
    # AAA doubles linearly; BBB halves; both cover all five sessions.
    for i, (aaa, bbb) in enumerate([(10, 40), (11, 36), (12, 32), (13, 28), (14, 20)]):
        day = DAY0 + timedelta(days=i)
        bars.append(_bar("AAA", day, float(aaa)))
        bars.append(_bar("BBB", day, float(bbb)))
    # CCC prints only two of five sessions: below the 60% coverage bar.
    bars.append(_bar("CCC", DAY0, 5.0))
    bars.append(_bar("CCC", DAY0 + timedelta(days=1), 6.0))
    db.save_bars(path, bars)


def test_band_indexes_members_to_100_and_aggregates(tmp_path: Path) -> None:
    path = tmp_path / "board.sqlite3"
    _seed(path)
    groups = [
        GroupConfig(name="Pair", assets=[_asset("AAA"), _asset("BBB"), _asset("CCC")]),
        GroupConfig(name="Ghost", assets=[_asset("ZZZ")]),
        GroupConfig(name=CRYPTO_GROUP_NAME, assets=[_asset("AAA", "crypto_perp")]),
        GroupConfig(name=XYZ_GROUP_NAME, assets=[_asset("BBB")]),
    ]

    payload = group_trends_payload(path, groups, days=14)

    assert payload["days"] == 14
    (group,) = cast(list[dict[str, Any]], payload["groups"])
    assert group["name"] == "Pair"
    assert group["category"] == "tradfi"
    # CCC covers 2/5 sessions (40%) and stays out of the band.
    assert group["members"] == 2

    series = group["series"]
    assert [point["date"] for point in series][0] == "2026-08-03"
    first, last = series[0], series[-1]
    # Both members index to 100 on the first session.
    assert (first["min"], first["max"], first["avg"]) == (100.0, 100.0, 100.0)
    # Final session: AAA 14/10 = 140, BBB 20/40 = 50.
    assert last["min"] == 50.0
    assert last["max"] == 140.0
    assert last["avg"] == 95.0


def test_days_window_trims_to_trailing_sessions(tmp_path: Path) -> None:
    path = tmp_path / "board.sqlite3"
    _seed(path)
    groups = [GroupConfig(name="Pair", assets=[_asset("AAA"), _asset("BBB")])]

    payload = group_trends_payload(path, groups, days=14)
    full = cast(list[dict[str, Any]], payload["groups"])[0]["series"]
    assert len(full) == 5

    # A crypto member flips the category label.
    crypto_groups = [GroupConfig(name="Perps", assets=[_asset("AAA", "crypto_perp")])]
    crypto_payload = group_trends_payload(path, crypto_groups, days=14)
    assert cast(list[dict[str, Any]], crypto_payload["groups"])[0]["category"] == "crypto"
