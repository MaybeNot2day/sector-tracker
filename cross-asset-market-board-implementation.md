# Cross-Asset Market Board Implementation Plan

> **For Hermes:** Use `subagent-driven-development` skill to implement this plan task-by-task.

**Goal:** Build a free, private Bloomberg-style cross-asset dashboard with manual sector/narrative buckets, live-ish prices, red/green move cells, and click-to-open charts.

**Architecture:** A lean Python backend normalizes multiple market data providers behind one `QuoteProvider` interface. The frontend is a static dashboard served by the backend: grouped ticker board, WebSocket quote updates, and a chart modal using cached OHLC bars. Data is stored in SQLite so the UI keeps working when Yahoo/Finnhub/Stooq hiccup.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, `httpx`, `yfinance`, optional Finnhub API key, Hyperliquid public WebSocket, vanilla TypeScript or Vite, TradingView `lightweight-charts`.

**Data caveat:** Yahoo Finance access through `yfinance` is unofficial and intended for personal/research use. Use it for a private dashboard, not a public redistributed data product. Treat Yahoo as a replaceable adapter, not sacred infrastructure.

---

## 1. Product Scope

Build a dashboard visually similar to a Bloomberg market board:

```text
MAG7                 LAST       CHANGE
AAPL                294.51     -1.58%
MSFT                382.24     -2.94%
NVDA                206.09     -0.64%

SEMIS - COMPUTE      LAST       CHANGE
AMD                 522.78     +3.05%
AVGO                396.91     +5.36%
ARM                 438.70    +10.69%

CRYPTO               LAST       CHANGE
BTC                104250.0    +1.20%
ETH                  3550.2    -0.80%
SOL                   165.3    +3.40%
```

Each row is clickable. Click opens a modal chart with interval buttons.

---

## 2. Requirements

### Functional

- Manual watchlist groups defined in YAML.
- Support these asset types in V0:
  - `equity`
  - `etf`
  - `crypto_perp`
- Show:
  - symbol
  - optional display name
  - last price
  - absolute change
  - percent change
  - timestamp / stale marker
  - data source
- Color move cells green/red based on percent change.
- Click any asset to open a chart.
- Cache quotes and historical bars in SQLite.
- If a provider fails, return stale cached values with `is_stale=true`.

### Non-functional

- Private/personal use only.
- Free or free-tier data sources.
- No paid Koyfin/Bloomberg dependency.
- No auth for V0; bind to localhost or private VPS behind SSH tunnel/Tailscale.
- Keep provider adapters isolated so Yahoo can be swapped out.

---

## 3. Repository Layout

```text
cross-asset-board/
  README.md
  pyproject.toml
  .env.example
  config/
    watchlists.yaml
    settings.example.yaml
  app/
    __init__.py
    main.py
    models.py
    config.py
    db.py
    scheduler.py
    providers/
      __init__.py
      base.py
      yahoo.py
      stooq.py
      finnhub.py
      hyperliquid.py
    services/
      __init__.py
      quotes.py
      history.py
    static/
      index.html
      styles.css
      app.js
  tests/
    test_config.py
    test_models.py
    test_yahoo_provider.py
    test_quote_service.py
    test_history_service.py
  scripts/
    run.sh
  deploy/
    cross-asset-board.service
```

---

## 4. Data Models

Create `app/models.py`:

```python
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
    ) -> "Quote":
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
```

---

## 5. Example Watchlist Config

Create `config/watchlists.yaml`:

