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
from typing import TYPE_CHECKING, Any, cast
from urllib.error import URLError
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page


E2E_ENABLED = bool(os.environ.get("RUN_PLAYWRIGHT") or os.environ.get("BOARD_E2E_BASE_URL"))

if E2E_ENABLED:
    try:
        from playwright.sync_api import (
            Error as PlaywrightError,
        )
        from playwright.sync_api import expect, sync_playwright
    except ModuleNotFoundError:
        pytest.skip(
            "Python Playwright smoke tests require the 'playwright' package",
            allow_module_level=True,
        )

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

    # Key Dates enrichment: one released print (figures + HIGH chip + neutral
    # delta), one pending (ACT em dash), one non-macro event with release null.
    key_date_rows = page.locator("#daily-board .key-date-row")
    expect(key_date_rows).to_have_count(3)
    cpi_row = key_date_rows.nth(0)
    expect(cpi_row.locator(".key-date-figures")).to_have_text(
        "EST -0.1% · PREV 1% · ACT -0.2% -0.10"
    )
    expect(cpi_row.locator(".key-tag-high")).to_have_text("HIGH")
    expect(cpi_row.locator(".key-date-main > span:not(.key-date-figures)")).to_have_text(
        "today · 14:30 CET · US · Consumer Price Index MoM"
    )
    expect(cpi_row).to_have_attribute(
        "title", re.compile(r"Consumer Price Index.*Data source: U\.S\. Bureau")
    )
    expect(cpi_row).to_have_attribute(
        "href", "https://www.tradingview.com/symbols/ECONOMICS-USCPI/"
    )
    expect(key_date_rows.nth(1).locator(".key-date-figures")).to_have_text(
        "EST 0.3% · PREV 0.4% · ACT —"
    )
    expect(key_date_rows.nth(2).locator(".key-date-figures")).to_have_count(0)
    # Unmatched events and enriched releases without a verified series URL
    # remain plain rows. A generic calendar page is not evidence for the event.
    assert key_date_rows.nth(1).evaluate("row => row.tagName") == "DIV"
    assert key_date_rows.nth(1).get_attribute("href") is None
    assert key_date_rows.nth(2).evaluate("row => row.tagName") == "DIV"
    assert key_date_rows.nth(2).get_attribute("href") is None

    # Fringe Corner: Hermes' ideas blotter — direction chip, marked P&L,
    # target + distance-to-go, a missing price rendering as an em dash, the
    # in-row thesis and stale marker, and the compact closed footer.
    fringe_heading = page.locator("#daily-board .analytics-panel").filter(
        has=page.get_by_role("heading", name="Fringe Corner")
    ).locator(".panel-heading > span")
    expect(fringe_heading).to_have_text("3 open · 2 closed · overall P&L +1.92%")
    fringe_rows = page.locator("#daily-board .fringe-row")
    expect(fringe_rows).to_have_count(3)
    expect(fringe_rows.nth(0).locator(".fringe-chip")).to_have_text("LONG")
    expect(fringe_rows.nth(0).locator(".fringe-pnl")).to_have_text("+4.16%")
    expect(fringe_rows.nth(0).locator(".fringe-ticker")).to_have_text("CIFR")
    expect(fringe_rows.nth(0).locator(".fringe-target")).to_have_text("12.00")
    expect(fringe_rows.nth(0).locator(".fringe-togo")).to_have_text("+36.83%")
    expect(fringe_rows.nth(1).locator(".fringe-chip")).to_have_text("SHORT")
    expect(fringe_rows.nth(1).locator(".fringe-pnl")).to_have_text("—")
    expect(fringe_rows.nth(1).locator(".fringe-entry")).to_have_text("—")
    expect(fringe_rows.nth(0)).to_contain_text("Miner with HPC optionality")
    expect(fringe_rows.nth(2).locator(".fringe-stale")).to_have_text("not refreshed")
    numeric_column_offsets = page.locator(".fringe-table").evaluate(
        """table => {
          const textRight = element => {
            const range = document.createRange();
            range.selectNodeContents(element);
            return range.getBoundingClientRect().right;
          };
          const headers = Array.from(table.querySelectorAll('th.fringe-num'));
          const cells = Array.from(table.querySelectorAll('tbody tr:first-child td.fringe-num'));
          return headers.map((header, index) =>
            Math.abs(textRight(header) - textRight(cells[index]))
          );
        }"""
    )
    assert max(numeric_column_offsets) <= 1
    closed_rows = page.locator("#daily-board .fringe-closed-row")
    expect(closed_rows).to_have_count(2)
    expect(closed_rows.nth(0)).to_contain_text("NVDA")
    expect(closed_rows.nth(0)).to_contain_text("160.20 \u2192 173.10")
    expect(closed_rows.nth(0)).to_contain_text("+8.05%")


