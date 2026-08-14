"""History for Hyperliquid symbols that have no watchlist (YAML) entry.

Charting a tape row must synthesize a hyperliquid crypto_perp asset; xyz
TradFi synthetics chart as equities, and unknown symbols stay unchartable.
"""

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
        assets=[AssetConfig(symbol="AAPL", type="equity", source="yahoo")],
    )
]

TAPE_CRYPTO = {"TRX"}
TAPE_TRADFI = {"MAGS", "AAPL"}


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

    def __init__(self, close: float) -> None:
        self._close = close
        self.calls: list[tuple[str, str]] = []

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        return []

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        self.calls.append((asset.symbol, interval))
        return [make_bar(asset.symbol, "yahoo", self._close, interval)]


class ScriptedHyperliquid(HyperliquidProvider):
    """Real HyperliquidProvider (isinstance matters for routing) with a warm cache."""

    def __init__(
        self,
        crypto: Iterable[str] = (),
        tradfi: Iterable[str] = (),
        close: float = 0.31,
    ) -> None:
        super().__init__()
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
        self.history_assets: list[AssetConfig] = []

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        self.history_assets.append(asset)
        return [make_bar(asset.symbol, "hyperliquid", self._close, interval)]


def make_service(
    tmp_path: Path, hyperliquid: HyperliquidProvider | None
) -> tuple[HistoryService, ScriptedHistory]:
    yahoo = ScriptedHistory(close=222.0)
    providers: dict[ProviderName, QuoteProvider] = {"yahoo": yahoo}
    if hyperliquid is not None:
        providers["hyperliquid"] = hyperliquid
    return HistoryService(tmp_path / "board.sqlite3", providers), yahoo


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", ["1h", "1d"])
async def test_tape_symbol_charts_via_synthetic_crypto_perp_asset(
    tmp_path: Path, interval: str
) -> None:
    hyperliquid = ScriptedHyperliquid(crypto=TAPE_CRYPTO, tradfi=TAPE_TRADFI)
    service, yahoo = make_service(tmp_path, hyperliquid)

    bars = await service.get_history(GROUPS, "trx", interval=interval, range_="1d")

    assert [(bar.provider, bar.close) for bar in bars] == [("hyperliquid", 0.31)]
    assert hyperliquid.history_assets == [
        AssetConfig(symbol="TRX", type="crypto_perp", source="hyperliquid")
    ]
    assert yahoo.calls == []


@pytest.mark.asyncio
async def test_tradfi_synthetic_charts_via_synthetic_equity_asset(tmp_path: Path) -> None:
    hyperliquid = ScriptedHyperliquid(crypto=TAPE_CRYPTO, tradfi=TAPE_TRADFI)
    service, yahoo = make_service(tmp_path, hyperliquid)

    bars = await service.get_history(GROUPS, "MAGS", interval="1h", range_="1d")

    assert [(bar.provider, bar.close) for bar in bars] == [("hyperliquid", 0.31)]
    assert hyperliquid.history_assets == [
        AssetConfig(symbol="MAGS", type="equity", source="hyperliquid")
    ]
    assert yahoo.calls == []


@pytest.mark.asyncio
async def test_unknown_symbol_returns_empty_without_fetch(tmp_path: Path) -> None:
    hyperliquid = ScriptedHyperliquid(crypto=TAPE_CRYPTO, tradfi=TAPE_TRADFI)
    service, yahoo = make_service(tmp_path, hyperliquid)

    bars = await service.get_history(GROUPS, "ZZZZ", interval="1h", range_="1d")

    assert bars == []
    assert hyperliquid.history_assets == []
    assert yahoo.calls == []


@pytest.mark.asyncio
async def test_tape_symbol_needs_a_hyperliquid_provider(tmp_path: Path) -> None:
    service, yahoo = make_service(tmp_path, hyperliquid=None)

    bars = await service.get_history(GROUPS, "TRX", interval="1h", range_="1d")

    assert bars == []
    assert yahoo.calls == []


@pytest.mark.asyncio
async def test_watchlist_symbol_still_uses_its_configured_provider(tmp_path: Path) -> None:
    """AAPL is a Hyperliquid TradFi synthetic, but its watchlist entry must win."""
    hyperliquid = ScriptedHyperliquid(crypto=TAPE_CRYPTO, tradfi=TAPE_TRADFI)
    service, yahoo = make_service(tmp_path, hyperliquid)

    bars = await service.get_history(GROUPS, "AAPL", interval="1d", range_="1y")

    assert [(bar.provider, bar.close) for bar in bars] == [("yahoo", 222.0)]
    assert yahoo.calls == [("AAPL", "1d")]
    assert hyperliquid.history_assets == []