```yaml
groups:
  - name: MAG7
    assets:
      - { symbol: AAPL, type: equity, source: yahoo, exchange: NASDAQ, name: Apple }
      - { symbol: MSFT, type: equity, source: yahoo, exchange: NASDAQ, name: Microsoft }
      - { symbol: NVDA, type: equity, source: yahoo, exchange: NASDAQ, name: Nvidia }
      - { symbol: AMZN, type: equity, source: yahoo, exchange: NASDAQ, name: Amazon }
      - { symbol: GOOGL, type: equity, source: yahoo, exchange: NASDAQ, name: Alphabet }
      - { symbol: META, type: equity, source: yahoo, exchange: NASDAQ, name: Meta }
      - { symbol: TSLA, type: equity, source: yahoo, exchange: NASDAQ, name: Tesla }

  - name: SEMIS_COMPUTE
    assets:
      - { symbol: AMD, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: AVGO, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: MRVL, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: ARM, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: TSM, type: equity, source: yahoo, exchange: NYSE }

  - name: SEMIS_STORAGE
    assets:
      - { symbol: MU, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: WDC, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: STX, type: equity, source: yahoo, exchange: NASDAQ }

  - name: CRYPTO
    assets:
      - { symbol: BTC, type: crypto_perp, source: hyperliquid, name: Bitcoin }
      - { symbol: ETH, type: crypto_perp, source: hyperliquid, name: Ethereum }
      - { symbol: SOL, type: crypto_perp, source: hyperliquid, name: Solana }
      - { symbol: HYPE, type: crypto_perp, source: hyperliquid, name: Hyperliquid }

  - name: CRYPTO_EQUITIES
    assets:
      - { symbol: COIN, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: MSTR, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: MARA, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: RIOT, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: CLSK, type: equity, source: yahoo, exchange: NASDAQ }

  - name: AI_INFRA
    assets:
      - { symbol: IREN, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: CIFR, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: WULF, type: equity, source: yahoo, exchange: NASDAQ }
      - { symbol: NBIS, type: equity, source: yahoo, exchange: NASDAQ }

  - name: ETF_MACRO
    assets:
      - { symbol: SPY, type: etf, source: yahoo, exchange: NYSEARCA, name: S&P 500 ETF }
      - { symbol: QQQ, type: etf, source: yahoo, exchange: NASDAQ, name: Nasdaq 100 ETF }
      - { symbol: SMH, type: etf, source: yahoo, exchange: NASDAQ, name: Semiconductors ETF }
      - { symbol: TLT, type: etf, source: yahoo, exchange: NASDAQ, name: 20Y Treasury ETF }
      - { symbol: GLD, type: etf, source: yahoo, exchange: NYSEARCA, name: Gold ETF }
      - { symbol: USO, type: etf, source: yahoo, exchange: NYSEARCA, name: Oil ETF }
```

---

## 6. Provider Strategy

```text
crypto_perp:
  primary: Hyperliquid WebSocket or REST
  fallback: cached stale value

equity:
  primary: Yahoo polling
  fallback: Stooq delayed/EOD
  optional: Finnhub if API key exists

etf:
  primary: Yahoo polling
  fallback: Stooq delayed/EOD
  optional: Finnhub if API key supports ticker
```

Recommended cadence:

```text
Hyperliquid crypto: streaming websocket or 5-15 sec polling
Yahoo quotes: every 30-60 sec during US session, every 5 min otherwise
Stooq fallback: every 5-15 min
History bars: cache 5-30 min depending on interval
```

---

## 7. Implementation Tasks

### Task 1: Initialize project skeleton

**Objective:** Create project metadata and directories.

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `.env.example`

`pyproject.toml`:

```toml
[project]
name = "cross-asset-board"
version = "0.1.0"
description = "Private cross-asset market dashboard"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.111.0",
    "uvicorn[standard]>=0.30.0",
    "httpx>=0.27.0",
    "pydantic>=2.7.0",
    "pydantic-settings>=2.3.0",
    "pyyaml>=6.0.1",
    "yfinance>=0.2.40",
    "websockets>=12.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.2.0",
    "pytest-asyncio>=0.23.0",
    "ruff>=0.5.0",
    "mypy>=1.10.0",
]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.mypy]
python_version = "3.11"
strict = true
```

`.env.example`:

