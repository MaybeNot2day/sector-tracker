from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

import pytest

E2E_ENABLED = bool(os.environ.get("RUN_PLAYWRIGHT") or os.environ.get("BOARD_E2E_BASE_URL"))

if E2E_ENABLED:
    try:
        from playwright.sync_api import (
            Browser,
            Page,
            expect,
            sync_playwright,
        )
        from playwright.sync_api import (
            Error as PlaywrightError,
        )
    except ModuleNotFoundError:
        pytest.skip(
            "Python Playwright smoke tests require the 'playwright' package",
            allow_module_level=True,
        )
else:
    Browser = Any  # type: ignore[assignment]
    Page = Any  # type: ignore[assignment]

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not E2E_ENABLED,
        reason="browser smoke tests are opt-in; set RUN_PLAYWRIGHT=1 or BOARD_E2E_BASE_URL",
    ),
]

TAPE_SYMBOL = "ZEC"


@pytest.fixture(scope="session")
def base_url() -> Iterator[str]:
    configured = os.environ.get("BOARD_E2E_BASE_URL")
    if configured:
        yield configured.rstrip("/")
        return

    port = _free_port()
    python = Path(".venv/bin/python")
    executable = str(python) if python.exists() else sys.executable
    env = {
        **os.environ,
        "ENABLE_BACKGROUND_TASKS": "false",
        "RUN_PLAYWRIGHT": "1",
    }
    process = subprocess.Popen(
        [
            executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(url, process)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(
                "Playwright Chromium is not installed; run `python -m playwright install chromium` "
                f"to enable these smoke tests ({exc})"
            )
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture()
def page(browser: Browser) -> Iterator[Page]:
    page = browser.new_page(viewport={"width": 1440, "height": 1100})
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.add_init_script(
        """
        (() => {
          class QuietWebSocket {
            static CONNECTING = 0;
            static OPEN = 1;
            static CLOSING = 2;
            static CLOSED = 3;
            constructor(url) {
              this.url = url;
              this.readyState = QuietWebSocket.CLOSED;
            }
            addEventListener() {}
            removeEventListener() {}
            send() {}
            close() { this.readyState = QuietWebSocket.CLOSED; }
          }
          window.WebSocket = QuietWebSocket;
        })();
        """
    )
    _stub_board_apis(page)
    try:
        yield page
    finally:
        try:
            page.close()
        finally:
            if page_errors:
                pytest.fail("Unexpected pageerror events:\n" + "\n".join(page_errors))


def test_daily_board_loads_without_page_errors_and_renders_core_sections(
    page: Page,
    base_url: str,
) -> None:
    _goto_board(page, base_url)

    expect(page.locator("#daily-view")).to_be_visible()
    expect(page.locator("#status-strip")).to_be_visible()
    expect(page.locator("#feed-mode")).to_contain_text(re.compile(r"WS Live|Poll 10s"))
    expect(page.locator("#live-freshness")).to_contain_text("Updated")
    expect(page.locator("#daily-board").get_by_role("heading", name="Benchmarks")).to_be_visible()
    expect(
        page.locator("#daily-board").get_by_role("heading", name="Dominant Themes")
    ).to_be_visible()

    benchmark_cards = page.locator("#daily-board .benchmark-card")
    expect(benchmark_cards.nth(0)).to_be_visible()
    assert benchmark_cards.count() >= 1

    theme_rows = page.locator("#daily-board .theme-table tbody tr")
    expect(theme_rows.nth(0)).to_be_visible()
    assert theme_rows.count() >= 1


def test_markets_tabs_render_rows_and_open_canvas_chart(page: Page, base_url: str) -> None:
    _goto_board(page, base_url)
    page.locator("#markets-tab").click()
    expect(page.locator("#markets-view")).to_be_visible()

    for category in ("tradfi", "crypto", "commodities"):
        button = page.locator(f'.category-tabs button[data-category="{category}"]')
        button.click()
        expect(button).to_have_attribute("aria-selected", "true")
        _wait_for_visible_market_row(page)
        assert _visible_market_row_count(page) >= 1, (
            f"{category} should render at least one visible row"
        )

    page.locator('.category-tabs button[data-category="tradfi"]').click()
    market_row = page.locator("#board .group-panel:not(.tape-panel) .asset-row").nth(0)
    expect(market_row).to_be_visible()
    market_row.click()

    expect(page.locator("#chart-modal")).to_have_attribute("aria-hidden", "false")
    _expect_chart_canvas_content(page)


def test_tape_deep_link_restores_crypto_chart_when_tape_row_exists(
    page: Page,
    base_url: str,
) -> None:
    _goto_board(page, base_url, f"#view=markets&cat=crypto&chart={TAPE_SYMBOL}")
    expect(page.locator("#markets-view")).to_be_visible()

    tape_row = page.locator(f'#crypto-tape .asset-row[data-symbol="{TAPE_SYMBOL}"]')
    try:
        expect(tape_row).to_be_visible(timeout=3_000)
    except AssertionError:
        pytest.skip(f"crypto tape row {TAPE_SYMBOL} is not available in this run")

    expect(page.locator("#chart-modal")).to_have_attribute("aria-hidden", "false")
    expect(page.locator("#chart-title")).to_have_text(TAPE_SYMBOL)
    _expect_chart_canvas_content(page)


def test_editor_failed_save_preserves_typed_asset_fields(page: Page, base_url: str) -> None:
    _goto_board(page, base_url)

    page.locator("#editor-open").click()
    expect(page.locator("#editor-modal")).to_have_attribute("aria-hidden", "false")
    page.locator("#asset-group option").nth(0).wait_for(state="attached")

    symbol = "NO_SUCH_SMOKE_SYMBOL"
    name = "Typed Smoke Name"
    page.locator("#asset-group").select_option("QA_EQUITY")
    page.locator("#asset-symbol").fill(symbol)
    page.locator("#asset-name").fill(name)
    page.locator('#asset-form button[type="submit"]').click()

    expect(page.locator("#editor-status")).to_contain_text("Symbol not recognized")
    expect(page.locator("#asset-symbol")).to_have_value(symbol)
    expect(page.locator("#asset-name")).to_have_value(name)

    page.locator("#editor-close").click()
    expect(page.locator("#editor-modal")).to_have_attribute("aria-hidden", "true")


def _goto_board(page: Page, base_url: str, fragment: str = "") -> None:
    page.goto(f"{base_url.rstrip('/')}/{fragment}", wait_until="domcontentloaded")
    expect(page.locator("#status-copy")).to_contain_text("QUOTED", timeout=10_000)


def _expect_chart_canvas_content(page: Page) -> None:
    page.wait_for_function(
        """
        () => Array.from(document.querySelectorAll('#chart canvas')).some((canvas) => {
          if (canvas.width < 50 || canvas.height < 50) return false;
          const context = canvas.getContext('2d');
          if (!context) return false;
          const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
          for (let index = 3; index < data.length; index += 4) {
            if (data[index] !== 0) return true;
          }
          return false;
        })
        """,
        timeout=10_000,
    )


def _wait_for_visible_market_row(page: Page) -> None:
    page.wait_for_function(
        """
        () => Array.from(document.querySelectorAll('#markets-view .asset-row')).some((row) => {
          const style = window.getComputedStyle(row);
          const rect = row.getBoundingClientRect();
          return style.visibility !== 'hidden'
            && style.display !== 'none'
            && rect.width > 0
            && rect.height > 0;
        })
        """,
        timeout=10_000,
    )


def _visible_market_row_count(page: Page) -> int:
    return page.locator("#markets-view .asset-row").evaluate_all(
        """
        (rows) => rows.filter((row) => {
          const style = window.getComputedStyle(row);
          const rect = row.getBoundingClientRect();
          return style.visibility !== 'hidden'
            && style.display !== 'none'
            && rect.width > 0
            && rect.height > 0;
        }).length
        """
    )


def _stub_board_apis(page: Page) -> None:
    def handle(route: Any) -> None:
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if path == "/api/quotes":
            _fulfill_json(route, BOARD_PAYLOAD)
        elif path == "/api/crypto-etf-flows":
            _fulfill_json(route, CRYPTO_ETF_FLOWS)
        elif path == "/api/snapshots":
            _fulfill_json(route, SNAPSHOTS_PAYLOAD)
        elif path == "/api/news":
            _fulfill_json(route, NEWS_PAYLOAD)
        elif path == "/api/groups" and request.method == "GET":
            _fulfill_json(route, WATCHLIST_PAYLOAD)
        elif (
            path.startswith("/api/groups/")
            and path.endswith("/assets")
            and request.method == "POST"
        ):
            _fulfill_json(route, {"detail": "symbol_not_found"}, status=422)
        elif path.startswith("/api/history/"):
            symbol = unquote(path.rsplit("/", 1)[1]).upper()
            _fulfill_json(route, _history_payload(symbol))
        elif path.startswith("/api/profile/"):
            symbol = unquote(path.rsplit("/", 1)[1]).upper()
            _fulfill_json(route, _profile_payload(symbol))
        else:
            route.continue_()

    page.route("**/api/**", handle)


def _fulfill_json(route: Any, payload: dict[str, Any], *, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    health_url = f"{url}/api/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            pytest.fail(f"uvicorn exited before serving health check:\n{output}")
        try:
            with urlopen(health_url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except URLError:
            time.sleep(0.1)
    pytest.fail(f"uvicorn did not answer {health_url} within 15s")


def _iso(offset_minutes: int = 0) -> str:
    return (
        datetime(2026, 7, 9, 14, 30, tzinfo=UTC) + timedelta(minutes=offset_minutes)
    ).isoformat()


def _quote(
    symbol: str, last: float, previous_close: float, *, provider: str = "yahoo"
) -> dict[str, Any]:
    change = last - previous_close
    return {
        "symbol": symbol,
        "provider": provider,
        "last": last,
        "previous_close": previous_close,
        "change_abs": round(change, 4),
        "change_pct": round((change / previous_close) * 100, 6),
        "timestamp": _iso(),
        "is_stale": False,
        "currency": "USD",
        "display_last": last,
        "display_previous_close": previous_close,
        "display_change_abs": round(change, 4),
        "display_change_pct": round((change / previous_close) * 100, 6),
        "display_currency": "USD",
    }


def _summary(start: float) -> dict[str, Any]:
    return {
        "sparkline": [start + index * 0.35 for index in range(32)],
        "rvol": 1.7,
        "open_change_pct": 0.8,
        "performance": {"1D": 1.2, "1W": 2.4, "1M": 5.1, "3M": 9.5, "YTD": 13.0, "1Y": 18.2},
        "range_52w": {
            "low": start * 0.72,
            "high": start * 1.26,
            "current": start * 1.04,
            "position_pct": 62,
            "off_low_pct": 44,
            "off_high_pct": -17,
        },
    }


def _asset(
    group: str,
    symbol: str,
    asset_type: str,
    source: str,
    name: str,
    last: float,
    previous_close: float,
    *,
    exchange: str = "US",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "type": asset_type,
        "source": source,
        "exchange": exchange,
        "name": name,
        "groupLabel": group,
        "quote": _quote(symbol, last, previous_close, provider=source),
        "summary": _summary(previous_close),
    }


def _theme(name: str, rank: int, score: int, change_1d: float, change_5d: float) -> dict[str, Any]:
    return {
        "name": name,
        "rank": rank,
        "score": score,
        "count": 2,
        "change_1d": change_1d,
        "change_5d": change_5d,
        "acceleration": round(change_1d - change_5d / 5, 2),
        "status": "DOMINANT" if score >= 75 else "STRONG",
    }


def _history_payload(symbol: str) -> dict[str, Any]:
    start = datetime(2026, 4, 1, tzinfo=UTC)
    bars = []
    base = 100.0 + (sum(ord(char) for char in symbol) % 25)
    for index in range(90):
        close = base + index * 0.45 + (index % 5) * 0.12
        bars.append(
            {
                "timestamp": (start + timedelta(days=index)).isoformat(),
                "open": round(close - 0.25, 4),
                "high": round(close + 0.9, 4),
                "low": round(close - 0.8, 4),
                "close": round(close, 4),
                "volume": 1_000_000 + index * 2_500,
            }
        )
    return {"symbol": symbol, "interval": "1d", "range": "1y", "bars": bars}


def _profile_payload(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": f"{symbol} Smoke Profile",
        "asset_type": "equity",
        "sector": "Technology",
        "industry": "Test Data",
        "exchange": "US",
        "description": "Deterministic profile data used by the browser smoke suite.",
        "metrics": [{"label": "Market Cap", "value": "$1.2T"}],
    }


TRADFI_ASSET = _asset("QA_EQUITY", "SPY", "etf", "yahoo", "S&P 500 ETF", 632.4, 625.0)
CRYPTO_ASSET = _asset(
    "QA_CRYPTO", "BTC", "crypto_perp", "lighter", "Bitcoin Perp", 118_500.0, 116_000.0
)
COMMODITY_ASSET = _asset(
    "QA_COMMODITIES", "GC=F", "future", "yahoo", "Gold Futures", 3_350.0, 3_320.0
)
THEMES = [
    _theme("QA_EQUITY", 1, 82, 1.18, 3.4),
    _theme("QA_CRYPTO", 2, 71, 2.05, 5.7),
    _theme("QA_COMMODITIES", 3, 66, 0.9, 2.1),
]

WATCHLIST_PAYLOAD: dict[str, Any] = {
    "groups": [
        {
            "name": "QA_EQUITY",
            "assets": [
                {
                    "symbol": "SPY",
                    "type": "etf",
                    "source": "yahoo",
                    "exchange": "US",
                    "name": "S&P 500 ETF",
                }
            ],
        },
        {
            "name": "QA_CRYPTO",
            "assets": [
                {
                    "symbol": "BTC",
                    "type": "crypto_perp",
                    "source": "lighter",
                    "exchange": None,
                    "name": "Bitcoin Perp",
                }
            ],
        },
        {
            "name": "QA_COMMODITIES",
            "assets": [
                {
                    "symbol": "GC=F",
                    "type": "future",
                    "source": "yahoo",
                    "exchange": "CME",
                    "name": "Gold Futures",
                }
            ],
        },
    ]
}

BOARD_PAYLOAD: dict[str, Any] = {
    "groups": [
        {"name": "QA_EQUITY", "assets": [TRADFI_ASSET]},
        {"name": "QA_CRYPTO", "assets": [CRYPTO_ASSET]},
        {"name": "QA_COMMODITIES", "assets": [COMMODITY_ASSET]},
    ],
    "crypto_tape": [
        {
            "symbol": TAPE_SYMBOL,
            "last": 48.2,
            "change_pct": 3.4,
            "funding_rate": 0.000012,
            "open_interest_usd": 42_000_000,
            "day_volume_usd": 9_100_000,
            "basket": "L1",
        },
        {
            "symbol": "SOL",
            "last": 168.3,
            "change_pct": 1.2,
            "funding_rate": -0.000004,
            "open_interest_usd": 55_000_000,
            "day_volume_usd": 14_300_000,
            "basket": "L1",
        },
    ],
    "overview": {
        "as_of": _iso(),
        "regime": {
            "label": "RISK-ON / BROAD",
            "tone": "positive",
            "vix": {"level": 14.2, "state": "Calm", "change_pct": -2.1, "tone": "positive"},
            "dominant": THEMES[0],
            "emerging": THEMES[1],
            "fading": THEMES[2],
        },
        "universe": {
            "total": 3,
            "quoted": 3,
            "history_count": 3,
            "above_20dma_pct": 66.7,
            "above_50dma_pct": 66.7,
            "above_200dma_pct": 100.0,
            "highs_20d": 1,
            "lows_20d": 0,
            "highs_52w": 1,
            "lows_52w": 0,
            "up_3pct": 1,
            "down_3pct": 0,
            "advancers": 3,
            "decliners": 0,
        },
        "benchmarks": [
            {
                "symbol": "SPY",
                "name": "S&P 500 ETF",
                "type": "etf",
                "change_1d": 1.18,
                "change_5d": 3.4,
                "distance_50dma": 4.2,
                "atr_extension": 1.1,
            },
            {
                "symbol": "GC=F",
                "name": "Gold Futures",
                "type": "future",
                "change_1d": 0.9,
                "change_5d": 2.1,
                "distance_50dma": 2.6,
                "atr_extension": 0.7,
            },
        ],
        "themes": THEMES,
        "rotation": {"climbers": [THEMES[0], THEMES[1]], "fallers": [THEMES[2]]},
        "crypto_breadth": {
            "total": 2,
            "median_change": 2.3,
            "advance_pct": 100.0,
            "up_3pct": 1,
            "down_3pct": 0,
            "up_10pct": 0,
            "down_10pct": 0,
            "volume_usd": 23_400_000,
            "positive_funding_pct": 50.0,
        },
    },
    "macro": [
        {"label": "VIX", "value": 14.2, "change_pct": -2.1, "invert_tone": True},
        {"label": "DXY", "value": 98.4, "change_pct": 0.2},
    ],
}

CRYPTO_ETF_FLOWS: dict[str, Any] = {
    "status": "ok",
    "source": "farside",
    "updated_at": _iso(),
    "assets": [
        {
            "asset": "BTC",
            "name": "Bitcoin ETFs",
            "latest_date": "2026-07-08",
            "latest_flow_usd": 125_000_000,
            "five_day_flow_usd": 420_000_000,
            "ten_day_flow_usd": 710_000_000,
            "leaders": [{"ticker": "IBIT", "flow_usd": 90_000_000}],
            "laggards": [{"ticker": "GBTC", "flow_usd": -12_000_000}],
        }
    ],
}

SNAPSHOTS_PAYLOAD: dict[str, Any] = {
    "snapshots": [
        {
            "date": "2026-07-07",
            "universe": {"above_50dma_pct": 55.0},
            "themes": [{"name": "QA_EQUITY", "score": 76}],
        },
        {
            "date": "2026-07-08",
            "universe": {"above_50dma_pct": 61.0},
            "themes": [{"name": "QA_EQUITY", "score": 79}],
        },
    ]
}

NEWS_PAYLOAD: dict[str, Any] = {"status": "ok", "channels": [], "items": []}
