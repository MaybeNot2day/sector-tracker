# Cross-Asset Board

A private Bloomberg-style market board for manual sector and narrative baskets.

The app runs a FastAPI backend with a static dashboard frontend. The Daily Board computes
regime, breadth, benchmark, theme-strength, five-day rotation metrics, and BTC/ETH/SOL spot
ETF flow reads from live quotes and cached daily history. A macro tape (VIX, DXY, US 10Y)
rides above both views, VIX feeds a volatility read in the regime panel, and the Markets view
splits into TradFi and Crypto categories. TradFi keeps the clickable watchlist grid with an
RVOL (volume vs 20-day average) column and chart workflow; Crypto shows the curated perp
watchlist plus an auto-synced tape of every crypto perp listed on Lighter (~110 markets),
grouped into Lighter's own baskets (L1, DeFi, AI, L2, Memes, Other via its tokenlist
categories) and sortable by 24h volume, funding, and OI — new listings appear without
config changes, and every tape row charts on click. A Crypto Breadth panel on the Daily
Board reads advance/decline, big movers, and funding share across the full tape while the
curated regime/breadth universe stays unpolluted.

Market data blends two worlds. Lighter DEX drives crypto perps end to end (quotes, candles,
funding, OI) and overlays live 24/7 prices onto the ~34 equities/ETFs it lists as synthetic
perps — day change is measured against the last official session close, so weekend and
after-hours moves show up without breaking session semantics. Intraday chart candles come
from Lighter wherever a market exists; daily bars, volume, profiles, and everything
analytics-related (DMAs, breadth, RVOL, 52W) stay on official Yahoo session data. Assets
not listed on Lighter run fully on Yahoo.

The daily board persists a condensed snapshot per UTC day (regime, breadth, theme scores)
to SQLite; the UI uses it for the 50DMA breadth trend sparkline and day-over-day theme
score deltas, and `/api/snapshots?days=30` serves the raw history.

Watchlists live in YAML and can also be edited in the app. Quotes and OHLC bars are cached in
SQLite, and market data providers are isolated behind a common interface so Yahoo, Lighter,
Stooq, Finnhub, and Farside can be swapped or extended.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

## Configuration

Use the settings button in the app or edit `config/watchlists.yaml` to change groups and assets.
The board supports:

- `equity`
- `etf`
- `crypto_perp`

Environment variables:

```bash
FINNHUB_API_KEY=
DATABASE_PATH=./data/market_board.sqlite3
WATCHLIST_PATH=./config/watchlists.yaml
QUOTE_POLL_SECONDS=10
HISTORY_REFRESH_SECONDS=3600
CRYPTO_ETF_FLOW_CACHE_SECONDS=900
```

Crypto ETF flow data uses public Farside tables via a text-rendered fetch route and is cached by
`CRYPTO_ETF_FLOW_CACHE_SECONDS`.

## Smoke Tests

```bash
pytest -v
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/groups
curl http://127.0.0.1:8000/api/quotes
curl http://127.0.0.1:8000/api/snapshots
```

## Deployment

### VPS (recommended)

A single long-lived process is what this architecture wants: warm caches (no funding
flicker), background quote/history loops, live WebSocket streaming, accruing daily
snapshots, durable watchlist edits, and a dedicated rate-limit budget for Lighter/Yahoo.

On a fresh Ubuntu 22.04/24.04 (or Debian 12) server, run one command:

```bash
curl -fsSL https://raw.githubusercontent.com/MaybeNot2day/sector-tracker/main/deploy/setup-vps.sh | sudo bash
```

It installs the app under `/opt/sector-tracker` with a dedicated system user, starts it
via systemd on port 8787, and enables auto-deploy: the server polls `origin/main` every
2 minutes and restarts itself when new commits land — pushing to GitHub is the whole
deploy workflow. The script is idempotent; re-run it to repair an install.

```bash
# after setup
open http://YOUR_SERVER_IP:8787
journalctl -u sector-tracker -f          # logs
systemctl restart sector-tracker         # manual restart
```

The board listens on all interfaces without authentication. For a private setup, either
install [Tailscale](https://tailscale.com) on the VPS and your devices (then firewall
port 8787 to the tailnet), or front it with Caddy/basic-auth.

### Vercel

This repo includes `api/index.py`, `requirements.txt`, and `vercel.json` for Vercel.
Vercel runs the FastAPI app as serverless functions, so `vercel.json` uses `/tmp` for
runtime SQLite/watchlist files, seeds SQLite from `config/market_board_seed.sqlite3`,
and disables background polling tasks. The browser polls `/api/quotes` directly in
production instead of opening the local WebSocket. Watchlist edits and daily snapshots
are runtime-only there; prefer the VPS for the full feature set.

```bash
vercel --prod
```
