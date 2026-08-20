# Cross-Asset Board

A private Bloomberg-style market board for manual sector and narrative baskets.

The app runs a FastAPI backend with a static dashboard frontend. The Daily Board computes
regime, breadth, benchmark, theme-strength, five-day rotation metrics, and BTC/ETH/SOL spot
ETF flow reads from live quotes and cached daily history. A macro tape (VIX, DXY, US 10Y)
rides above both views, VIX feeds a volatility read in the regime panel, and the Markets view
splits into TradFi, Crypto, and Commodities categories. TradFi keeps the clickable watchlist
grid — Last / Abs / 1D% / ΔOpen (move since today's session open; UTC day for crypto) /
RVOL (volume vs 20-day average) / trend sparkline — with the chart workflow; Crypto shows
the curated perp watchlist plus an auto-synced tape of every crypto perp listed on Hyperliquid
(~110 markets), grouped into Hyperliquid's own baskets (L1, DeFi, AI, L2, Memes, Other via its
tokenlist categories) and sortable by 24h volume, funding, and OI — new listings appear
without config changes, and every tape row charts on click. Commodities tracks Yahoo
continuous front-month futures (metals, energy, ags) with a Globex-aware session chip.
A Crypto Breadth panel on the Daily Board reads advance/decline, big movers, and funding
share across the full tape while the curated regime/breadth universe stays unpolluted.
A toggleable full-height news drawer streams public Telegram channels (scraped from their
t.me previews, no API key): the server polls every 15 seconds and pushes new posts to the
browser over the WebSocket, and each channel gets a per-browser mute chip.

The AI view has four live panels. Models normalizes the public OpenRouter catalog into
comparable input, output, cache, and 3:1 blended prices with provider, context, open-weight,
and intelligence metadata. Token Index prices the observed prompt/completion mix from
OpenRouter's public rankings and splits the result into broad, open-weight-proxy, and
proprietary series. AI Capex tracks reported quarterly capital expenditure for eight
hyperscalers and chip suppliers from Yahoo Finance company statements, including QoQ/YoY
changes, trailing-four-quarter spend, and capex/revenue intensity. Reported total capex is an
AI-infrastructure proxy because issuers do not consistently isolate AI-only spend. Hardware
compares normalized cloud GPU rental rates, provider offers, and workload cost estimates from
ComputePrices. Catalog, index, capex, and GPU observations accrue in SQLite
(`ai_model_snapshots`, `ai_token_index`, `ai_capex_history`,
`ai_gpu_compute_snapshots`) instead of treating today's upstream response as historical truth.
The token index is explicitly an OpenRouter market proxy, not a replica of Silicon Data's
multi-network benchmark.

Market data blends two worlds. Hyperliquid drives crypto perps end to end (quotes, candles,
funding, OI) and overlays live 24/7 prices onto the equities/ETFs its xyz dex lists as synthetic
perps — day change is measured against the last official session close, so weekend and
after-hours moves show up without breaking session semantics. Intraday chart candles come
from Hyperliquid wherever a market exists; daily bars, volume, profiles, and everything
analytics-related (DMAs, breadth, RVOL, 52W) stay on official Yahoo session data. Assets
not listed on Hyperliquid run fully on Yahoo.

For U.S. equities and ETFs, the chart modal can also load a MarketData.app option-chain
snapshot: expiry selection, ATM IV, put/call OI, call and put walls, max pain, and switchable
GEX/OI strike profiles. Net GEX is an explicit dealer-positioning proxy (calls positive, puts
negative), reported as dollars per 1% underlying move; it is not observed dealer inventory.
Open interest updates daily.

The daily board persists a condensed snapshot per UTC day (regime, breadth, theme scores)
to SQLite; the UI uses it for the 50DMA breadth trend sparkline and day-over-day theme
score deltas, and `/api/snapshots?days=30` serves the raw history.

Watchlists live in YAML and can also be edited in the app. Quotes and OHLC bars are cached in
SQLite, and market data providers are isolated behind a common interface so Yahoo, Hyperliquid,
Stooq, and Farside can be swapped or extended.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

