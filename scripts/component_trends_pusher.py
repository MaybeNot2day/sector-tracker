#!/usr/bin/env python3
"""Scrape PCPartPicker trend galleries and push them to the market board.

Cloudflare 403s PCPartPicker page fetches from datacenter IP ranges, so the
board's VPS cannot scrape the trend pages itself (their chart PNG CDN stays
reachable). This script runs on a residential-network machine (a macOS
LaunchAgent on the desktop, mirroring the vault uploader pattern), scrapes
the six category galleries, and POSTs the normalized payload to
POST /api/component-trends guarded by the board's EDIT_TOKEN.

Stdlib only and Python 3.9 compatible so it runs on macOS system python3.

Config lives in ~/.config/sector-tracker/uploader.env (KEY=VALUE lines):

    BOARD_URL=https://dashboard.example
    EDIT_TOKEN=...
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

CONFIG_PATH = Path.home() / ".config/sector-tracker/uploader.env"
BASE_URL = "https://pcpartpicker.com"
REQUEST_TIMEOUT = 30
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html",
}

CATEGORIES = (
    ("memory", "Memory"),
    ("cpu", "CPUs"),
    ("video-card", "Video Cards"),
    ("internal-hard-drive", "Storage"),
    ("power-supply", "Power Supplies"),
    ("monitor", "Monitors"),
)

# Mirrors app/services/component_trends.py: src+title pairs from the page's
# inline JS gallery; src repeats as thumb, titles carry \u002D escapes.
IMAGE_ENTRY = re.compile(
    r"src:\s*\"(//cdna\.pcpartpicker\.com/[^\"]+/images/trends/[^\"]+\.png)\"[^{}]*?"
    r"title:\s*\"([^\"]+)\"",
    re.DOTALL,
)


def load_config(path: Path = CONFIG_PATH) -> dict[str, str]:
    config: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()
    except OSError:
        pass
    return config


def parse_trend_charts(html: str) -> list[dict[str, str]]:
    charts: list[dict[str, str]] = []
    seen: set[str] = set()
    for src, raw_title in IMAGE_ENTRY.findall(html):
        if src in seen:
            continue
        seen.add(src)
        title = raw_title.encode("utf-8").decode("unicode_escape").strip()
        charts.append({"title": title, "image": "https:" + src})
    return charts


def fetch_category(slug: str, label: str) -> dict[str, object] | None:
    url = f"{BASE_URL}/trends/price/{slug}/"
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            html = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        print(f"fetch failed for {slug}: {exc}", file=sys.stderr)
        return None
    charts = parse_trend_charts(html)
    if not charts:
        print(f"no charts parsed for {slug}", file=sys.stderr)
        return None
    return {"slug": slug, "label": label, "url": url, "charts": charts}


def push(board_url: str, edit_token: str, payload: dict[str, object]) -> None:
    request = urllib.request.Request(
        board_url.rstrip("/") + "/api/component-trends",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Edit-Token": edit_token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        print(response.read().decode("utf-8", "replace"))


def main() -> int:
    config = load_config()
    board_url = config.get("BOARD_URL", "")
    edit_token = config.get("EDIT_TOKEN", "")
    if not board_url or not edit_token:
        print(f"BOARD_URL and EDIT_TOKEN required in {CONFIG_PATH}", file=sys.stderr)
        return 1

    categories: list[dict[str, object]] = []
    for slug, label in CATEGORIES:
        category = fetch_category(slug, label)
        if category is not None:
            categories.append(category)
    if not categories:
        print("every category scrape failed; nothing pushed", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)  # noqa: UP017 — macOS system python3 is 3.9
    payload: dict[str, object] = {
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "categories": categories,
    }
    try:
        push(board_url, edit_token, payload)
    except (urllib.error.URLError, OSError) as exc:
        print(f"push failed: {exc}", file=sys.stderr)
        return 1
    stamp = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    total = sum(len(cast("list[object]", category["charts"])) for category in categories)
    print(f"[{stamp}] pushed {len(categories)} categories ({total} charts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
