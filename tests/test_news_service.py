from datetime import datetime
from typing import Any

import pytest

from app.config import Settings
from app.services import news as news_module
from app.services.news import NewsService, parse_channel_page

CHANNEL = "marketfeed"
PREVIEW_PREFIX = news_module.PREVIEW_URL.format(channel="")


def post_block(post_id: str, text_html: str | None, time_attr: str | None) -> str:
    """One t.me/s message block, mirroring the real preview markup shape."""
    parts = [f'<div class="tgme_widget_message" data-post="{post_id}">']
    if text_html is not None:
        parts.append(
            f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text_html}</div>'
        )
    if time_attr is not None:
        parts.append(
            f'<a class="tgme_widget_message_date" href="https://t.me/{post_id}">'
            f'<time datetime="{time_attr}" class="time">08:09</time></a>'
        )
    parts.append("</div>")
    return "".join(parts)


def page(*blocks: str, title: str | None = None) -> str:
    head = f'<meta property="og:title" content="{title}">' if title is not None else ""
    return f"<html><head>{head}</head><body>{''.join(blocks)}</body></html>"


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTP:
    """Stands in for httpx.AsyncClient, routing channel handles to preview pages."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[str] = []
        self.constructions = 0
        self.closes = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        class _Client:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                fake.constructions += 1

            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

            async def aclose(self) -> None:
                fake.closes += 1

            async def get(self, url: str, headers: dict[str, str] | None = None) -> FakeResponse:
                channel = url.removeprefix(PREVIEW_PREFIX)
                fake.requests.append(channel)
                result = fake.routes[channel]
                if isinstance(result, Exception):
                    raise result
                return result if isinstance(result, FakeResponse) else FakeResponse(result)

        monkeypatch.setattr(news_module.httpx, "AsyncClient", _Client)


# ---------------------------------------------------------------------------
# parse_channel_page
# ---------------------------------------------------------------------------


def test_parser_pairs_text_and_time_per_post_and_builds_item_shape() -> None:
    html_page = page(
        post_block(f"{CHANNEL}/101", "First headline", "2026-07-07T08:09:55+00:00"),
        post_block(f"{CHANNEL}/102", "Second headline", "2026-07-07T09:00:00+00:00"),
    )

    items = parse_channel_page(CHANNEL, html_page)

    assert items == [
        {
            "id": f"{CHANNEL}/101",
            "channel": CHANNEL,
            "text": "First headline",
            "timestamp": "2026-07-07T08:09:55+00:00",
            "link": f"https://t.me/{CHANNEL}/101",
        },
        {
            "id": f"{CHANNEL}/102",
            "channel": CHANNEL,
            "text": "Second headline",
            "timestamp": "2026-07-07T09:00:00+00:00",
            "link": f"https://t.me/{CHANNEL}/102",
        },
    ]


def test_parser_cleans_tags_breaks_entities_and_whitespace() -> None:
    raw = (
        "<b>BREAKING:</b>  Fed &amp; Treasury<br/><br/>"
        "  &#036;42 &quot;deal&quot;  <br>"
        '<a href="https://example.com">link text</a>'
    )
    html_page = page(post_block(f"{CHANNEL}/1", raw, "2026-07-07T08:09:55+00:00"))

    items = parse_channel_page(CHANNEL, html_page)

    assert items[0]["text"] == 'BREAKING: Fed & Treasury\n$42 "deal"\nlink text'


def test_parser_skips_foreign_channel_posts_from_reposts() -> None:
    html_page = page(
        post_block("otherchan/55", "Quoted repost body", "2026-07-07T07:00:00+00:00"),
        post_block(f"{CHANNEL}/7", "Own post", "2026-07-07T08:00:00+00:00"),
    )

    items = parse_channel_page(CHANNEL, html_page)

    assert [item["id"] for item in items] == [f"{CHANNEL}/7"]


@pytest.mark.parametrize(
    ("name", "text_html", "time_attr"),
    [
        ("media only, no text div", None, "2026-07-07T08:00:00+00:00"),
        ("no time tag", "Has text", None),
        ("text cleans to empty", "<b> </b>&nbsp;", "2026-07-07T08:00:00+00:00"),
        ("invalid datetime", "Has text", "not-a-date"),
    ],
)
def test_parser_skips_incomplete_posts(
    name: str, text_html: str | None, time_attr: str | None
) -> None:
    html_page = page(
        post_block(f"{CHANNEL}/1", text_html, time_attr),
        post_block(f"{CHANNEL}/2", "Kept", "2026-07-07T09:00:00+00:00"),
    )

    items = parse_channel_page(CHANNEL, html_page)

    assert [item["id"] for item in items] == [f"{CHANNEL}/2"], name


@pytest.mark.parametrize(
    ("name", "time_attr", "expected"),
    [
        ("naive treated as UTC", "2026-07-07T08:09:55", "2026-07-07T08:09:55+00:00"),
        ("offset normalized to UTC", "2026-07-07T10:09:55+02:00", "2026-07-07T08:09:55+00:00"),
        ("already UTC round-trips", "2026-07-07T08:09:55+00:00", "2026-07-07T08:09:55+00:00"),
    ],
)
def test_parser_normalizes_timestamps_to_utc(name: str, time_attr: str, expected: str) -> None:
    html_page = page(post_block(f"{CHANNEL}/1", "Text", time_attr))

    items = parse_channel_page(CHANNEL, html_page)

    assert items[0]["timestamp"] == expected, name


# ---------------------------------------------------------------------------
# NewsService.refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_within_ttl_skips_http_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP(
        {
            "alpha": page(post_block("alpha/1", "A1", "2026-07-07T08:00:00+00:00")),
            "beta": page(post_block("beta/1", "B1", "2026-07-07T08:01:00+00:00")),
        }
    )
    fake.install(monkeypatch)
    service = NewsService(["alpha", "beta"], cache_seconds=1000)

    assert await service.refresh() == 2
    assert sorted(fake.requests) == ["alpha", "beta"]

    assert await service.refresh() == 0
    assert len(fake.requests) == 2  # no second round of HTTP inside the window


@pytest.mark.asyncio
async def test_refresh_counts_only_previously_unseen_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page = page(post_block("alpha/1", "A1", "2026-07-07T08:00:00+00:00"))
    fake = FakeHTTP({"alpha": first_page})
    fake.install(monkeypatch)
    service = NewsService(["alpha"], cache_seconds=0)

    assert await service.refresh() == 1
    # Same page again: fetched, but nothing new.
    assert await service.refresh() == 0
    assert len(fake.requests) == 2

    fake.routes["alpha"] = page(
        post_block("alpha/1", "A1", "2026-07-07T08:00:00+00:00"),
        post_block("alpha/2", "A2", "2026-07-07T08:05:00+00:00"),
    )
    assert await service.refresh() == 1


@pytest.mark.asyncio
async def test_refresh_reuses_and_closes_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeHTTP(
        {"alpha": page(post_block("alpha/1", "A1", "2026-07-07T08:00:00+00:00"))}
    )
    fake.install(monkeypatch)
    service = NewsService(["alpha"], cache_seconds=0)

    await service.refresh()
    await service.refresh()

    assert fake.requests == ["alpha", "alpha"]
    assert fake.constructions == 1
    await service.aclose()
    assert fake.closes == 1



@pytest.mark.asyncio
async def test_refresh_skips_failing_channel_but_keeps_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP(
        {
            "good": page(post_block("good/1", "Fine", "2026-07-07T08:00:00+00:00")),
            "down": RuntimeError("connection refused"),
            "empty": "",
        }
    )
    fake.install(monkeypatch)
    service = NewsService(["good", "down", "empty"], cache_seconds=0)

    assert await service.refresh() == 1

    ids = [item["id"] for item in service.feed_payload()["items"]]
    assert ids == ["good/1"]


@pytest.mark.asyncio
async def test_refresh_with_no_channels_returns_zero_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP({})
    fake.install(monkeypatch)
    service = NewsService([], cache_seconds=0)

    assert await service.refresh() == 0
    assert fake.requests == []
    assert fake.constructions == 0


# ---------------------------------------------------------------------------
# NewsService.feed_payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_payload_sorts_newest_first_with_title_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHTTP(
        {
            "alpha": page(
                post_block("alpha/1", "Old alpha", "2026-07-07T08:00:00+00:00"),
                post_block("alpha/2", "New alpha", "2026-07-07T10:00:00+00:00"),
                title="Chan &amp; Title",
            ),
            # No og:title -> channel_title falls back to the handle.
            "beta": page(post_block("beta/1", "Mid beta", "2026-07-07T09:00:00+00:00")),
        }
    )
    fake.install(monkeypatch)
    service = NewsService(["alpha", "beta"], cache_seconds=1000)
    await service.refresh()

    payload = service.feed_payload()

    assert [(item["id"], item["channel_title"]) for item in payload["items"]] == [
        ("alpha/2", "Chan & Title"),
        ("beta/1", "beta"),
        ("alpha/1", "Chan & Title"),
    ]
    assert payload["channels"] == ["alpha", "beta"]
    assert datetime.fromisoformat(payload["updated_at"]).tzinfo is not None


# ---------------------------------------------------------------------------
# Trim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_trims_to_100_newest_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stamp(i: int) -> str:
        return f"2026-07-07T{i // 60:02d}:{i % 60:02d}:00+00:00"

    blocks = [post_block(f"alpha/{i}", f"Post {i}", stamp(i)) for i in range(1, 121)]
    fake = FakeHTTP({"alpha": page(*blocks)})
    fake.install(monkeypatch)
    service = NewsService(["alpha"], cache_seconds=1000)

    assert await service.refresh() == 120

    ids = [item["id"] for item in service.feed_payload()["items"]]
    assert len(ids) == 100
    assert ids[0] == "alpha/120"
    assert ids[-1] == "alpha/21"
    assert "alpha/20" not in ids


# ---------------------------------------------------------------------------
# Settings.news_channels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("strips @, whitespace, empties", "@a, b,,c ", ["a", "b", "c"]),
        ("single handle", "marketfeed", ["marketfeed"]),
        ("blank string yields none", "", []),
        ("only separators yields none", " , ,", []),
    ],
)
def test_news_channels_parses_comma_separated_handles(
    name: str, raw: str, expected: list[str]
) -> None:
    settings = Settings(news_telegram_channels=raw, _env_file=None)

    assert settings.news_channels == expected, name
