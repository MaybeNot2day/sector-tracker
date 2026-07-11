from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import httpx

# Telegram's public web preview (t.me/s/<channel>) serves the last ~20 posts
# of any public channel without auth — same spirit as the Farside scrape.
# Private channels have no preview and cannot be read without a logged-in
# Telegram session, which this app deliberately avoids.
PREVIEW_URL = "https://t.me/s/{channel}"
FETCH_TIMEOUT = 10.0
MAX_FEED_ITEMS = 100
# One full t.me preview page (~20 posts) — the floor each channel retains.
MIN_ITEMS_PER_CHANNEL = 20
USER_AGENT = "Mozilla/5.0"
MAX_FETCH_CONCURRENCY = 6

_POST_RE = re.compile(r'data-post="([^"]+)"')
_TEXT_RE = re.compile(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TIME_RE = re.compile(r'<time datetime="([^"]+)"')
_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>")


class NewsService:
    """Merged live feed from public Telegram channels.

    The background loop calls refresh() every poll interval and broadcasts
    over the quotes WebSocket when new posts appear; /api/news serves the
    same cache for initial paint and non-WS deployments.
    """

    def __init__(self, channels: list[str], *, cache_seconds: int = 15) -> None:
        self.channels = channels
        self.cache_seconds = cache_seconds
        self._items: dict[str, dict[str, Any]] = {}
        self._titles: dict[str, str] = {}
        self._fetched = 0.0
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._fetch_semaphore = asyncio.Semaphore(MAX_FETCH_CONCURRENCY)

    async def get_feed(self) -> dict[str, object]:
        await self.refresh()
        return self.feed_payload()

    async def refresh(self) -> int:
        """Fetch all channels once per cache window; returns new-item count."""
        if not self.channels or monotonic() - self._fetched < self.cache_seconds:
            return 0
        async with self._lock:
            if monotonic() - self._fetched < self.cache_seconds:
                return 0
            pages = await asyncio.gather(
                *(self._fetch_channel(channel) for channel in self.channels),
                return_exceptions=True,
            )
            new_items = 0
            for channel, page in zip(self.channels, pages, strict=True):
                if not isinstance(page, str) or not page:
                    continue
                title = _TITLE_RE.search(page)
                if title:
                    self._titles[channel] = html.unescape(title.group(1))
                for item in parse_channel_page(channel, page):
                    if item["id"] not in self._items:
                        new_items += 1
                    self._items[item["id"]] = item
            self._trim()
            self._fetched = monotonic()
            return new_items

    def feed_payload(self) -> dict[str, object]:
        items = sorted(self._items.values(), key=lambda item: item["timestamp"], reverse=True)
        return {
            "items": [
                {**item, "channel_title": self._titles.get(item["channel"], item["channel"])}
                for item in items
            ],
            "channels": self.channels,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    def _trim(self) -> None:
        if len(self._items) <= MAX_FEED_ITEMS:
            return
        # Fair retention: a firehose channel (marketfeed posts every minute or
        # two) must not evict slower channels out of the shared window — t.me
        # previews only serve the last ~20 posts, so once a slow channel's
        # newest post ages past the firehose's cap it would never come back.
        # Every channel always keeps its newest slice (at least one full
        # preview page); the total stays near MAX_FEED_ITEMS.
        per_channel = max(MAX_FEED_ITEMS // max(len(self.channels), 1), MIN_ITEMS_PER_CHANNEL)
        by_channel: dict[str, list[dict[str, Any]]] = {}
        for item in self._items.values():
            by_channel.setdefault(item["channel"], []).append(item)
        kept: list[dict[str, Any]] = []
        for channel_items in by_channel.values():
            channel_items.sort(key=lambda item: item["timestamp"], reverse=True)
            kept.extend(channel_items[:per_channel])
        self._items = {item["id"]: item for item in kept}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True)
        return self._client

    async def _fetch_channel(self, channel: str) -> str:
        async with self._fetch_semaphore:
            try:
                response = await self._http_client().get(
                    PREVIEW_URL.format(channel=channel),
                    headers={"User-Agent": USER_AGENT},
                )
                response.raise_for_status()
                return response.text
            except Exception:
                return ""


def parse_channel_page(channel: str, page: str) -> list[dict[str, Any]]:
    """Extract posts from a t.me/s preview page.

    Splits the page on data-post anchors so each post's text and timestamp
    stay paired; media-only posts (no text div) are skipped.
    """
    posts = list(_POST_RE.finditer(page))
    items: list[dict[str, Any]] = []
    for index, match in enumerate(posts):
        post_id = match.group(1)
        if not post_id.startswith(f"{channel}/"):
            continue
        start = match.end()
        end = posts[index + 1].start() if index + 1 < len(posts) else len(page)
        block = page[start:end]
        text_match = _TEXT_RE.search(block)
        time_match = _TIME_RE.search(block)
        if not text_match or not time_match:
            continue
        text = _clean_text(text_match.group(1))
        timestamp = _parse_time(time_match.group(1))
        if not text or timestamp is None:
            continue
        items.append(
            {
                "id": post_id,
                "channel": channel,
                "text": text,
                "timestamp": timestamp.isoformat(),
                "link": f"https://t.me/{post_id}",
            }
        )
    return items


def _clean_text(raw: str) -> str:
    text = _BR_RE.sub("\n", raw)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
