from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app import db
from app.main import app
from app.models import AssetConfig, GroupConfig
from app.providers.hyperliquid import HyperliquidProvider
from app.scheduler import refresh_hyperliquid_discovery
from app.services.hyperliquid_discovery import (
    CRYPTO_GROUP_NAME,
    XYZ_GROUP_NAME,
    HyperliquidDiscoveryService,
)


class ScriptedProvider(HyperliquidProvider):
    def __init__(self, universe: dict[str, dict[str, str]]) -> None:
        super().__init__()
        self.universe = universe

    async def discovery_universe(self) -> dict[str, dict[str, str]]:
        return {kind: dict(listings) for kind, listings in self.universe.items()}


@pytest.mark.asyncio
async def test_baseline_then_new_listings_persist_and_delist_safely(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    provider = ScriptedProvider(
        {
            "crypto": {"BTC": "BTC", "ETH": "ETH"},
            "xyz": {"AAPL": "xyz:AAPL"},
        }
    )
    service = HyperliquidDiscoveryService(database, group_limit=2)
    base = [
        GroupConfig(
            name="CURATED",
            assets=[AssetConfig("EXISTING", "equity", "yahoo")],
        )
    ]

    # The first complete snapshot establishes a no-flood baseline.
    assert await service.scan(provider) is False
    assert service.merge_groups(base) == base
    assert service.status()["last_result"] == {
        "crypto": {"baseline": True, "added": 0, "deactivated": 0, "reactivated": 0},
        "xyz": {"baseline": True, "added": 0, "deactivated": 0, "reactivated": 0},
    }

    provider.universe = {
        "crypto": {"BTC": "BTC", "ETH": "ETH", "DUP": "DUP", "NEW": "NEW"},
        "xyz": {
            "AAPL": "xyz:AAPL",
            "DUP": "xyz:DUP",
            "EXISTING": "xyz:EXISTING",
            "NEWXYZ": "xyz:NEWXYZ",
        },
    }
    assert await service.scan(provider) is True
    groups = service.merge_groups(base)
    assert [group.name for group in groups] == ["CURATED", CRYPTO_GROUP_NAME, XYZ_GROUP_NAME]
    assert [asset.symbol for asset in groups[1].assets] == ["DUP", "NEW"]
    # Runtime merge prevents crypto/xyz and curated symbol identity collisions.
    assert [asset.symbol for asset in groups[2].assets] == ["NEWXYZ"]
    assert groups[2].assets[0].type == "equity"
    assert groups[2].assets[0].exchange == "HYPERLIQUID"

    # SQLite state survives a process restart.
    restarted = HyperliquidDiscoveryService(database, group_limit=2)
    assert [group.name for group in restarted.merge_groups(base)] == [
        "CURATED",
        CRYPTO_GROUP_NAME,
        XYZ_GROUP_NAME,
    ]

    # A fresh crypto snapshot deactivates a missing listing. Omitted xyz means
    # that dex is unavailable, so its persisted group must stay untouched.
    provider.universe = {"crypto": {"BTC": "BTC", "ETH": "ETH", "DUP": "DUP"}}
    assert await restarted.scan(provider) is True
    groups = restarted.merge_groups(base)
    assert [asset.symbol for asset in groups[1].assets] == ["DUP"]
    assert [asset.symbol for asset in groups[2].assets] == ["NEWXYZ"]


@pytest.mark.asyncio
async def test_scheduler_refreshes_runtime_groups_atomically(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    provider = ScriptedProvider({"crypto": {"BTC": "BTC"}, "xyz": {"AAPL": "xyz:AAPL"}})
    service = HyperliquidDiscoveryService(database)
    await service.scan(provider)
    base = [GroupConfig(name="BASE", assets=[])]
    state = SimpleNamespace(
        providers={"hyperliquid": provider},
        hyperliquid_discovery_service=service,
        base_groups=base,
        groups=base,
        watchlist_lock=asyncio.Lock(),
        trends_revision=0,
    )

    provider.universe["crypto"]["NEW"] = "NEW"
    assert await refresh_hyperliquid_discovery(state) is True
    assert [group.name for group in state.groups] == ["BASE", CRYPTO_GROUP_NAME]
    assert state.groups[1].assets[0].symbol == "NEW"
    assert state.trends_revision == 1


def test_groups_endpoint_keeps_auto_groups_read_only(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    db.sync_hyperliquid_listings(database, "crypto", {"BTC": "BTC"}, "2026-08-20T10:00:00Z")
    db.sync_hyperliquid_listings(
        database,
        "crypto",
        {"BTC": "BTC", "NEW": "NEW"},
        "2026-08-20T10:05:00Z",
    )
    service = HyperliquidDiscoveryService(database)
    base = [GroupConfig(name="BASE", assets=[])]
    state_names = (
        "base_groups",
        "groups",
        "hyperliquid_discovery_service",
        "settings",
        "providers",
    )
    saved = {name: getattr(app.state, name) for name in state_names if hasattr(app.state, name)}
    try:
        app.state.base_groups = base
        app.state.groups = service.merge_groups(base)
        app.state.settings = SimpleNamespace(edit_token="sekrit")
        app.state.providers = {"hyperliquid": ScriptedProvider({})}
        app.state.hyperliquid_discovery_service = service
        status = TestClient(app).get(
            "/api/hyperliquid-status",
            headers={"X-Edit-Token": "sekrit"},
        )
        assert status.status_code == 200
        assert status.json()["discovery"]["group_limit"] == 25
        response = TestClient(app).get("/api/groups")
        assert response.status_code == 200
        assert [group["name"] for group in response.json()["groups"]] == ["BASE"]
        assert [group.name for group in app.state.groups] == ["BASE", CRYPTO_GROUP_NAME]
    finally:
        for name in state_names:
            if name in saved:
                setattr(app.state, name, saved[name])
            elif hasattr(app.state, name):
                delattr(app.state, name)