def test_closed_news_panel_is_inert_until_opened(page: Page, base_url: str) -> None:
    _goto_board(page, base_url)
    panel = page.locator("#news-panel")
    close = page.locator("#news-close")

    expect(panel).to_have_attribute("aria-hidden", "true")
    expect(panel).to_have_attribute("inert", "")
    page.evaluate(
        """() => {
          if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        }"""
    )
    page.keyboard.press("Tab")
    focused_id = cast(str, page.evaluate("() => document.activeElement?.id || ''"))
    assert focused_id != "news-close"
    close_was_focused = cast(
        bool,
        page.evaluate(
            """() => {
              const close = document.querySelector('#news-close');
              close.focus();
              return document.activeElement === close;
            }"""
        ),
    )
    assert close_was_focused is False

    page.locator("#news-toggle").click()

    expect(panel).to_have_attribute("aria-hidden", "false")
    assert panel.get_attribute("inert") is None
    expect(close).to_be_focused()

    page.keyboard.press("Escape")
    expect(panel).to_have_attribute("aria-hidden", "true")
    expect(panel).to_have_attribute("inert", "")
    expect(page.locator("#news-toggle")).to_be_focused()


def test_theme_toggle_persists_light_mode(page: Page, base_url: str) -> None:
    _goto_board(page, base_url)
    root = page.locator("html")
    toggle = page.locator("#theme-toggle")

    expect(root).to_have_attribute("data-theme", "dark")
    expect(toggle).to_have_attribute("aria-label", "Switch to light theme")
    toggle.click()

    expect(root).to_have_attribute("data-theme", "light")
    expect(toggle).to_have_attribute("aria-pressed", "true")
    expect(toggle).to_have_attribute("aria-label", "Switch to dark theme")
    colors = page.evaluate(
        """() => {
          const panel = document.querySelector('.analytics-panel');
          return {
            scheme: getComputedStyle(document.documentElement).colorScheme,
            body: getComputedStyle(document.body).backgroundColor,
            panel: getComputedStyle(panel).backgroundColor,
            text: getComputedStyle(document.body).color,
            stored: localStorage.getItem('board-theme'),
          };
        }"""
    )
    assert colors == {
        "scheme": "light",
        "body": "rgb(243, 244, 242)",
        "panel": "rgb(255, 255, 255)",
        "text": "rgb(32, 36, 42)",
        "stored": "light",
    }

    page.reload(wait_until="domcontentloaded")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    expect(page.locator("#theme-toggle")).to_have_attribute("aria-label", "Switch to dark theme")

    page.locator("#theme-toggle").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(page.locator("#theme-toggle")).to_have_attribute("aria-pressed", "false")


def test_daily_board_hides_fringe_panel_when_book_is_empty(page: Page, base_url: str) -> None:
    # Routes registered later win in Playwright, so this overrides only the
    # fixture's /api/fringe stub; every other endpoint keeps its data.
    page.route(
        "**/api/fringe",
        lambda route: _fulfill_json(route, {"as_of": _iso(), "open": [], "closed": []}),
    )
    _goto_board(page, base_url)

    # No empty shell: the panel does not exist at all, and the rest of the
    # daily board renders unchanged.
    expect(page.locator("#daily-board .benchmark-card").nth(0)).to_be_visible()
    expect(
        page.locator("#daily-board").get_by_role("heading", name="Fringe Corner")
    ).to_have_count(0)
    expect(page.locator("#daily-board .fringe-row")).to_have_count(0)


def test_markets_tabs_render_rows_and_open_canvas_chart(page: Page, base_url: str) -> None:
    _goto_board(page, base_url)
    page.locator("#markets-tab").click()
    expect(page.locator("#markets-view")).to_be_visible()

    for category in ("tradfi", "crypto", "commodities"):
        button = page.locator(f'.category-tabs button[data-category="{category}"]')
        button.click()
        expect(button).to_have_attribute("aria-pressed", "true")
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


