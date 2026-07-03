from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

import pytest

from app.models import AssetConfig, Bar, GroupConfig, ProviderName, Quote
from app.providers.base import QuoteProvider
from app.providers.lighter import LighterProvider
from app.services.history import HistoryService

GROUPS = [
    GroupConfig(
        name="TEST",
        assets=[
            AssetConfig(symbol="AAPL", type="equity", source="yahoo"),
            AssetConfig(symbol="BTC", type="crypto_perp", source="lighter"),
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


class ScriptedLighter(LighterProvider):
    """Real LighterProvider (isinstance matters for routing) with scripted bars."""

    def __init__(self, close: float | None, symbols: set[str]) -> None:
        super().__init__()
        self._details = {
            symbol: {"symbol": symbol, "market_id": index + 1, "status": "active"}
            for index, symbol in enumerate(sorted(symbols))
        }
        self._details_time = monotonic()
        self._close = close
        self.history_calls: list[tuple[str, str]] = []

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        self.history_calls.append((asset.symbol, interval))
        if self._close is None:
            return []
        return [make_bar(asset.symbol, "lighter", self._close, interval)]


@pytest.mark.asyncio
async def test_intraday_prefers_lighter_when_it_lists_the_symbol(tmp_path: Path) -> None:
    yahoo = ScriptedHistory(close=222.0)
    lighter = ScriptedLighter(close=111.0, symbols={"AAPL", "BTC"})
    service = HistoryService(tmp_path / "board.sqlite3", {"yahoo": yahoo, "lighter": lighter})

    bars = await service.get_history(GROUPS, "AAPL", interval="1h", range_="1d")

    assert [bar.close for bar in bars] == [111.0]
    assert bars[0].provider == "lighter"
    # Lighter answered, so the configured provider is never consulted.
    assert yahoo.calls == []


@pytest.mark.asyncio
async def test_intraday_falls_back_to_configured_provider_when_lighter_empty(
    tmp_path: Path,
) -> None:
    yahoo = ScriptedHistory(close=222.0)
    lighter = ScriptedLighter(close=None, symbols={"AAPL", "BTC"})
    service = HistoryService(tmp_path / "board.sqlite3", {"yahoo": yahoo, "lighter": lighter})

    bars = await service.get_history(GROUPS, "AAPL", interval="1h", range_="1d")

    assert lighter.history_calls == [("AAPL", "1h")]
    assert [bar.close for bar in bars] == [222.0]
    assert bars[0].provider == "yahoo"


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", ["1d", "1wk"])
async def test_daily_history_never_consults_lighter(tmp_path: Path, interval: str) -> None:
    yahoo = ScriptedHistory(close=222.0)
    lighter = ScriptedLighter(close=111.0, symbols={"AAPL", "BTC"})
    service = HistoryService(tmp_path / "board.sqlite3", {"yahoo": yahoo, "lighter": lighter})

    bars = await service.get_history(GROUPS, "AAPL", interval=interval, range_="1y")

    assert lighter.history_calls == []
    assert yahoo.calls == [("AAPL", interval)]
    assert [bar.close for bar in bars] == [222.0]


@pytest.mark.asyncio
async def test_intraday_skips_lighter_when_symbol_not_listed(tmp_path: Path) -> None:
    yahoo = ScriptedHistory(close=222.0)
    lighter = ScriptedLighter(close=111.0, symbols={"BTC"})  # no AAPL market
    service = HistoryService(tmp_path / "board.sqlite3", {"yahoo": yahoo, "lighter": lighter})

    bars = await service.get_history(GROUPS, "AAPL", interval="1h", range_="1d")

    assert lighter.history_calls == []
    assert [bar.close for bar in bars] == [222.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", ["1h", "1d"])
async def test_lighter_sourced_assets_use_configured_provider_once(
    tmp_path: Path, interval: str
) -> None:
    yahoo = ScriptedHistory(close=222.0)
    lighter = ScriptedLighter(close=111.0, symbols={"AAPL", "BTC"})
    service = HistoryService(tmp_path / "board.sqlite3", {"yahoo": yahoo, "lighter": lighter})

    bars = await service.get_history(GROUPS, "BTC", interval=interval, range_="1d")

    # Exactly one attempt: as the configured source, never a second time as
    # the intraday preference.
    assert lighter.history_calls == [("BTC", interval)]
    assert yahoo.calls == []
    assert [bar.close for bar in bars] == [111.0]
