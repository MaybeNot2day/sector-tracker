# Cross-Asset Board

A private Bloomberg-style market board for manual sector and narrative baskets.

The app runs a FastAPI backend with a static dashboard frontend. The Daily Board computes
regime, breadth, benchmark, theme-strength, five-day rotation metrics, and BTC/ETH/SOL spot
ETF flow reads from live quotes and cached daily history. A macro tape (VIX, DXY, US 10Y)
rides above both views, VIX feeds a volatility read in the regime panel, and the Markets view
keeps the full clickable watchlist grid with an RVOL (volume vs 20-day average) column and
chart workflow. Crypto perp rows carry Hyperliquid funding and open-interest reads.

The daily board persists a condensed snapshot per UTC day (regime, breadth, theme scores)
to SQLite; the UI uses it for the 50DMA breadth trend sparkline and day-over-day theme
score deltas, and `/api/snapshots?days=30` serves the raw history.

Watchlists live in YAML and can also be edited in the app. Quotes and OHLC bars are cached in
SQLite, and market data providers are isolated behind a common interface so Yahoo,
Hyperliquid, Stooq, Finnhub, and Farside can be swapped or extended.

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

### Vercel

This repo includes `api/index.py`, `requirements.txt`, and `vercel.json` for Vercel.
Vercel runs the FastAPI app as serverless functions, so `vercel.json` uses `/tmp` for
runtime SQLite/watchlist files, seeds SQLite from `config/market_board_seed.sqlite3`,
and disables background polling tasks. The browser polls `/api/quotes` directly in
production instead of opening the local WebSocket.

```bash
vercel --prod
```

Watchlist edits on Vercel are runtime-only unless an external persistent store is added; the
same applies to daily-board snapshots, which live in the runtime SQLite file.
For durable always-on background quote/history polling, use the VPS deployment below.

### VPS

For a private VPS, run the server bound to localhost and access it through an SSH tunnel:

```bash
ssh -L 8787:127.0.0.1:8787 ds@your-vps
```

Then open http://127.0.0.1:8787.