The Trends tab draws PCPartPicker-style performance bands for every watchlist
group from the cached daily bars: each member is indexed to 100 at the window
start, the shaded envelope spans the min–max member, and the line is the
equal-weight average (1M/3M/6M/1Y windows via `GET /api/trends?days=`). Cards
cross-link into the Markets view filtered to that group. Below the bands, the
tab embeds [PCPartPicker's](https://pcpartpicker.com/trends/) daily component
price-trend charts (memory, CPUs, video cards, storage, PSUs, monitors) —
street prices for DRAM/NAND lead the board's MEMORY equity theme. The backend
scrapes the public gallery lists (`GET /api/component-trends`, cached 6h) and
serves the PNGs same-origin through `GET /api/component-image`.

The Earnings tab shows the trading week's report calendar as Mon–Fri day
cards (`GET /api/earnings`, optional `?start=` snaps to that date's week).
Rows come from Nasdaq's public calendar API — every reporting company with
EPS consensus, report session (BMO/AMC/TNS), and market cap. A single
batched TradingView Scanner request adds exact scheduled/estimated release
timestamps, rendered in CET/CEST; missing or conflicting timestamps fall
back to the session instead of inventing a time. Per day the top seven
names rank board-held symbols first, then market cap, then analyst coverage;
the rest collapse into a "+N more reports" count. Detailed rows carry a
last-4-quarters beat/miss strip (Nasdaq earnings-surprise API) and, for held
symbols only, an options-implied move (ATM IV from MarketData.app scaled to
the first expiration after the report). Day lists and release times cache
6h and surprise histories 24h with stale-on-error fallback.

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

The Reports button in the top bar opens the report library: every brief ever
pushed, grouped by date newest first, filterable by brief via facet chips, with
"Load older reports" paging back through the archive (`GET
/api/reports?limit=&offset=&slug=` serves the pages plus `has_more` and the
distinct-slug `filters` facets). Reports are stored in SQLite keyed by
`(slug, date)`: a re-run of the same job replaces that day's report in place
(keeping its original `created_at` stamp), while prior days are retained as the
archive. Same-slug uploads dated older than the newest brief are archived under
their own date but never drive projections — the fringe book and key dates
follow only the newest brief per slug, so vault backfills are always safe.
`#report=<id>` deep links work everywhere: markdown links inside a
report body navigate the reader in place, and a bare `https://<board>/#report=<id>`
URL boots the dashboard with that report open. Obsidian-style YAML frontmatter
is stripped from previews and the rendered view; the renderer escapes all HTML.

Reports can carry data-bearing charts: a fenced ` ```chart ` block with a small
declarative spec renders as an inline SVG (no external assets, values frozen in
the report). A malformed spec falls back to a plain code block.

~~~markdown
```chart
type: bar            # bar | line (default line)
title: Crypto ETF flows, $M
unit: $M             # optional, appended to value labels
labels: Mon, Tue, Wed, Thu, Fri
series: 120, -45, 300, 210, -80
```
~~~

Line charts accept up to four series, optionally named (`series: SPY: 0.2, 0.8, 1.4`);
named series get a legend with the last value. Bar charts take one series and color
bars by sign. Limits: 120 points per series.

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

Any section whose heading mentions **calendar** or **key dates** feeds the calendar
panel on the Daily view (styled after terminal key-date rails). Hermes briefs need no
changes: their `## Economic Calendar (CEST)` / `### Today's Calendar — CET` markdown
tables are parsed as-is — the first column is the when-cell, the second the event.
Dates resolve against the report date: a `### Tuesday, July 14, 2026` subheading pins
the rows below it, weekday tokens (`11:00 Wed`) roll forward to the next occurrence,
month-day tokens (`Thu Jul 16 14:30`) are explicit, and bare times mean the report's
own day. A timezone in the section heading is appended to bare times, so the panel
shows exactly the zone the agent wrote.

Other agents can feed the panel with explicit bullets, one per event:

```markdown
## Key Dates

- 2026-07-15 08:30 ET — PPI — Producer Price Index (June) [MACRO]
- 2026-07-22 AMC — TSLA earnings [EARNINGS]
- 2026-07-16 — ARB unlock — 92.6M ARB (1.4% of circ supply) [CRYPTO]
```

Grammar: ISO date, optional time (`HH:MM` plus timezone word, or `AMC`/`BMO`), a dash or
colon separator, the title, and an optional trailing `[CATEGORY]` tag. Untagged titles
infer a category from keywords (earnings/opex/holiday/unlock); tables default to `MACRO`,
bullets to `EVENT`. `MACRO`, `CRYPTO`, `EARNINGS`, `OPEX`, and `HOLIDAY` get dedicated
colors. Malformed rows are skipped, never fatal. The stored rows mirror their source
report: a re-run replaces that slug's events wholesale, deleting the report clears them,
and two briefs naming the same `(date, title)` share one calendar row.
`GET /api/key-dates` serves upcoming events from the current US-Eastern day forward
(`days`, default 90).

Macro events are enriched at serve time from TradingView's public economic calendar:
each matched item gains a `release` object with consensus, previous, actual, surprise,
importance, and an indicator description, refreshed every ~20s around scheduled release
times so actuals land within about a minute (pushed over the WS as `key_dates` frames).
Matching is fuzzy-title within ±1 day of the stored date; unmatched events keep
`release: null`. Nothing is persisted, and a calendar outage degrades to the plain
payload. `ECON_CALENDAR_COUNTRIES` filters the source feed and
`ECON_CALENDAR_CACHE_SECONDS` sets the idle cache TTL.

### Fringe Corner

A Hermes-managed book of daily fringe trading ideas, fed through the same pipeline as
every other report: the Hermes cron writes a dated markdown brief into the vault,
Syncthing ships it to the uploader box, and the vault uploader POSTs it to
`/api/reports`. Any section whose heading mentions **fringe** feeds the book, one
action per bullet:

```markdown
## Fringe Corner

- OPEN LONG CIFR — miner squeeze into the halving narrative [conf: 60%] [stop: $7.40] [target: $12] [horizon: 2w]
- HOLD SHORT XLU — utilities still crowded, thesis intact
- CLOSE LONG NVDA — earnings played out, taking the win
```

Grammar: `ACTION DIRECTION TICKER — text`, where ACTION is `OPEN`/`HOLD`/`CLOSE`
(case-insensitive), DIRECTION is `LONG`/`SHORT`, the ticker is an uppercase
`[A-Z0-9.-=]` token (`BRK-B`, `ES=F`, `BTC`), the separator is an em-dash, colon, or
spaced hyphen, and optional trailing `[conf: ...]` / `[stop: ...]` / `[target: ...]` /
`[horizon: ...]` tags carry free text in any order. A price-looking number in the
target/stop (`$12`, `78.50`, `75k`) is parsed out; `conf` accepts `60%`, `60`, or
`0.6` and clamps to 5–95%. Malformed bullets are skipped, never fatal.

The book runs a **$10,000 paper portfolio sized by half-Kelly, calibrated by
its own track record**. At entry-stamp time the board computes `b = |target −
entry| / |entry − stop|` and `f* = conf − (1 − conf)/b`, commits `f*/2` of the
bankroll (floored at 2%, capped at 25% per position and 100% gross exposure;
bankroll = $10k + cumulative realized dollars), and fixes the notional for the
life of the position — HOLD never resizes. Three risk gates sit on top:

- **Calibration cap**: while the realized win rate is below 35% (5+ closed
  trades), declared confidence is ignored and each OPEN sizes at a fixed risk
  budget — 0.75% of the bankroll at the declared stop distance (2% floor with
  no usable stop).
- **Circuit breakers**: 3 consecutive losses halve new sizes; 5 consecutive
  losses, or a negative rolling expectancy over 8+ closed trades, size new
  OPENs at $0.
- **Gap-risk haircut**: stops wider than 5% scale the notional down linearly
  (`5% / stop distance`) — gaps jump wide stops.

Missing or geometrically broken conf/stop inputs fall back to a 5% default.
`/api/fringe` carries per-idea `size_notional`, `unrealized_usd`/`realized_usd`,
a `summary.portfolio` block (equity, return, exposure), and a `stats.risk_mode`
block exposing every gate; every pre-capital idea — open positions and priced
closes alike — was grandfathered at a flat $1,000, so historical realized
results count in dollars too. Closed ideas also carry **MAE/MFE** (max
adverse/favorable excursion from cached daily bars) and the stats block
averages them plus the giveback — how much of the best mark evaporated by the
close.

Declared stops AND targets are **enforced intraday**: `scripts/fringe_stop_monitor.py`
runs on the Hermes box every 5 minutes around the clock, and when a position's mark
crosses either declared barrier on two consecutive ticks (bad-tick filter) it closes
the position through `POST /api/fringe/{id}/close` (edit-token gated). The board
re-marks at its own fresh price — gaps close with honest slippage, not the barrier
print — and the close lands in the ledger as `auto-stop: ...` / `auto-target: ...` for
the agent to review in its next brief; a move with legs beyond a harvested target is
re-opened next brief as a fresh, re-sized bet. Positions without a declared stop
cannot be stop-enforced; those trigger one alert per day when the mark sits 10%+
against entry. New ideas still open only through the daily brief.

The same monitor runs a **trailing-stop ratchet**: once a position moves +1R in
the idea's favor, the working stop lifts to breakeven; beyond that it trails 1R
behind the mark and only ever tightens (state lives in the monitor's JSON file,
the declared stop on the board stays the original). A trailed close lands as
`auto-trail: ...` with the declared stop noted for context.

Two housekeeping jobs feed the loop back into the agent. Before each weekday
brief (13:45 Berlin) `scripts/fringe_stats_notepad.py` stamps the rolling track
record — win rate, expectancy, streak, direction/asset buckets, open
giveback-to-stops, and the active risk mode — into the Fringe cron job's
durable notepad (`fringe_stats`), which the brief prompt reads first. On
Fridays (15:30 Berlin) `scripts/fringe_weekly_review.py` writes a five-bullet
self-review into the vault and posts it to the board as the `Fringe Weekly
Review` report. Both support `--dry-run`.

New ideas pass a **mandatory due-diligence stage** before they may open (briefs dated
2026-07-31 onward). The cron agent researches each candidate independently of the
morning briefs — fresh web searches for the latest coverage plus, for US
equities/ETFs, the ticker's recent SEC filings from EDGAR (8-K material events,
S-3/424B dilution, 13D/G activists, earnings timing) — and records a per-ticker
verdict in a `## Due Diligence` section: `### <TICKER> — CONFIRMED` with dated,
source-linked findings, or `— REJECTED` with the disqualifier. The uploader (and the
pipeline watchdog behind it) enforces the contract: a brief whose `## Fringe Corner`
section OPENs a ticker without a matching CONFIRMED, source-linked block is rejected
before it reaches the board. HOLD/CLOSE-only briefs need no section.

Hermes manages its own book explicitly — unlike Key Dates the ledger **accrues**
instead of mirroring. `OPEN` on an already-open `(ticker, direction)` idea just
refreshes the thesis/horizon/target (entry price and opened date are preserved); `HOLD`
updates the note; `HOLD` with nothing open opens forgivingly; `CLOSE` stamps the
close date, reason, and exit price; `CLOSE` with nothing open is ignored. Ideas the
latest report does not mention stay open and are flagged stale in the panel. The one
mirror-like rule is same-day: re-uploading the same slug on the same date replays the
actions idempotently and retracts ideas the run created that day but no longer
mentions (they never really existed) — prior days' ideas are never rolled back, and
deleting a report leaves the book intact.

Entry prices are stamped at ingest and exits at close, using Hyperliquid for tickers it
lists as crypto and Yahoo for everything else (arbitrary tickers work; the watchlist
is not consulted). A provider outage leaves the price null and the next `/api/fringe`
build re-stamps it lazily. `GET /api/fringe` serves the open book marked to market
(~60s quote cache) with unrealized P&L and distance-to-target plus the ten most
recent closes with realized P&L.

`GET /api/market-context?days=30` (days clamped to 7..90) is the digest that gives
Hermes continuous market memory instead of a moment: daily board snapshot history
(same rows as `/api/snapshots`), 5d/20d watchlist leaders/laggards from cached daily
bars, per-asset ETF flow history accrued from every successful Farside fetch, the
next week of key dates (with release enrichment), and its own fringe book with P&L.
Every piece degrades to empty on failure — the digest never 500s.

On the uploader box, add the report title to `REPORT_TITLES` in
`~/.config/sector-tracker/uploader.env` (e.g. `REPORT_TITLES=...,Fringe Corner`) or
the vault watcher will skip the file.

Reference skeleton for the Hermes cron job:

```text
1. GET $BOARD/api/market-context?days=30 — regime/breadth history, movers,
   ETF flows, upcoming key dates, and your current book with P&L.
2. Read today's research briefs.
3. Vet every OPEN candidate with independent research: web-search the last
   week of coverage, pull recent EDGAR filings for US equities/ETFs, and
   record a CONFIRMED/REJECTED verdict per ticker in "## Due Diligence"
   (dated findings with source links; REJECTED stays on the record).
4. Write the daily report with a "## Fringe Corner" section that manages the
   open book EXPLICITLY: HOLD every idea you still like (updated note),
   CLOSE what is done or invalidated (reason), OPEN only CONFIRMED ideas
   (thesis + [conf: ...] + [stop: ...] + [target: ...] + [horizon: ...]).
   Unmentioned ideas stay open but go stale.
```

### Automatic vault uploads

`scripts/vault_report_uploader.py` makes the pipeline hands-off: it scans a vault
directory for files named `YYYY-MM-DD <Title>.md` (the Hermes cron convention), uploads
new or changed ones, and remembers content hashes in
`~/.local/state/sector-tracker/vault-uploads.json` so nothing uploads twice. Only titles
on the cron-report allowlist upload — ad-hoc dated research notes in the vault stay off
the board. Known daily reports are contract-checked before upload (dated YAML plus
report-specific structural markers), so incomplete cron output is retried instead of
replacing the dashboard report. Config lives in
`~/.config/sector-tracker/uploader.env` (`BOARD_URL`, `EDIT_TOKEN`, `VAULT_DIR`,
`MAX_AGE_DAYS`, `REPORT_TITLES`, `ALERT_TARGET`, `NOTIFY_TARGET` — report titles are
comma-separated and case-insensitive; defaults cover the known cron jobs, `*` disables
the filter). `NOTIFY_TARGET` announces each landed batch through the Hermes gateway
(comma-separated `hermes send` targets, e.g. `slack:#market-briefs, telegram`):
"New briefs on the dashboard: <titles> → <BOARD_URL>". Delivery failures are logged,
never fatal. Run `--baseline` once at install to mark existing files as seen, and
`--dry-run` to preview.

The production wiring runs on the Hermes box (`hermes-ts`), which already receives the
Obsidian vault at `/home/ds/hermes-research` via Syncthing (macOS TCC blocks launchd
agents from reading `~/Desktop`, so the watcher runs there instead). The script is
installed at `~/.local/bin/vault_report_uploader.py` and driven by the systemd *user*
units in `deploy/` (lingering is enabled, so they run unattended):

- `sector-tracker-uploader.path` — fires the moment Syncthing writes a report file
- `sector-tracker-uploader.timer` — 30-minute sweep that catches in-place edits
- `sector-tracker-uploader.service` — one upload pass posting to the HTTPS board
- `sector-tracker-report-watchdog.timer` — checks each weekday delivery every 10 minutes
  from 09:00–15:50 Europe/Berlin, after staged per-report deadlines (the cron fleet
  publishes on Berlin wall times: morning briefs 09:00, US&Asia Close 11:00, Macro
  Tape 13:30, Fringe Corner 14:00 — DST-stable for the reader)
- `sector-tracker-report-watchdog.service` — validates vault files, repairs missed
  uploads, compares dashboard bodies, checks the Fringe ledger, and sends edge-triggered
  failure/recovery alerts through Hermes
- `sector-tracker-stops.timer` / `.service` — the 5-minute, 24/7 auto-stop monitor
  described in the Fringe Corner section
- `sector-tracker-backup.timer` / `.service` — nightly (04:10 UTC) off-box database
  backup: pulls a consistent snapshot from the token-gated `GET /api/backup`
  (`VACUUM INTO` on the droplet), verifies integrity and the irreplaceable tables,
  gzips it into the Syncthing-mirrored vault (`~/hermes-research/.board-backups/`,
  14 kept), and alerts through Hermes on failure. Restore: gunzip a snapshot over
  `data/market_board.sqlite3` and restart the service. The chain gives three copies:
  droplet (live) → Hermes box → Mac.

`deploy/install-hermes.sh [host]` is the idempotent installer for everything above:
it syncs the scripts and systemd user units from this repo to the Hermes box,
verifies checksums, reloads systemd, and enables every trigger — the box matching
git is a command, not a hope.

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
EDIT_TOKEN=                # when set, watchlist edits require this token
DATABASE_PATH=./data/market_board.sqlite3
DATABASE_SEED_PATH=./config/market_board_seed.sqlite3
WATCHLIST_PATH=./config/watchlists.yaml
WATCHLIST_SEED_PATH=./config/watchlists.yaml
QUOTE_POLL_SECONDS=10
HISTORY_REFRESH_SECONDS=3600
CRYPTO_ETF_FLOW_CACHE_SECONDS=900
MARKETDATA_TOKEN=                                  # server-side API token; leave empty to disable options snapshots
MARKETDATA_BASE_URL=https://api.marketdata.app     # MarketData.app REST API origin
OPTIONS_CACHE_SECONDS=60                           # chain snapshot cache, 15-900 seconds
COMPUTEPRICES_API_KEY=                  # optional free key; public GPU pages are the keyless fallback
ECON_CALENDAR_CACHE_SECONDS=300         # key-dates enrichment cache; auto-drops to 20s around releases
ECON_CALENDAR_COUNTRIES=US,EU,DE,GB,JP,CN
NEWS_TELEGRAM_CHANNELS=marketfeed,RetardFrens,tradehaven,AGGRNEWSWIRE,WalterBloomberg   # public t.me handles; each gets a mute chip in the drawer
NEWS_POLL_SECONDS=15
```

Crypto ETF flow data uses public Farside tables via a text-rendered fetch route and is cached by
`CRYPTO_ETF_FLOW_CACHE_SECONDS`.

MarketData.app options data is requested only when an eligible chart modal opens. Create an API
token in the [MarketData.app dashboard](https://dashboard.marketdata.app/), set
`MARKETDATA_TOKEN` in the server's `.env`, and restart the app. The token stays server-side;
browsers call `/api/options/{symbol}` and never receive credentials. The integration omits the
optional `mode` parameter, so MarketData.app applies the account default: paid accounts default
to live mode, while free and trial accounts receive delayed data.

Hardware uses the authenticated ComputePrices v1 API when `COMPUTEPRICES_API_KEY` is set.
Without a key, it reads the public GPU comparison and top-model provider pages; this keyless
mode has the same current-price semantics but fewer detailed models. The browser never receives
the API key.

## Smoke Tests

```bash
pytest -v
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/groups
curl http://127.0.0.1:8000/api/quotes
curl http://127.0.0.1:8000/api/snapshots
curl http://127.0.0.1:8000/api/options/SPY   # requires MARKETDATA_TOKEN
```

Diagnostics: `/api/hyperliquid-status` (feed cache freshness, 429 cooldowns) and
`/api/yahoo-status` (curl presence, live spark probe).

## Deployment

### VPS (recommended)

A single long-lived process is what this architecture wants: warm caches (no funding
flicker), background quote/history loops, live WebSocket streaming, accruing daily
snapshots, durable watchlist edits, and a dedicated rate-limit budget for Hyperliquid/Yahoo.

On a fresh Ubuntu 22.04/24.04 (or Debian 12) server, install Tailscale and join
the server to your tailnet first. Then run:

```bash
curl -fsSL https://raw.githubusercontent.com/MaybeNot2day/sector-tracker/main/deploy/setup-vps.sh | sudo bash
```

The idempotent installer puts the app under `/opt/sector-tracker`, binds Uvicorn
to loopback, and publishes it publicly through Tailscale Funnel HTTPS. The systemd
unit runs as a dedicated, sandboxed user. Runtime installs use the hashed,
fully pinned `requirements.txt`; the auto-deploy timer polls `origin/main`,
restarts only after installing that lock, and rolls back any revision whose
local `/api/health` check fails.

```bash
# after setup
open https://YOUR-TAILSCALE-DNS-NAME
journalctl -u sector-tracker -f
systemctl restart sector-tracker
```

The setup generates a random `EDIT_TOKEN`; keep it in a password manager and
configure the same value in the Hermes uploader. To rotate it:

```bash
sudo sed -i 's/^EDIT_TOKEN=.*/EDIT_TOKEN=NEW_RANDOM_VALUE/' /opt/sector-tracker/.env
sudo systemctl restart sector-tracker
```

Read access is public through Tailscale Funnel. Mutation endpoints still require
`X-Edit-Token`; the browser keeps that token only for the current tab session.

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
