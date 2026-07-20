from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast, get_args

import yaml  # type: ignore[import-untyped]  # PyYAML does not ship typing metadata.
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import AssetConfig, AssetType, GroupConfig, ProviderName


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # When set, watchlist create/delete endpoints require the X-Edit-Token
    # header; leave empty for open local development.
    edit_token: str = ""
    database_path: Path = Path("./data/market_board.sqlite3")
    # The repo seed warms daily-board metrics on first boot in any fresh
    # environment; existing runtime databases are never overwritten.
    database_seed_path: Path = Path("./config/market_board_seed.sqlite3")
    watchlist_path: Path = Path("./config/watchlists.yaml")
    watchlist_seed_path: Path = Path("./config/watchlists.yaml")
    quote_poll_seconds: int = Field(default=10, ge=5)
    history_refresh_seconds: int = Field(default=3600, ge=300)
    crypto_etf_flow_cache_seconds: int = Field(default=900, ge=60)
    # TradingView economic-calendar enrichment for the Key Dates rail:
    # base cache TTL (drops to ~20s around scheduled releases) and the
    # comma-separated country filter sent to the calendar endpoint.
    econ_calendar_cache_seconds: int = Field(default=300, ge=30)
    econ_calendar_countries: str = "US,EU,DE,GB,JP,CN,CA,AU,NZ"
    # Public Telegram channels for the live news drawer, comma-separated
    # t.me handles. Polled every news_poll_seconds and pushed over the WS.
    news_telegram_channels: str = "marketfeed,RetardFrens,tradehaven,AGGRNEWSWIRE,WalterBloomberg"
    news_poll_seconds: int = Field(default=15, ge=5)
    enable_background_tasks: bool = True

    @property
    def news_channels(self) -> list[str]:
        return [
            channel.strip().lstrip("@")
            for channel in self.news_telegram_channels.split(",")
            if channel.strip()
        ]


def load_watchlists(path: Path) -> list[GroupConfig]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
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
        group_name = str(group_raw.get("name", "<unknown>"))
        assets = [_parse_asset(asset_raw, group_name) for asset_raw in assets_raw]
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
    # Write-then-rename: a crash mid-write must never leave a truncated
    # watchlist YAML behind (os.replace is atomic on the same filesystem).
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    os.replace(tmp_path, path)


def find_group(groups: list[GroupConfig], name: str) -> GroupConfig | None:
    wanted = _normalize_group_name(name)
    for group in groups:
        if _normalize_group_name(group.name) == wanted:
            return group
    return None


def _parse_asset(raw: dict[str, Any], group: str) -> AssetConfig:
    if not isinstance(raw, dict):
        raise ValueError("asset entries must be mappings")
    symbol = str(raw["symbol"]).upper()
    # cast() was a no-op here: a hand-edited YAML with e.g. source: binance
    # parsed fine and then silently never quoted. Check the Literal values.
    asset_type_raw = raw.get("type")
    if asset_type_raw not in get_args(AssetType):
        raise ValueError(f"group {group} asset {symbol}: unknown type {asset_type_raw!r}")
    asset_type = cast(AssetType, asset_type_raw)
    source_raw = raw.get("source")
    if source_raw not in get_args(ProviderName):
        raise ValueError(f"group {group} asset {symbol}: unknown source {source_raw!r}")
    source = cast(ProviderName, source_raw)
    return AssetConfig(
        symbol=symbol,
        type=asset_type,
        source=source,
        exchange=str(raw["exchange"]) if raw.get("exchange") else None,
        name=str(raw["name"]) if raw.get("name") else None,
    )


def _normalize_group_name(name: str) -> str:
    return " ".join(name.strip().split()).casefold()
