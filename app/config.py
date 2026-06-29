from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import AssetConfig, AssetType, GroupConfig, ProviderName


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    finnhub_api_key: str = ""
    database_path: Path = Path("./data/market_board.sqlite3")
    watchlist_path: Path = Path("./config/watchlists.yaml")
    watchlist_seed_path: Path = Path("./config/watchlists.yaml")
    quote_poll_seconds: int = Field(default=10, ge=5)
    history_refresh_seconds: int = Field(default=3600, ge=300)
    crypto_etf_flow_cache_seconds: int = Field(default=900, ge=60)
    enable_background_tasks: bool = True


def load_watchlists(path: Path) -> list[GroupConfig]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or "groups" not in raw:
        raise ValueError("watchlist YAML must contain top-level 'groups'")
    if not isinstance(raw["groups"], list):
        raise ValueError("watchlist YAML 'groups' must be a list")

    groups: list[GroupConfig] = []
    for group_raw in raw["groups"]:
        if not isinstance(group_raw, dict):
            raise ValueError("each group must be a mapping")
        assets_raw = group_raw.get("assets", [])
        if not isinstance(assets_raw, list):
            raise ValueError(f"group {group_raw.get('name', '<unknown>')} assets must be a list")
        assets = [_parse_asset(asset_raw) for asset_raw in assets_raw]
        groups.append(GroupConfig(name=str(group_raw["name"]), assets=assets))
    return groups


def save_watchlists(path: Path, groups: list[GroupConfig]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "groups": [
            {
                "name": group.name,
                "assets": [
                    {
                        key: value
                        for key, value in {
                            "symbol": asset.symbol,
                            "type": asset.type,
                            "source": asset.source,
                            "exchange": asset.exchange,
                            "name": asset.name,
                        }.items()
                        if value is not None
                    }
                    for asset in group.assets
                ],
            }
            for group in groups
        ]
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def find_group(groups: list[GroupConfig], name: str) -> GroupConfig | None:
    wanted = _normalize_group_name(name)
    for group in groups:
        if _normalize_group_name(group.name) == wanted:
            return group
    return None


def _parse_asset(raw: dict[str, Any]) -> AssetConfig:
    if not isinstance(raw, dict):
        raise ValueError("asset entries must be mappings")
    return AssetConfig(
        symbol=str(raw["symbol"]).upper(),
        type=cast(AssetType, raw["type"]),
        source=cast(ProviderName, raw["source"]),
        exchange=str(raw["exchange"]) if raw.get("exchange") else None,
        name=str(raw["name"]) if raw.get("name") else None,
    )


def _normalize_group_name(name: str) -> str:
    return " ".join(name.strip().split()).casefold()
