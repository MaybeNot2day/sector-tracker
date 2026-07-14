# Cross-Asset Board

A private Bloomberg-style market board for manual sector and narrative baskets.

The app runs a FastAPI backend with a static dashboard frontend. The Daily Board computes
regime, breadth, benchmark, theme-strength, five-day rotation metrics, and BTC/ETH/SOL spot
ETF flow reads from live quotes and cached daily history. A macro tape (VIX, DXY, US 10Y)
rides above both views, VIX feeds a volatility read in the regime panel, and the Markets view
splits into TradFi, Crypto, and Commodities categories. TradFi keeps the clickable watchlist
grid — Last / Abs / 1D% / ΔOpen (move since today's session open; UTC day for crypto) /
RVOL (volume vs 20-day average) / trend sparkline — with the chart workflow; Crypto shows
the curated perp watchlist plus an auto-synced tape of every crypto perp listed on Lighter
(~110 markets), grouped into Lighter's own baskets (L1, DeFi, AI, L2, Memes, Other via its
tokenlist categories) and sortable by 24h volume, funding, and OI — new listings appear
without config changes, and every tape row charts on click. Commodities tracks Yahoo
continuous front-month futures (metals, energy, ags) with a Globex-aware session chip.
A Crypto Breadth panel on the Daily Board reads advance/decline, big movers, and funding
share across the full tape while the curated regime/breadth universe stays unpolluted.
A toggleable full-height news drawer streams public Telegram channels (scraped from their
t.me previews, no API key): the server polls every 15 seconds and pushes new posts to the
browser over the WebSocket, and each channel gets a per-browser mute chip.

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

## Tests

```bash
python -m pytest
```

Browser smoke tests are opt-in so the default suite stays fast and does not require
Chromium:

```bash
python -m playwright install chromium
RUN_PLAYWRIGHT=1 python -m pytest tests/test_playwright_smoke.py -q
```

To run the same smoke suite against an already-running board instead of the test fixture
server:

```bash
BOARD_E2E_BASE_URL=http://127.0.0.1:8000 python -m pytest tests/test_playwright_smoke.py -q
```

## Agent Reports

The Reports button in the top bar opens a modal that renders markdown reports pushed by
external agents (e.g. Hermes cron jobs). Reports are stored in SQLite; only the newest
report per slug is kept — a re-run of the same job replaces that day's report, and a new
day's brief replaces the previous day's entirely. Obsidian-style YAML frontmatter is
stripped from previews and the rendered view; the renderer escapes all HTML.

Push a report (the write routes honor `EDIT_TOKEN` via `X-Edit-Token`, same as watchlist
edits):

```bash
curl -X POST https://your-board/api/reports \
  -H "Content-Type: application/json" \
  -H "X-Edit-Token: $EDIT_TOKEN" \
  --data @- <<'JSON'
{"title": "Biotech Pharma Brief", "body": "## Overnight\n- item one", "date": "2026-07-10"}
JSON
```

Or from a file with Python:

```bash
python - <<'PY'
import json, os, pathlib, urllib.request
body = pathlib.Path("report.md").read_text(encoding="utf-8")
req = urllib.request.Request(
    "https://your-board/api/reports",
    data=json.dumps({"title": "Biotech Pharma Brief", "body": body}).encode(),
    headers={"Content-Type": "application/json", "X-Edit-Token": os.environ["EDIT_TOKEN"]},
)
print(urllib.request.urlopen(req).read().decode())
PY
```

`date` defaults to today (UTC); `slug` defaults to the slugified title. `GET /api/reports`
lists metadata with previews, `GET /api/reports/{id}` returns the full body, and
`DELETE /api/reports/{id}` (token-gated) removes one.

### Key Dates

A `## Key Dates` section in any report body feeds the calendar panel on the Daily view
(styled after terminal key-date rails), one bullet per event:

```markdown
## Key Dates

- 2026-07-15 08:30 ET — PPI — Producer Price Index (June) [MACRO]
- 2026-07-17 — US monthly options expiration (opex) [OPEX]
- 2026-07-22 AMC — TSLA earnings [EARNINGS]
- 2026-07-16 — ARB unlock — 92.6M ARB (1.4% of circ supply) [CRYPTO]
```