```bash
FINNHUB_API_KEY=
DATABASE_PATH=./data/market_board.sqlite3
WATCHLIST_PATH=./config/watchlists.yaml
QUOTE_POLL_SECONDS=45
```

Verify:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -c "import fastapi, yfinance, httpx; print('ok')"
```

Expected: `ok`.

---

### Task 2: Add model tests and models

**Objective:** Define normalized quote/bar/group models.

**Files:**
- Create: `app/models.py`
- Create: `tests/test_models.py`

Test:

```python
from datetime import datetime, timezone

from app.models import Quote


def test_quote_computes_change_fields_when_prev_close_exists() -> None:
    q = Quote.from_last_and_prev_close(
        symbol="AAPL",
        asset_type="equity",
        provider="yahoo",
        last=110.0,
        previous_close=100.0,
        timestamp=datetime.now(timezone.utc),
    )

    assert q.change_abs == 10.0
    assert q.change_pct == 10.0
```

Run:

```bash
pytest tests/test_models.py -v
```

Expected: pass.

---

### Task 3: Add YAML config loader

**Objective:** Parse `config/watchlists.yaml` into typed config objects.

**Files:**
- Create: `app/config.py`
- Create: `tests/test_config.py`

`app/config.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.models import AssetConfig, GroupConfig


def load_watchlists(path: Path) -> list[GroupConfig]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or "groups" not in raw:
        raise ValueError("watchlist YAML must contain top-level 'groups'")

    groups: list[GroupConfig] = []
    for group_raw in raw["groups"]:
        assets = [_parse_asset(asset_raw) for asset_raw in group_raw.get("assets", [])]
        groups.append(GroupConfig(name=str(group_raw["name"]), assets=assets))
    return groups


def _parse_asset(raw: dict[str, Any]) -> AssetConfig:
    return AssetConfig(
        symbol=str(raw["symbol"]).upper(),
        type=raw["type"],
        source=raw["source"],
        exchange=raw.get("exchange"),
        name=raw.get("name"),
    )
```

Verify:

```bash
pytest tests/test_config.py -v
```

---

### Task 4: Create provider interface

**Objective:** Add a common interface for quote/history providers.

**Files:**
- Create: `app/providers/base.py`

```python
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import AssetConfig, Bar, Quote


class QuoteProvider(ABC):
    name: str

    @abstractmethod
    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        raise NotImplementedError

    @abstractmethod
    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        raise NotImplementedError
```

Verify:

```bash
python -m compileall app
```

---

### Task 5: Implement Yahoo provider

**Objective:** Fetch equities/ETFs from Yahoo via `yfinance`, normalized to `Quote`.

**Files:**
- Create: `app/providers/yahoo.py`

```python
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import yfinance as yf

from app.models import AssetConfig, Bar, Quote
from app.providers.base import QuoteProvider


class YahooProvider(QuoteProvider):
    name = "yahoo"

    async def get_quotes(self, assets: list[AssetConfig]) -> list[Quote]:
        return await asyncio.to_thread(self._get_quotes_sync, assets)

    def _get_quotes_sync(self, assets: list[AssetConfig]) -> list[Quote]:
        quotes: list[Quote] = []
        for asset in assets:
            try:
                ticker = yf.Ticker(asset.symbol)
                info = ticker.fast_info
                last = float(info["last_price"])
                prev = float(info["previous_close"]) if info.get("previous_close") else None
                quotes.append(
                    Quote.from_last_and_prev_close(
                        symbol=asset.symbol,
                        asset_type=asset.type,
                        provider="yahoo",
                        last=last,
                        previous_close=prev,
                        timestamp=datetime.now(timezone.utc),
                    )
                )
            except Exception:
                continue
        return quotes

    async def get_history(self, asset: AssetConfig, *, interval: str, range_: str) -> list[Bar]:
        return await asyncio.to_thread(self._get_history_sync, asset, interval, range_)

    def _get_history_sync(self, asset: AssetConfig, interval: str, range_: str) -> list[Bar]:
        ticker = yf.Ticker(asset.symbol)
        df = ticker.history(period=range_, interval=interval, auto_adjust=False)
        bars: list[Bar] = []
        for idx, row in df.iterrows():
            ts = idx.to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            bars.append(
                Bar(
                    symbol=asset.symbol,
                    provider="yahoo",
                    interval=interval,
                    timestamp=ts,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row["Volume"]) if "Volume" in row else None,
                )
            )
        return bars