def test_crypto_panels_swap_open_column_for_rolling_24h(page: Page, base_url: str) -> None:
    _goto_board(page, base_url)
    page.locator("#markets-tab").click()
    expect(page.locator("#markets-view")).to_be_visible()

    # TradFi keeps the original layout: pct = quote change, open = change since open.
    page.locator('.category-tabs button[data-category="tradfi"]').click()
    tradfi_panel = page.locator('#board .group-panel[data-group="QA_EQUITY"]')
    expect(tradfi_panel.locator('.group-title button[data-sort-key="open"]')).to_have_text(
        "\u0394Open"
    )
    spy_row = tradfi_panel.locator('.asset-row[data-symbol="SPY"]')
    expect(spy_row.locator('[data-cell="pct"]')).to_have_text("+1.18%")
    expect(spy_row.locator('[data-cell="open"]')).to_have_text("+0.80%")

    # Crypto relabels the open column to the rolling 24h move and swaps the bindings:
    # pct = summary.open_change_pct (UTC day), open = quote change_pct (Lighter 24h).
    page.locator('.category-tabs button[data-category="crypto"]').click()
    crypto_panel = page.locator('#board .group-panel[data-group="QA_CRYPTO"]')
    open_button = crypto_panel.locator('.group-title button[data-sort-key="open"]')
    expect(open_button).to_have_text("24h %")
    expect(open_button).to_have_attribute("title", re.compile(r"[Rr]olling 24h"))
    expect(crypto_panel.locator('.group-title button[data-sort-key="pct"]')).to_have_attribute(
        "title", re.compile(r"UTC midnight")
    )

    btc_row = crypto_panel.locator('.asset-row[data-symbol="BTC"]')
    expect(btc_row.locator('[data-cell="pct"]')).to_have_text("-1.25%")
    expect(btc_row.locator('[data-cell="open"]')).to_have_text("+2.16%")
    eth_row = crypto_panel.locator('.asset-row[data-symbol="ETH"]')
    expect(eth_row.locator('[data-cell="pct"]')).to_have_text("+3.40%")
    expect(eth_row.locator('[data-cell="open"]')).to_have_text("-1.92%")

    tape_pct = page.locator('#crypto-tape .tape-panel .group-title button[data-sort-key="pct"]')
    expect(tape_pct.first).to_have_text("24h %")

    # Sorting the relabeled column follows the displayed rolling-24h values (desc on
    # first click): BTC (+2.16%) must jump ahead of the configured-first ETH (-1.92%).
    rows = crypto_panel.locator(".asset-row")
    expect(rows.nth(0)).to_have_attribute("data-symbol", "ETH")
    open_button.click()
    expect(open_button).to_have_attribute("aria-pressed", "true")
    expect(rows.nth(0)).to_have_attribute("data-symbol", "BTC")
    expect(rows.nth(1)).to_have_attribute("data-symbol", "ETH")


def test_crypto_tape_reconciles_in_place_and_delegates_actions(
    page: Page,
    base_url: str,
) -> None:
    _goto_board(page, base_url)
    page.locator("#markets-tab").click()
    page.locator('.category-tabs button[data-category="crypto"]').click()

    panel = page.locator('#crypto-tape .tape-panel[data-basket="L1"]')
    row = panel.locator(f'.asset-row[data-symbol="{TAPE_SYMBOL}"]')
    expect(panel).to_be_visible()
    expect(row).to_be_visible()
    expect(row.locator(".last-cell")).to_have_text("48.20")

    pager = panel.locator(".tape-pager")
    expect(pager.locator("span")).to_have_text("1\u201315 of 17")
    expect(pager.get_by_role("button", name="Previous page")).to_be_disabled()
    expect(pager.get_by_role("button", name="Next page")).to_be_enabled()

    # Sort clicks are delegated by the persistent tape root and reorder keyed rows.
    volume_sort = panel.locator('.group-title button[data-sort-key="volume"]')
    volume_sort.click()
    expect(volume_sort).to_have_attribute("aria-label", re.compile("ascending"))
    expect(panel.locator(".asset-row").first).to_have_attribute("data-symbol", "QA00")
    volume_sort.click()
    expect(volume_sort).to_have_attribute("aria-label", re.compile("descending"))
    expect(panel.locator(".asset-row").first).to_have_attribute("data-symbol", "SOL")

    row.focus()
    page.evaluate(
        """() => {
          window.__tapePanelBeforeRefresh = document.querySelector(
            '#crypto-tape .tape-panel[data-basket="L1"]'
          );
          window.__tapeRowBeforeRefresh = document.querySelector(
            '#crypto-tape .asset-row[data-symbol="ZEC"]'
          );
        }"""
    )
    page.evaluate("() => fetchQuotes()")
    expect(row.locator(".last-cell")).to_have_text("49.70")
    identity = page.evaluate(
        """() => ({
          panel: window.__tapePanelBeforeRefresh === document.querySelector(
            '#crypto-tape .tape-panel[data-basket="L1"]'
          ),
          row: window.__tapeRowBeforeRefresh === document.querySelector(
            '#crypto-tape .asset-row[data-symbol="ZEC"]'
          ),
          focused: document.activeElement === window.__tapeRowBeforeRefresh,
        })"""
    )
    assert identity == {"panel": True, "row": True, "focused": True}

    # Pager and row actions continue to work through the one delegated listener.
    pager.get_by_role("button", name="Next page").click()
    expect(pager.locator("span")).to_have_text("16\u201317 of 17")
    expect(pager.get_by_role("button", name="Next page")).to_be_disabled()
    pager.get_by_role("button", name="Previous page").click()
    expect(row).to_be_visible()
    row.click()
    expect(page.locator("#chart-modal")).to_have_attribute("aria-hidden", "false")
    expect(page.locator("#chart-title")).to_have_text(TAPE_SYMBOL)


def test_market_search_debounces_and_enter_flushes_before_focus(
    page: Page,
    base_url: str,
) -> None:
    _goto_board(page, base_url)
    page.locator("#markets-tab").click()
    page.locator('.category-tabs button[data-category="tradfi"]').click()
    search = page.locator("#market-search")
    spy_row = page.locator('#board .asset-row[data-symbol="SPY"]')
    expect(spy_row).to_be_visible()

    search.fill("does-not-exist")
    # The input event does not synchronously rebuild the board or URL.
    expect(spy_row).to_be_visible()
    assert "q=does-not-exist" not in page.url
    page.wait_for_timeout(160)
    expect(spy_row).to_have_count(0)
    assert "q=does-not-exist" in page.url

    page.locator("#market-filter-clear").click()
    expect(page.locator('#board .asset-row[data-symbol="SPY"]')).to_be_visible()
    assert "q=" not in page.url

    search.fill("SPY")
    search.press("Enter")
    expect(page.locator('#board .asset-row[data-symbol="SPY"]')).to_be_focused()
    assert "q=SPY" in page.url
    # No stale debounce callback may undo the Enter-flushed state.
    page.wait_for_timeout(160)
    expect(page.locator('#board .asset-row[data-symbol="SPY"]')).to_be_focused()


