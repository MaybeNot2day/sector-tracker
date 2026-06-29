from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

AssetType = Literal["equity", "etf", "crypto_perp", "crypto_spot", "index_proxy"]
ProviderName = Literal["yahoo", "stooq", "finnhub", "hyperliquid"]


@dataclass(frozen=True)
class Quote:
    symbol: str
    asset_type: AssetType
    provider: ProviderName
    last: float
    previous_close: float | None
    change_abs: float | None
    change_pct: float | None
    timestamp: datetime
    is_stale: bool = False
    error: str | None = None

    @classmethod
    def from_last_and_prev_close(
        cls,
        *,
        symbol: str,
        asset_type: AssetType,
        provider: ProviderName,
        last: float,
        previous_close: float | None,
        timestamp: datetime,
        is_stale: bool = False,
        error: str | None = None,
    ) -> Quote:
        if previous_close and previous_close != 0:
            change_abs = round(last - previous_close, 6)
            change_pct = round((last - previous_close) / previous_close * 100, 6)
        else:
            change_abs = None
            change_pct = None
        return cls(
            symbol=symbol,
            asset_type=asset_type,
            provider=provider,
            last=last,
            previous_close=previous_close,
            change_abs=change_abs,
            change_pct=change_pct,
            timestamp=timestamp,
            is_stale=is_stale,
            error=error,
        )


@dataclass(frozen=True)
class Bar:
    symbol: str
    provider: ProviderName
    interval: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


@dataclass(frozen=True)
class AssetConfig:
    symbol: str
    type: AssetType
    source: ProviderName
    exchange: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class GroupConfig:
    name: str
    assets: list[AssetConfig]