```

Manual verification:

```bash
python - <<'PY'
import asyncio
from app.models import AssetConfig
from app.providers.yahoo import YahooProvider

async def main():
    provider = YahooProvider()
    quotes = await provider.get_quotes([
        AssetConfig(symbol='SPY', type='etf', source='yahoo'),
        AssetConfig(symbol='NVDA', type='equity', source='yahoo'),
    ])
    for q in quotes:
        print(q)

asyncio.run(main())
PY
```

Expected: prints quote objects for SPY/NVDA, or fails gracefully with empty list if Yahoo is down/rate-limited.

---

### Task 6: Implement SQLite cache

**Objective:** Persist latest quotes and historical bars.

**Files:**
- Create: `app/db.py`

Schema:

```sql
CREATE TABLE IF NOT EXISTS latest_quotes (
    symbol TEXT PRIMARY KEY,
    asset_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    last REAL NOT NULL,
    previous_close REAL,
    change_abs REAL,
    change_pct REAL,
    timestamp TEXT NOT NULL,
    is_stale INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    provider TEXT NOT NULL,
    interval TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    PRIMARY KEY (symbol, provider, interval, timestamp)
);
```

Implementation requirements:

- Use stdlib `sqlite3`.
- Add `init_db(path)`.
- Add `save_quotes(quotes)`.
- Add `load_latest_quote(symbol)`.
- Add `save_bars(bars)`.
- Add `load_bars(symbol, interval, provider)`.

---

### Task 7: Add quote service with stale fallback

**Objective:** Aggregate providers, update cache, and return fresh or stale quotes.

**Files:**
- Create: `app/services/quotes.py`

Behavior:

- Flatten configured groups.
- Group assets by `source`.
- Call provider for each source.
- Save successful quotes to DB.
- For missing symbols, return cached quote with `is_stale=true`.
- If no cache exists, return an error quote with `error="no_quote_available"`.

Pseudo-code:

```python
async def get_board_quotes(groups: list[GroupConfig]) -> dict[str, list[Quote]]:
    assets = flatten(groups)
    by_provider = group_by_source(assets)
    fresh_by_symbol = {}

    for source, source_assets in by_provider.items():
        provider = providers[source]
        quotes = await provider.get_quotes(source_assets)
        for quote in quotes:
            fresh_by_symbol[quote.symbol] = quote

    db.save_quotes(list(fresh_by_symbol.values()))
    return reconstruct_group_order(groups, fresh_by_symbol, stale_cache_fallback=True)