def test_unchanged_news_keeps_nodes_and_refreshes_age_text(
    page: Page,
    base_url: str,
) -> None:
    _goto_board(page, base_url)
    item = page.locator("#news-list .news-item")
    channel = page.locator("#news-channels .news-channel-chip")
    age = item.locator("time")
    expect(item).to_have_count(1)
    expect(channel).to_have_count(1)
    expect(age).to_have_attribute("data-news-timestamp", NEWS_PAYLOAD["items"][0]["timestamp"])
    page.evaluate(
        """() => {
          window.__newsItemBeforeRefresh = document.querySelector('#news-list .news-item');
          window.__newsChannelBeforeRefresh = document.querySelector(
            '#news-channels .news-channel-chip'
          );
          document.querySelector('#news-list time').textContent = 'stale age';
        }"""
    )

    page.evaluate("() => fetchNews()")
    identity = page.evaluate(
        """() => ({
          item: window.__newsItemBeforeRefresh === document.querySelector('#news-list .news-item'),
          channel: window.__newsChannelBeforeRefresh === document.querySelector(
            '#news-channels .news-channel-chip'
          ),
          age: document.querySelector('#news-list time').textContent,
        })"""
    )
    assert identity["item"] is True
    assert identity["channel"] is True
    assert identity["age"] != "stale age"


def test_news_refresh_keeps_reading_position_when_items_prepend(
    page: Page,
    base_url: str,
) -> None:
    _goto_board(page, base_url)

    def news_payload(extra: int) -> dict[str, Any]:
        posts = [
            {
                "id": f"qa_channel/{200 - index}",
                "channel": "qa_channel",
                "channel_title": "QA Channel",
                "text": f"Post number {200 - index} with enough text to give every row height.",
                "timestamp": _iso(),
                "link": f"https://t.me/qa_channel/{200 - index}",
            }
            for index in range(-extra, 24)
        ]
        return {"status": "ok", "channels": ["qa_channel"], "items": posts, "updated_at": _iso()}

    state = {"extra": 0}
    page.route(
        "**/api/news",
        lambda route: _fulfill_json(route, news_payload(state["extra"])),
    )
    page.locator("#news-toggle").click()
    page.evaluate("() => fetchNews()")
    expect(page.locator("#news-list .news-item")).to_have_count(24)

    # Read something below the fold, then let three new posts land.
    anchored = page.evaluate(
        """() => {
          const list = document.querySelector('#news-list');
          const target = list.querySelectorAll('.news-item')[6];
          list.scrollTop = target.getBoundingClientRect().top
            - list.getBoundingClientRect().top + list.scrollTop;
          const offset = target.getBoundingClientRect().top - list.getBoundingClientRect().top;
          return { id: target.dataset.newsId, offset };
        }"""
    )
    state["extra"] = 3
    page.evaluate("() => fetchNews()")
    expect(page.locator("#news-list .news-item")).to_have_count(27)

    after = page.evaluate(
        f"""() => {{
          const list = document.querySelector('#news-list');
          const item = list.querySelector('[data-news-id="{anchored["id"]}"]');
          return item.getBoundingClientRect().top - list.getBoundingClientRect().top;
        }}"""
    )
    # The item under the reader's eyes must not move when posts prepend.
    assert abs(after - anchored["offset"]) <= 2


def test_daily_board_rebuild_preserves_page_scroll(page: Page, base_url: str) -> None:
    _goto_board(page, base_url)
    expect(page.locator("#daily-view")).to_be_visible()

    result = page.evaluate(
        """() => {
          const scroller = document.scrollingElement;
          scroller.scrollTop = 400;
          const before = scroller.scrollTop;
          const board = document.querySelector('#daily-board');
          const regime = board.querySelector('[data-panel="regime"]');
          const rotation = board.querySelector('[data-panel="rotation"]');
          // Identical data: reconcile must keep every panel node (a full
          // DOM swap here is what used to kill scroll momentum).
          lastDailyRenderKey = "";
          renderDailyBoard(latestData.overview, latestCryptoEtfFlows);
          const keptAll = board.querySelector('[data-panel="regime"]') === regime
            && board.querySelector('[data-panel="rotation"]') === rotation;
          // Changed regime data: only that chunk is replaced.
          lastDailyRenderKey = "";
          const overview = structuredClone(latestData.overview);
          overview.regime = { ...(overview.regime || {}), label: 'SMOKE-REGIME' };
          renderDailyBoard(overview, latestCryptoEtfFlows);
          return {
            before,
            after: scroller.scrollTop,
            keptAll,
            regimeReplaced: board.querySelector('[data-panel="regime"]') !== regime,
            rotationKept: board.querySelector('[data-panel="rotation"]') === rotation,
          };
        }"""
    )
    assert result["keptAll"] is True
    assert result["regimeReplaced"] is True
    assert result["rotationKept"] is True
    assert result["after"] == result["before"]



