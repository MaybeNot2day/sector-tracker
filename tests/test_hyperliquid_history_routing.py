from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

import pytest

from app.models import AssetConfig, Bar, GroupConfig, ProviderName, Quote
from app.providers.base import QuoteProvider
from app.providers.hyperliquid import HyperliquidProvider
from app.services.history import HistoryService

GROUPS = [
    GroupConfig(
        name="TEST",
        assets=[
            AssetConfig(symbol="AAPL", type="equity", source="yahoo"),
            AssetConfig(symbol="ROBO", type="etf", source="yahoo"),
            AssetConfig(symbol="BTC", type="crypto_perp", source="hyperliquid"),
        ],
    )
]


def make_bar(symbol: str, provider: ProviderName, close: float, interval: str) -> Bar:
    return Bar(
        symbol=symbol,
        provider=provider,
        interval=interval,
        timestamp=datetime.now(UTC) - timedelta(minutes=5),
        open=close,
        high=close,
        low=close,
        close=close,
    )


class ScriptedHistory(QuoteProvider):
    name = "yahoo"

    def __init__(self, close: float | None) -> None:
        self._close = close
        self.calls: list[tuple[str, str]] = []

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        return []

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        self.calls.append((asset.symbol, interval))
        if self._close is None:
            return []
        return [make_bar(asset.symbol, "yahoo", self._close, interval)]


class ScriptedHyperliquid(HyperliquidProvider):
    """Real HyperliquidProvider (isinstance matters for routing) with scripted bars."""

    def __init__(
        self,
        close: float | None,
        *,
        crypto: Iterable[str] = (),
        tradfi: Iterable[str] = (),
    ) -> None:
        super().__init__()
        # Warm market maps: the routing gate only lets a Hyperliquid market
        # serve a TradFi asset's candles when it is classified as a TradFi
        # synthetic (crypto tokens must never chart same-named equities).
        self._crypto = {
            symbol.upper(): {"coin": symbol.upper(), "display": symbol.upper(), "last": 1.0}
            for symbol in crypto
        }
        self._tradfi = {
            symbol.upper(): {
                "coin": f"xyz:{symbol.upper()}",
                "display": symbol.upper(),
                "last": 1.0,
            }
            for symbol in tradfi
        }
        self._markets_time = monotonic()
        self._close = close
        self.history_calls: list[tuple[str, str]] = []

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        self.history_calls.append((asset.symbol, interval))
        if self._close is None:
            return []
        return [make_bar(asset.symbol, "hyperliquid", self._close, interval)]


@pytest.mark.asyncio
async def test_intraday_prefers_hyperliquid_when_it_lists_the_symbol(tmp_path: Path) -> None:
    yahoo = ScriptedHistory(close=222.0)
    hyperliquid = ScriptedHyperliquid(close=111.0, crypto={"BTC"}, tradfi={"AAPL"})
    service = HistoryService(
        tmp_path / "board.sqlite3", {"yahoo": yahoo, "hyperliquid": hyperliquid}
    )

    bars = await service.get_history(GROUPS, "AAPL", interval="1h", range_="1d")

    assert [bar.close for bar in bars] == [111.0]
    assert bars[0].provider == "hyperliquid"
    # Hyperliquid answered, so the configured provider is never consulted.
    assert yahoo.calls == []


@pytest.mark.asyncio
async def test_intraday_falls_back_to_configured_provider_when_hyperliquid_empty(
    tmp_path: Path,
) -> None:
    yahoo = ScriptedHistory(close=222.0)
    hyperliquid = ScriptedHyperliquid(close=None, crypto={"BTC"}, tradfi={"AAPL"})
    service = HistoryService(
        tmp_path / "board.sqlite3", {"yahoo": yahoo, "hyperliquid": hyperliquid}
    )

    bars = await service.get_history(GROUPS, "AAPL", interval="1h", range_="1d")

    assert hyperliquid.history_calls == [("AAPL", "1h")]
    assert [bar.close for bar in bars] == [222.0]
    assert bars[0].provider == "yahoo"


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", ["1d", "1wk"])
async def test_daily_history_never_consults_hyperliquid(tmp_path: Path, interval: str) -> None:
    yahoo = ScriptedHistory(close=222.0)
    hyperliquid = ScriptedHyperliquid(close=111.0, crypto={"BTC"}, tradfi={"AAPL"})
    service = HistoryService(
        tmp_path / "board.sqlite3", {"yahoo": yahoo, "hyperliquid": hyperliquid}
    )

    bars = await service.get_history(GROUPS, "AAPL", interval=interval, range_="1y")

    assert hyperliquid.history_calls == []
    assert yahoo.calls == [("AAPL", interval)]
    assert [bar.close for bar in bars] == [222.0]


@pytest.mark.asyncio
async def test_intraday_skips_hyperliquid_when_symbol_not_listed(tmp_path: Path) -> None:
    yahoo = ScriptedHistory(close=222.0)
    hyperliquid = ScriptedHyperliquid(close=111.0, crypto={"BTC"})  # no AAPL market
    service = HistoryService(
        tmp_path / "board.sqlite3", {"yahoo": yahoo, "hyperliquid": hyperliquid}
    )

    bars = await service.get_history(GROUPS, "AAPL", interval="1h", range_="1d")

    assert hyperliquid.history_calls == []
    assert [bar.close for bar in bars] == [222.0]


@pytest.mark.asyncio
async def test_intraday_skips_hyperliquid_when_market_is_crypto_classified(
    tmp_path: Path,
) -> None:
    """Hyperliquid's ROBO is a crypto token, not the robotics ETF."""
    yahoo = ScriptedHistory(close=222.0)
    hyperliquid = ScriptedHyperliquid(close=111.0, crypto={"BTC", "ROBO"})
    service = HistoryService(
        tmp_path / "board.sqlite3", {"yahoo": yahoo, "hyperliquid": hyperliquid}
    )

    bars = await service.get_history(GROUPS, "ROBO", interval="1h", range_="1d")

    assert hyperliquid.history_calls == []
    assert yahoo.calls == [("ROBO", "1h")]
    assert [bar.close for bar in bars] == [222.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", ["1h", "1d"])
async def test_hyperliquid_sourced_assets_use_configured_provider_once(
    tmp_path: Path, interval: str
) -> None:
    yahoo = ScriptedHistory(close=222.0)
    hyperliquid = ScriptedHyperliquid(close=111.0, crypto={"BTC"}, tradfi={"AAPL"})
    service = HistoryService(
        tmp_path / "board.sqlite3", {"yahoo": yahoo, "hyperliquid": hyperliquid}
    )

    bars = await service.get_history(GROUPS, "BTC", interval=interval, range_="1d")

    # Exactly one attempt: as the configured source, never a second time as
    # the intraday preference.
    assert hyperliquid.history_calls == [("BTC", interval)]
    assert yahoo.calls == []
    assert [bar.close for bar in bars] == [111.0]