```

---

### Task 8: Implement Hyperliquid provider

**Objective:** Fetch crypto perp prices from Hyperliquid.

**Files:**
- Create: `app/providers/hyperliquid.py`

V0 approach:

- Start with REST polling for simplicity.
- Upgrade to WebSocket after dashboard works.
- Track configured symbols: `BTC`, `ETH`, `SOL`, `HYPE`.
- Maintain latest prices in DB.
- Compute 24h percent change if source response gives prior reference; otherwise use previous cached daily snapshot later.

Verification:

```bash
python - <<'PY'
# Print BTC/ETH latest prices using the provider.
PY
```

Expected: current-looking numbers, no crash on provider error.

---

### Task 9: Add FastAPI routes

**Objective:** Serve dashboard, config, quotes, history, and WebSocket updates.

**Files:**
- Create: `app/main.py`

Routes:

```text
GET /                         -> dashboard HTML
GET /api/health               -> health check
GET /api/groups               -> watchlist config without secrets
GET /api/quotes               -> grouped latest quotes
GET /api/history/{symbol}     -> OHLC bars, query: interval, range
WS  /ws/quotes                -> periodic pushed quote updates
```

Minimal health route:

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Cross-Asset Board")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

Verify:

```bash
uvicorn app.main:app --reload
curl http://127.0.0.1:8000/api/health
```

Expected:

```json
{"status":"ok"}
```

---

### Task 10: Build dashboard UI

**Objective:** Render the grouped quote board.

**Files:**
- Create: `app/static/index.html`
- Create: `app/static/styles.css`
- Create: `app/static/app.js`

UI requirements:

- Dark background.
- Responsive grid of group panels.
- Group header in uppercase.
- Columns: symbol, last, change abs, change pct.
- Red/green background only on change cells.
- Stale quotes show dim text or `STALE` marker.
- Source marker optional: `YH`, `HL`, `STQ`, `FH`.

CSS base:

```css
:root {
  --bg: #050505;
  --panel: #111;
  --row-alt: #1b1b1b;
  --text: #e8e8e8;
  --muted: #888;
  --green: #064f18;
  --red: #7a1010;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

.board {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 14px;
  padding: 16px;
}

.group-title {
  font-weight: 800;
  background: #000;
  padding: 6px 8px;
  letter-spacing: 0.08em;
}

.asset-row {
  display: grid;
  grid-template-columns: 1fr 90px 90px 90px;
  cursor: pointer;
}

.asset-row:hover {
  outline: 1px solid #555;
}

.change-positive { background: var(--green); }
.change-negative { background: var(--red); }
.stale { color: var(--muted); }
```

---

### Task 11: Add chart modal

**Objective:** Clicking a row opens an OHLC chart.

**Files:**
- Modify: `app/static/index.html`
- Modify: `app/static/app.js`

Use TradingView Lightweight Charts from CDN for V0:

```html
<script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
```

Behavior:

- On row click:
  - open modal
  - call `/api/history/{symbol}?interval=1d&range=1y`
  - render candlestick or line chart
- Add buttons: `1M`, `3M`, `YTD`, `1Y`, `5Y`.
- If history fails, show clean error in modal.

---

### Task 12: Add background quote polling

**Objective:** Refresh prices and push updates to UI.

**Files:**
- Create: `app/scheduler.py`
- Modify: `app/main.py`

Behavior:

- On FastAPI startup, start background task.
- Every `QUOTE_POLL_SECONDS`, refresh Yahoo/Stooq/Finnhub assets.
- Hyperliquid provider keeps stream or poll loop.
- WebSocket clients receive grouped quotes every refresh.

Verification:

- Start server.
- Open browser.
- Confirm values update without page refresh.
- Force provider error.
- Confirm stale cached values remain visible.

---

### Task 13: Add Stooq fallback provider

**Objective:** Provide delayed/EOD fallback for equities/ETFs.

**Files:**
- Create: `app/providers/stooq.py`

Implementation notes:

- US tickers often use `.us`: `aapl.us`, `spy.us`.
- Current quote endpoint pattern: `https://stooq.com/q/l/?s=aapl.us&f=sd2t2ohlcv&h&e=csv`.
- Historical endpoint pattern: `https://stooq.com/q/d/l/?s=aapl.us&i=d`.
- Normalize Stooq symbols separately. Internal symbols should remain `AAPL`, `SPY`, etc.

Behavior:

- `AAPL` maps to `aapl.us` for Stooq.
- If Yahoo returns nothing, service checks Stooq cached/fetched quote/history.

---

### Task 14: Add optional Finnhub provider

**Objective:** Use cleaner free-tier stock quotes when API key exists.

**Files:**
- Create: `app/providers/finnhub.py`

Behavior:

- If `FINNHUB_API_KEY` is empty, provider is disabled.
- Respect free-tier limits.
- Use it for quote verification/fallback, not mandatory V0.

---

### Task 15: Add VPS deployment

**Objective:** Run on VPS as a private local service.

**Files:**
- Create: `scripts/run.sh`
- Create: `deploy/cross-asset-board.service`

`scripts/run.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
exec uvicorn app.main:app --host 127.0.0.1 --port 8787
```

`deploy/cross-asset-board.service`:

```ini
[Unit]
Description=Cross Asset Market Board
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ds/cross-asset-board
ExecStart=/home/ds/cross-asset-board/scripts/run.sh
Restart=always
RestartSec=5
EnvironmentFile=/home/ds/cross-asset-board/.env

[Install]
WantedBy=multi-user.target
```

Access through SSH tunnel:

```bash
ssh -L 8787:127.0.0.1:8787 ds@your-vps
```

Open:

```text
http://127.0.0.1:8787
```

---

## 8. Testing Checklist

Unit tests:

```bash
pytest -v
```

Must cover:

- YAML config parsing.
- Quote percent-change math.
- Provider error handling.
- DB quote save/load.
- Fallback-to-stale-cache behavior.

Manual smoke tests:

```bash
uvicorn app.main:app --reload
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/groups
curl http://127.0.0.1:8000/api/quotes
```

Browser checks:

- Dashboard loads.
- Groups render in configured order.
- Red/green cells match sign of change.
- Clicking `NVDA` opens chart.
- Clicking `BTC` opens chart or clean unsupported-history message.
- If Yahoo fails, cached/stale quotes show instead of blank screen.

---

## 9. MVP Acceptance Criteria

MVP is done when:

- `config/watchlists.yaml` controls displayed groups/assets.
- Equities/ETFs show prices from Yahoo or fallback cache.
- Hyperliquid crypto assets show live or frequently refreshed prices.
- UI displays grouped market board with colored changes.
- Asset click opens chart modal for Yahoo-backed assets.
- Server restart preserves latest cached quotes.
- Provider failure does not break the dashboard.

---

## 10. Later Features

Only after V0 works. YAGNI, sadly, still undefeated.

### Market analytics

- Relative move vs benchmark:
  - equities vs `SPY` or `QQQ`
  - crypto equities vs `BTC`
- 20-day ATR-normalized move.
- Gap from previous close.
- Premarket/after-hours price if Yahoo exposes it reliably.
- Volume vs 20-day average.

### Workflow

- Sort within group by `% change`.
- Toggle compact / expanded view.
- Notes per ticker.
- Telegram alerts for threshold moves.
- Screenshot/export board as PNG.

### Data quality

- Provider status panel.
- Last successful provider fetch timestamp.
- Symbol-level source override.
- Automatic fallback from Yahoo → Finnhub → Stooq.

### Research links

- TradingView chart link.
- Yahoo Finance page.
- SEC filings for equities.
- Hyperliquid market page for perps.
- Earnings date.
- Market cap.
- Short interest if sourced cleanly.

---

## 11. Implementation Order Summary

```text
1. Project skeleton
2. Models
3. YAML config loader
4. Provider interface
5. Yahoo provider
6. SQLite cache
7. Quote service with stale fallback
8. FastAPI routes
9. Static dashboard UI
10. Chart modal
11. Background polling + WebSocket updates
12. Hyperliquid provider
13. Stooq fallback
14. Optional Finnhub provider
15. VPS deployment
```

Ship V0 after step 12 if it works. Stooq/Finnhub can come after; do not let fallback architecture block the first usable board.

---

## 12. Brutal Notes

- Do not make this a portfolio tracker. Different product.
- Do not add accounts/auth until it leaves localhost/private VPS.
- Do not build a screener first. Manual baskets are the edge.
- Do not depend on one unofficial data source.
- Do not over-polish before the first working board. Black background, red/green cells, clickable charts. Done.