@pytest.mark.parametrize("selector", [".key-dates-list", ".fringe-scroll"])
def test_daily_board_inner_panels_chain_upward_wheel_to_page(
    page: Page, base_url: str, selector: str
) -> None:
    _goto_board(page, base_url)
    expect(page.locator("#daily-view")).to_be_visible()

    page.evaluate("() => window.scrollTo(0, document.scrollingElement.scrollHeight)")
    target = page.locator(f"#daily-board {selector}")
    target.hover()
    before = page.evaluate("() => window.scrollY")
    page.mouse.wheel(0, -300)
    page.wait_for_timeout(100)

    assert page.evaluate("() => window.scrollY") < before

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


def test_reports_modal_lists_reports_and_renders_escaped_markdown_reader(
    page: Page, base_url: str
) -> None:
    _goto_board(page, base_url)

    page.locator("#reports-open").click()
    expect(page.locator("#reports-modal")).to_have_attribute("aria-hidden", "false")

    cards = page.locator("#reports-list .report-card")
    expect(cards).to_have_count(3)
    expect(page.locator("#reports-list .reports-date")).to_have_count(2)
    expect(page.locator("#reports-list .reports-date").first).to_contain_text("Jul 9, 2026")
    expect(cards.first).to_contain_text("Hermes Daily Flows")
    expect(cards.first).to_contain_text("Position sizing check")
    # Back button belongs to the reader; in list view it stays hidden.
    expect(page.locator("#reports-back")).to_be_hidden()

    cards.first.click()
    expect(page.locator("#reports-list")).to_be_hidden()
    reader = page.locator("#report-reader")
    expect(reader).to_be_visible()
    body = reader.locator(".report-body")
    expect(reader.locator(".report-head h2")).to_be_focused()
    expect(body.locator("h2")).to_have_text("Flow Summary")
    expect(body.locator("table th").first).to_have_text("Asset")
    expect(body.locator("table td").first).to_have_text("ZEC")
    expect(body.locator("strong").first).to_have_text("oversized")
    # YAML frontmatter never reaches the reader.
    expect(body).not_to_contain_text("frontmatter-must-not-render")
    # Raw HTML in a report body must be escaped: visible as text, never a live element.
    expect(body).to_contain_text("<script>alert(1)</script>")
    expect(body.locator("script")).to_have_count(0)
    # Hard-wrapped list items keep their indented continuation inside the same <li>,
    # never spilling into a paragraph after the list.
    wrapped_bullet = "Funding across majors normalized under twelve percent."
    expect(body.locator("li").filter(has_text=wrapped_bullet)).to_have_count(1)
    expect(body.locator("p").filter(has_text="normalized under twelve percent")).to_have_count(0)
    wrapped_ordered = "Rotate stale hedges toward liquid perps."
    expect(body.locator("ol li").filter(has_text=wrapped_ordered)).to_have_count(1)
    expect(body.locator("p").filter(has_text="toward liquid perps")).to_have_count(0)
    # Hard-wrapped paragraph lines are soft breaks: one flowing <p>, no forced <br>.
    flowed = body.locator("p").filter(has_text="two-sided across assets with funding normalizing")
    expect(flowed).to_have_count(1)
    expect(flowed.locator("br")).to_have_count(0)
    # A trailing double space is a Markdown hard break and keeps its <br>.
    expect(body.locator("p").filter(has_text="Hard break stays").locator("br")).to_have_count(1)
    # Indented bullets nest as a sublist inside the parent <li>.
    parent_item = body.locator("ul > li").filter(has_text="Integration tools")
    expect(parent_item.locator("ul > li")).to_have_count(2)
    expect(parent_item.locator("ul > li").first).to_have_text("Valetudo firmware")
    # ~~text~~ renders as <del>, ![[embeds]]/[[Target|Alias]] collapse to plain text.
    expect(body.locator("del")).to_have_text("stale claim")
    expect(body).to_contain_text("chart alias and Chart Note")
    expect(body).not_to_contain_text("[[")
    # Bare source URLs autolink; the wrapping paren stays outside the anchor.
    autolink = body.locator('a[href="https://example.com/oil-prices-jump"]')
    expect(autolink).to_have_text("https://example.com/oil-prices-jump")
    expect(autolink).to_have_attribute("target", "_blank")
    expect(body.locator("p").filter(has_text="Sources: Al Jazeera")).to_contain_text(
        "(https://example.com/oil-prices-jump)"
    )

    page.locator("#reports-back").click()
    expect(cards.first).to_be_focused()
    expect(page.locator("#report-reader")).to_be_hidden()
    expect(page.locator("#reports-back")).to_be_hidden()
    expect(page.locator("#reports-list .report-card")).to_have_count(3)


