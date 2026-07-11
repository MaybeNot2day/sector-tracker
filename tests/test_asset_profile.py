from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from time import sleep

from app.models import AssetConfig
from app.services import asset_profile
from app.services.asset_profile import (
    AssetProfileService,
    _equity_metrics,
    _etf_metrics,
    _profile_from_yahoo_info,
)


def test_equity_average_volume_is_not_currency_prefixed() -> None:
    metrics = _equity_metrics(
        {
            "marketCap": 1_250_000_000,
            "averageVolume": 52_800_000,
            "fiftyTwoWeekHigh": 1255.19,
            "fiftyTwoWeekLow": 103.38,
        }
    )

    by_label = {str(metric["label"]): metric["value"] for metric in metrics}

    assert by_label["Market Cap"] == "$1.25B"
    assert by_label["Avg Volume"] == "52.8M"
    assert by_label["52W Range"] == "$103.38 - $1,255"
    assert "52W High" not in by_label
    assert "52W Low" not in by_label


def test_etf_average_volume_is_not_currency_prefixed() -> None:
    metrics = _etf_metrics(
        {
            "totalAssets": 18_000_000_000,
            "averageVolume": 1_240_000,
        }
    )

    by_label = {str(metric["label"]): metric["value"] for metric in metrics}

    assert by_label["Assets"] == "$18.00B"
    assert by_label["Avg Volume"] == "1.2M"


def test_non_usd_profile_monetary_metrics_are_converted(monkeypatch) -> None:
    monkeypatch.setattr(asset_profile, "_usd_money_divisor", lambda _info: 1_550.0)
    asset = AssetConfig(
        symbol="000660.KS",
        type="equity",
        source="yahoo",
        exchange="KRX",
        name="SK Hynix",
    )

    profile = _profile_from_yahoo_info(
        asset,
        {
            "currency": "KRW",
            "longName": "SK hynix Inc.",
            "marketCap": 1_550_000_000_000,
            "enterpriseValue": 3_100_000_000_000,
            "totalRevenue": 155_000_000_000,
            "averageVolume": 5_100_000,
            "fiftyTwoWeekLow": 155_000,
            "fiftyTwoWeekHigh": 3_100_000,
        },
    )
    by_label = {str(metric["label"]): metric["value"] for metric in profile["metrics"]}  # type: ignore[index]

    assert by_label["Market Cap"] == "$1.00B"
    assert by_label["EV"] == "$2.00B"
    assert by_label["Revenue"] == "$100.0M"
    assert by_label["Avg Volume"] == "5.1M"
    assert by_label["52W Range"] == "$100.00 - $2,000"


def test_partial_profile_failures_are_not_long_cached(monkeypatch) -> None:
    calls = {"count": 0}

    class FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def get_info(self) -> dict[str, object]:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary profile failure")
            return {
                "longName": "AST SpaceMobile, Inc.",
                "quoteType": "EQUITY",
                "sector": "Technology",
                "longBusinessSummary": "Builds a space-based cellular broadband network.",
                "marketCap": 1_000_000_000,
            }

    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
    service = AssetProfileService(cache_seconds=3600, failure_retry_seconds=0)
    asset = AssetConfig(symbol="ASTS", type="equity", source="yahoo", name="AST SpaceMobile")

    first = service.get_profile(asset)
    second = service.get_profile(asset)

    assert first["status"] == "partial"
    assert second["status"] == "ok"
    assert second["description"] == "Builds a space-based cellular broadband network."
    assert calls["count"] == 2


def test_profile_failure_cooldown_suppresses_retries_then_expires(monkeypatch) -> None:
    calls = {"count": 0}

    class RecoveringTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def get_info(self) -> dict[str, object]:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary outage")
            return {
                "longName": "AST SpaceMobile, Inc.",
                "longBusinessSummary": "Recovered profile",
                "marketCap": 1_000_000_000,
            }

    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", RecoveringTicker)
    service = AssetProfileService(cache_seconds=3600, failure_retry_seconds=120)
    asset = AssetConfig(symbol="ASTS", type="equity", source="yahoo")

    first = service.get_profile(asset)
    second = service.get_profile(asset)

    assert first is second
    assert first["status"] == "partial"
    assert calls["count"] == 1

    failed_at, failed_payload = service._failures["ASTS"]
    service._failures["ASTS"] = (failed_at - 121, failed_payload)
    recovered = service.get_profile(asset)

    assert recovered["status"] == "ok"
    assert recovered["description"] == "Recovered profile"
    assert calls["count"] == 2


def test_concurrent_profile_misses_collapse_per_symbol(monkeypatch) -> None:
    calls = 0
    calls_lock = Lock()

    class SlowTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def get_info(self) -> dict[str, object]:
            nonlocal calls
            with calls_lock:
                calls += 1
            sleep(0.03)
            return {"longBusinessSummary": "One shared result", "marketCap": 1_000_000}

    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", SlowTicker)
    service = AssetProfileService()
    asset = AssetConfig(symbol="AAPL", type="equity", source="yahoo")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(service.get_profile, [asset] * 8))

    assert calls == 1
    assert all(result is results[0] for result in results)


def test_different_profile_symbols_fetch_concurrently(monkeypatch) -> None:
    rendezvous = Barrier(2, timeout=1)

    class ParallelTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def get_info(self) -> dict[str, object]:
            rendezvous.wait()
            return {
                "longName": self.symbol,
                "longBusinessSummary": f"{self.symbol} profile",
                "marketCap": 1_000_000,
            }

    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", ParallelTicker)
    service = AssetProfileService()
    assets = [
        AssetConfig(symbol="AAPL", type="equity", source="yahoo"),
        AssetConfig(symbol="MSFT", type="equity", source="yahoo"),
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(service.get_profile, assets))

    assert [result["symbol"] for result in results] == ["AAPL", "MSFT"]
    assert all(result["status"] == "ok" for result in results)


def test_expired_good_profile_is_served_when_refresh_fails(monkeypatch) -> None:
    class FailingTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def get_info(self) -> dict[str, object]:
            raise RuntimeError("rate limited")

    cached = {
        "status": "ok",
        "symbol": "TSM",
        "name": "Taiwan Semiconductor Manufacturing Company Limited",
        "asset_type": "equity",
        "source": "yahoo",
        "exchange": "NYQ",
        "sector": "Technology",
        "industry": "Semiconductors",
        "website": None,
        "description": "Cached profile",
        "metrics": [{"label": "Market Cap", "value": "$1.00T"}],
    }
    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", FailingTicker)
    service = AssetProfileService(cache_seconds=1)
    service._cache["TSM"] = (0.0, cached)
    asset = AssetConfig(symbol="TSM", type="equity", source="yahoo", name="TSM")

    profile = service.get_profile(asset)

    assert profile is cached
    assert profile["description"] == "Cached profile"