Grammar: ISO date, optional time (`HH:MM` plus timezone word, or `AMC`/`BMO`), a dash or
colon separator, the title, and an optional trailing `[CATEGORY]` tag (defaults to
`EVENT`; `MACRO`, `CRYPTO`, `EARNINGS`, `OPEX`, and `HOLIDAY` get dedicated colors).
Malformed bullets are skipped, never fatal. The stored rows mirror their source report:
a re-run replaces that slug's events wholesale, deleting the report clears them, and
two briefs naming the same `(date, title)` share one calendar row. `GET /api/key-dates`
serves upcoming events from the current US-Eastern day forward (`days`, default 90).

### Automatic vault uploads

`scripts/vault_report_uploader.py` makes the pipeline hands-off: it scans a vault
directory for files named `YYYY-MM-DD <Title>.md` (the Hermes cron convention), uploads
new or changed ones, and remembers content hashes in
`~/.local/state/sector-tracker/vault-uploads.json` so nothing uploads twice. Only titles
on the cron-report allowlist upload — ad-hoc dated research notes in the vault stay off
the board. Config lives in `~/.config/sector-tracker/uploader.env` (`BOARD_URL`,
`EDIT_TOKEN`, `VAULT_DIR`, `MAX_AGE_DAYS`, `REPORT_TITLES` — comma-separated cron report
titles, case-insensitive; defaults to the known cron jobs, `*` disables the filter).
Run `--baseline` once at install to mark existing files as seen, and `--dry-run` to
preview.

The production wiring runs on the Hermes box (`hermes-ts`), which already receives the
Obsidian vault at `/home/ds/hermes-research` via Syncthing (macOS TCC blocks launchd
agents from reading `~/Desktop`, so the watcher runs there instead). The script is
installed at `~/.local/bin/vault_report_uploader.py` and driven by the systemd *user*
units in `deploy/` (lingering is enabled, so they run unattended):

- `sector-tracker-uploader.path` — fires the moment Syncthing writes a report file
- `sector-tracker-uploader.timer` — 30-minute sweep that catches in-place edits
- `sector-tracker-uploader.service` — one upload pass posting to the droplet board

## Configuration

Use the settings button in the app or edit `config/watchlists.yaml` to change groups and assets.
The board supports:

- `equity`
- `etf`
- `crypto_perp`
- `future` (Yahoo futures like `GC=F`; Globex session chip, no RVOL — Yahoo's
  historical futures volume uses a different counting regime than live prints)

Environment variables:

```bash
FINNHUB_API_KEY=
EDIT_TOKEN=                # when set, watchlist edits require this token
DATABASE_PATH=./data/market_board.sqlite3
DATABASE_SEED_PATH=./config/market_board_seed.sqlite3
WATCHLIST_PATH=./config/watchlists.yaml
WATCHLIST_SEED_PATH=./config/watchlists.yaml
QUOTE_POLL_SECONDS=10
HISTORY_REFRESH_SECONDS=3600
CRYPTO_ETF_FLOW_CACHE_SECONDS=900
NEWS_TELEGRAM_CHANNELS=marketfeed,RetardFrens,tradehaven,AGGRNEWSWIRE,WalterBloomberg   # public t.me handles; each gets a mute chip in the drawer
NEWS_POLL_SECONDS=15
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

Diagnostics: `/api/lighter-status` (feed cache freshness, 429 cooldowns) and
`/api/yahoo-status` (curl presence, live spark probe).

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

Viewing is public by design; watchlist edits should be locked before sharing the URL.
Set `EDIT_TOKEN` and the create/delete endpoints require it — the editor prompts for
the token once per browser and remembers it:

```bash
echo 'EDIT_TOKEN=pick-something-long' >> /opt/sector-tracker/.env
systemctl restart sector-tracker
```

For a fully private board, install [Tailscale](https://tailscale.com) on the VPS and
your devices (then firewall port 8787 to the tailnet), or front it with Caddy for
HTTPS + basic auth.

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