@pytest.mark.parametrize(
    ("width", "height"),
    [(390, 844), (768, 900), (800, 900), (1440, 1100)],
)
def test_viewport_portals_and_modals_never_expand_the_document(
    page: Page,
    base_url: str,
    width: int,
    height: int,
) -> None:
    page.set_viewport_size({"width": width, "height": height})
    _goto_board(page, base_url)

    help_button = page.locator(".help-tip").first
    help_button.focus()
    tooltip = page.get_by_role("tooltip")
    expect(tooltip).to_be_visible()
    expect(help_button).to_have_attribute("aria-describedby", "help-tooltip")
    tooltip_box = tooltip.bounding_box()
    assert tooltip_box is not None
    assert tooltip_box["x"] >= 0
    assert tooltip_box["x"] + tooltip_box["width"] <= width
    assert tooltip_box["y"] >= 0
    assert tooltip_box["y"] + tooltip_box["height"] <= height
    assert page.evaluate("() => document.documentElement.scrollWidth") == width

    page.keyboard.press("Escape")
    expect(tooltip).to_be_hidden()
    expect(help_button).not_to_have_attribute("aria-describedby", "help-tooltip")

    page.locator("#news-toggle").click()
    expect(page.locator("#news-close")).to_be_focused()
    assert page.evaluate("() => document.documentElement.scrollWidth") == width
    header_box = page.locator(".app-header").bounding_box()
    assert header_box is not None
    assert header_box["x"] >= 0
    assert header_box["x"] + header_box["width"] <= width
    page.keyboard.press("Escape")

    page.locator("#editor-open").click()
    shell_box = page.locator(".editor-shell").bounding_box()
    modal_header_box = page.locator("#editor-modal .modal-header").bounding_box()
    assert shell_box is not None
    assert modal_header_box is not None
    assert abs(modal_header_box["y"] - shell_box["y"]) <= 2
    assert shell_box["x"] >= 0
    assert shell_box["x"] + shell_box["width"] <= width
    assert page.evaluate("() => document.documentElement.scrollWidth") == width
    page.locator("#editor-close").click()


def test_tablet_market_toolbar_and_category_group_fit_viewport(
    page: Page,
    base_url: str,
) -> None:
    page.set_viewport_size({"width": 800, "height": 900})
    _goto_board(page, base_url)
    page.locator("#markets-tab").click()

    category_group = page.get_by_role("group", name="Market category")
    expect(category_group).to_be_visible()
    crypto = category_group.get_by_role("button", name="Crypto")
    crypto.click()
    expect(crypto).to_have_attribute("aria-pressed", "true")
    expect(category_group.get_by_role("button", name="TradFi")).to_have_attribute(
        "aria-pressed", "false"
    )
    expect(page.get_by_role("button", name="Refresh market data")).to_be_visible()

    tools_box = page.locator(".market-tools").bounding_box()
    filter_box = page.locator("#market-filter-status").bounding_box()
    assert tools_box is not None
    assert filter_box is not None
    assert tools_box["x"] >= 0
    assert tools_box["x"] + tools_box["width"] <= 800
    assert filter_box["x"] + filter_box["width"] <= 800
    assert page.evaluate("() => document.documentElement.scrollWidth") == 800


