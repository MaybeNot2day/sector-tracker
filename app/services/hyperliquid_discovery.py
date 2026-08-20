"""Persistent discovery of newly listed Hyperliquid crypto and xyz markets."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import db
from app.models import AssetConfig, GroupConfig
from app.providers.hyperliquid import HyperliquidProvider

CRYPTO_GROUP_NAME = "HYPERLIQUID_NEW_CRYPTO"
XYZ_GROUP_NAME = "HYPERLIQUID_NEW_XYZ"


class HyperliquidDiscoveryService:
    """Baseline each dex once, then surface later listings as runtime groups."""

    def __init__(self, database_path: Path, *, group_limit: int = 25) -> None:
        self.database_path = database_path
        self.group_limit = group_limit
        self._listings = db.load_auto_hyperliquid_listings(database_path)
        self._lock = asyncio.Lock()
        self._last_scan: str | None = None
        self._last_result: dict[str, dict[str, int | bool]] = {}

    async def scan(self, provider: HyperliquidProvider) -> bool:
        """Persist fresh universes; return whether visible runtime groups changed."""
        async with self._lock:
            universe = await provider.discovery_universe()
            before = self._visible_signature()
            observed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            results: dict[str, dict[str, int | bool]] = {}
            for market_kind in ("crypto", "xyz"):
                listings = universe.get(market_kind)
                if not listings:
                    continue
                results[market_kind] = await asyncio.to_thread(
                    db.sync_hyperliquid_listings,
                    self.database_path,
                    market_kind,
                    listings,
                    observed_at,
                )
            self._listings = await asyncio.to_thread(
                db.load_auto_hyperliquid_listings, self.database_path
            )
            self._last_scan = observed_at
            self._last_result = results
            return before != self._visible_signature()

    def merge_groups(self, base_groups: list[GroupConfig]) -> list[GroupConfig]:
        """Append read-only auto groups without introducing symbol collisions."""
        groups = list(base_groups)
        occupied = {asset.symbol for group in base_groups for asset in group.assets}
        for market_kind, group_name in (
            ("crypto", CRYPTO_GROUP_NAME),
            ("xyz", XYZ_GROUP_NAME),
        ):
            assets: list[AssetConfig] = []
            for row in self._listings:
                if row.get("market_kind") != market_kind:
                    continue
                symbol = str(row.get("symbol") or "").upper()
                if not symbol or symbol in occupied:
                    continue
                first_seen = str(row.get("first_seen_at") or "")[:10]
                assets.append(
                    AssetConfig(
                        symbol=symbol,
                        type="crypto_perp" if market_kind == "crypto" else "equity",
                        source="hyperliquid",
                        exchange=None if market_kind == "crypto" else "HYPERLIQUID",
                        name=f"New listing · {first_seen}" if first_seen else "New listing",
                    )
                )
                occupied.add(symbol)
                if len(assets) >= self.group_limit:
                    break
            if assets:
                groups.append(GroupConfig(name=group_name, assets=assets))
        return groups

    def status(self) -> dict[str, Any]:
        groups = self.merge_groups([])
        return {
            "last_scan": self._last_scan,
            "group_limit": self.group_limit,
            "groups": {group.name: len(group.assets) for group in groups},
            "last_result": self._last_result,
        }

    def _visible_signature(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (str(row.get("market_kind") or ""), str(row.get("symbol") or ""))
            for row in self._listings
        )