def test_mobile_fringe_cards_prioritize_full_thesis_and_touch_targets(
    page: Page,
    base_url: str,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    _goto_board(page, base_url)

    thesis = page.locator(".fringe-row").first.locator(".fringe-thesis")
    thesis_box = thesis.bounding_box()
    assert thesis_box is not None
    assert thesis_box["width"] >= 220
    assert thesis.evaluate("(node) => getComputedStyle(node).whiteSpace") == "normal"
    expect(thesis).to_have_text("Miner with HPC optionality; base building above 8.")
    expect(page.locator(".fringe-row").first.locator(".fringe-entry")).to_be_hidden()
    expect(page.locator(".fringe-row").first.locator(".fringe-last")).to_be_hidden()

    page.locator("#markets-tab").click()
    category_buttons = page.locator(".category-tabs button")
    for index in range(category_buttons.count()):
        box = category_buttons.nth(index).bounding_box()
        assert box is not None
        assert box["height"] >= 44
    for control_id in (
        "theme-toggle",
        "news-toggle",
        "reports-open",
        "refresh-button",
        "editor-open",
    ):
        box = page.locator(f"#{control_id}").bounding_box()
        assert box is not None
        assert box["width"] >= 44
        assert box["height"] >= 44
    assert page.evaluate("() => document.documentElement.scrollWidth") == 390


def test_benchmark_grid_uses_balanced_explicit_columns(
    page: Page,
    base_url: str,
) -> None:
    _goto_board(page, base_url)
    grid = page.locator(".benchmark-grid")
    expect(grid.locator(".benchmark-card")).to_have_count(2)
    column_tracks = grid.evaluate(
        "(node) => getComputedStyle(node).gridTemplateColumns.split(' ').filter(Boolean)"
    )
    assert len(column_tracks) == 3


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
    return cast(
        int,
        page.locator("#markets-view .asset-row").evaluate_all(
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
        ),
    )


def _stub_board_apis(page: Page) -> None:
    quote_requests = 0

    def handle(route: Any) -> None:
        nonlocal quote_requests
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if path == "/api/quotes":
            quote_requests += 1
            payload = json.loads(json.dumps(BOARD_PAYLOAD))
            if quote_requests >= 2:
                payload["crypto_tape"][0]["last"] = 49.7
            _fulfill_json(route, payload)
        elif path == "/api/crypto-etf-flows":
            _fulfill_json(route, CRYPTO_ETF_FLOWS)
        elif path == "/api/snapshots":
            _fulfill_json(route, SNAPSHOTS_PAYLOAD)
        elif path == "/api/news":
            _fulfill_json(route, NEWS_PAYLOAD)
        elif path == "/api/key-dates":
            _fulfill_json(route, KEY_DATES_PAYLOAD)
        elif path == "/api/fringe":
            _fulfill_json(route, FRINGE_PAYLOAD)
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
        elif path.startswith("/api/reports/"):
            _fulfill_json(route, REPORT_DETAIL_PAYLOAD)
        elif path == "/api/reports":
            _fulfill_json(route, REPORTS_LIST_PAYLOAD)
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


def _summary(start: float, *, open_change_pct: float = 0.8) -> dict[str, Any]:
    return {
        "sparkline": [start + index * 0.35 for index in range(32)],
        "rvol": 1.7,
        "open_change_pct": open_change_pct,
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
    open_change_pct: float = 0.8,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "type": asset_type,
        "source": source,
        "exchange": exchange,
        "name": name,
        "groupLabel": group,
        "quote": _quote(symbol, last, previous_close, provider=source),
        "summary": _summary(previous_close, open_change_pct=open_change_pct),
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
    "QA_CRYPTO",
    "BTC",
    "crypto_perp",
    "lighter",
    "Bitcoin Perp",
    118_500.0,
    116_000.0,
    # Rolling 24h (quote change_pct -> +2.16%) vs UTC-day move (-1.25%): kept far
    # apart so a regression back to the old pct/open bindings fails loudly.
    open_change_pct=-1.25,
)
CRYPTO_ASSET_ETH = _asset(
    "QA_CRYPTO",
    "ETH",
    "crypto_perp",
    "lighter",
    "Ether Perp",
    3_580.0,
    3_650.0,
    # Opposite ordering vs BTC on the two metrics (-1.92% rolling vs +3.40% UTC-day)
    # so sorting the 24h column by the wrong field visibly reorders the panel.
    open_change_pct=3.4,
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
                },
                {
                    "symbol": "ETH",
                    "type": "crypto_perp",
                    "source": "lighter",
                    "exchange": None,
                    "name": "Ether Perp",
                },
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
        {"name": "QA_CRYPTO", "assets": [CRYPTO_ASSET_ETH, CRYPTO_ASSET]},
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
BOARD_PAYLOAD["crypto_tape"].extend(
    [
        {
            "symbol": f"QA{index:02d}",
            "last": 1.0 + index,
            "change_pct": float(index) / 10,
            "funding_rate": 0.000001,
            "open_interest_usd": 100_000 + index,
            "day_volume_usd": 1_000_000 + index,
            "basket": "L1",
        }
        for index in range(15)
    ]
)


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

NEWS_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "updated_at": _iso(),
    "channels": ["qa_news"],
    "items": [
        {
            "id": "qa-news-1",
            "channel": "qa_news",
            "channel_title": "QA News",
            "timestamp": _iso(),
            "link": "https://example.com/qa-news-1",
            "text": "Deterministic market news fixture.",
        }
    ],
}

# as_of pins the relative-day labels; release objects exercise the economic
# calendar enrichment: released with surprise + high importance, pending
# (actual null), and unmatched (release null — the common non-macro case).
KEY_DATES_PAYLOAD: dict[str, Any] = {
    "as_of": "2026-07-09",
    "key_dates": [
        {
            "date": "2026-07-09",
            "time": "14:30 CET",
            "title": "US June CPI (M-o-M)",
            "category": "MACRO",
            "release": {
                "time_utc": "2026-07-09T12:30:00Z",
                "period": "Jun",
                "actual": "-0.2%",
                "forecast": "-0.1%",
                "previous": "1%",
                "surprise": -0.1,
                "importance": 1,
                "comment": "Consumer Price Index measures the change in prices "
                "paid by consumers for a representative basket of goods.",
                "matched_title": "Consumer Price Index MoM",
                "country": "US",
                "source": "U.S. Bureau of Labor Statistics",
                "series_url": "https://www.tradingview.com/symbols/ECONOMICS-USCPI/",
            },
        },
        {
            "date": "2026-07-10",
            "time": "08:30 ET",
            "title": "US Retail Sales",
            "category": "MACRO",
            "release": {
                "time_utc": "2026-07-10T12:30:00Z",
                "period": "Jun",
                "actual": None,
                "forecast": "0.3%",
                "previous": "0.4%",
                "surprise": None,
                "importance": 0,
                "comment": None,
                "matched_title": "Retail Sales MoM",
            },
        },
        {
            "date": "2026-07-14",
            "time": None,
            "title": "TSLA earnings",
            "category": "EARNINGS",
            "release": None,
        },
    ],
}

# Hermes book: an open long marking to market, an open short the providers
# cannot price yet (null price → em dash), a stale idea the newest report
# skipped, and two closed ideas for the footer list.
FRINGE_PAYLOAD: dict[str, Any] = {
    "as_of": _iso(),
    "open": [
        {
            "id": 3,
            "ticker": "CIFR",
            "direction": "long",
            "thesis": "Miner with HPC optionality; base building above 8.",
            "horizon": "2w",
            "target": "$12",
            "target_price": 12.0,
            "to_target_pct": 36.83,
            "opened": "2026-07-08",
            "last_mentioned": "2026-07-09",
            "stale": False,
            "entry_price": 8.42,
            "last": 8.77,
            "unrealized_pct": 4.16,
            "source_slug": "fringe-corner",
        },
        {
            "id": 4,
            "ticker": "PRIV",
            "direction": "short",
            "thesis": "Illiquid name no provider can price yet.",
            "horizon": None,
            "target": None,
            "target_price": None,
            "to_target_pct": None,
            "opened": "2026-07-09",
            "last_mentioned": "2026-07-09",
            "stale": False,
            "entry_price": None,
            "last": None,
            "unrealized_pct": None,
            "source_slug": "fringe-corner",
        },
        {
            "id": 2,
            "ticker": "XLU",
            "direction": "short",
            "thesis": "Defensive bid fading as yields back up.",
            "horizon": "1m",
            "target": "78.50",
            "target_price": 78.5,
            "to_target_pct": 2.97,
            "opened": "2026-07-01",
            "last_mentioned": "2026-07-05",
            "stale": True,
            "entry_price": 82.1,
            "last": 80.9,
            "unrealized_pct": 1.46,
            "source_slug": "fringe-corner",
        },
    ],
    "closed": [
        {
            "id": 1,
            "ticker": "NVDA",
            "direction": "long",
            "thesis": "Blackwell ramp",
            "target": "$170",
            "opened": "2026-06-20",
            "closed": "2026-07-08",
            "entry_price": 160.2,
            "exit_price": 173.1,
            "realized_pct": 8.05,
            "close_reason": "Target hit into earnings",
        },
        {
            "id": 0,
            "ticker": "GME",
            "direction": "short",
            "thesis": "Squeeze exhaustion",
            "target": None,
            "opened": "2026-06-15",
            "closed": "2026-07-02",
            "entry_price": 28.4,
            "exit_price": 30.1,
            "realized_pct": -5.99,
            "close_reason": "Stopped on renewed retail flow",
        },
    ],
}

REPORTS_LIST_PAYLOAD: dict[str, Any] = {
    "reports": [
        {
            "id": 7,
            "slug": "hermes-daily-flows",
            "date": "2026-07-09",
            "title": "Hermes Daily Flows",
            "created_at": "2026-07-09T14:00:00+00:00",
            "preview": "Position sizing check and net flows",
        },
        {
            "id": 6,
            "slug": "levels-watch",
            "date": "2026-07-09",
            "title": "Levels Watch",
            "created_at": "2026-07-09T06:00:00+00:00",
            "preview": "Key levels for the session",
        },
        {
            "id": 5,
            "slug": "hermes-daily-flows",
            "date": "2026-07-08",
            "title": "Hermes Daily Flows",
            "created_at": "2026-07-08T14:00:00+00:00",
            "preview": "Prior session flows",
        },
    ]
}

REPORT_BODY_MARKDOWN = "\n".join(
    [
        "---",
        "tags: [hermes]",
        "secret: frontmatter-must-not-render",
        "---",
        "## Flow Summary",
        "",
        "Position is **oversized** relative to plan.",
        "",
        "Overnight tape was two-sided across",
        "assets with funding normalizing.",
        "",
        "Hard break stays  ",
        "on its own line.",
        "",
        "Sources: Al Jazeera (https://example.com/oil-prices-jump), desk color.",
        "",
        "| Asset | Change |",
        "| --- | --- |",
        "| ZEC | +4.2% |",
        "",
        "- Funding across majors",
        "  normalized under twelve percent.",
        "- Basis steady",
        "- Integration tools",
        "  - Valetudo firmware",
        "  - node client",
        "",
        "1. Rotate stale hedges",
        "   toward liquid perps.",
        "",
        "~~stale claim~~ replaced by ![[chart alias.png|chart alias]] and [[Chart Note]].",
        "",
        "<script>alert(1)</script>",
    ]
)

REPORT_DETAIL_PAYLOAD: dict[str, Any] = {
    "id": 7,
    "slug": "hermes-daily-flows",
    "date": "2026-07-09",
    "title": "Hermes Daily Flows",
    "created_at": "2026-07-09T14:00:00+00:00",
    "body": REPORT_BODY_MARKDOWN,
}
