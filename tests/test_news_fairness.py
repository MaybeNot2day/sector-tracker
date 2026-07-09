"""Regression tests for per-channel fair retention in NewsService._trim.

The old trim kept the globally newest 100 items, so a firehose channel
(posting every minute) evicted every post of slower channels — and because
t.me previews only serve ~20 posts, a slow channel's items never came back.
The fixed trim keeps each channel's newest max(MAX // n_channels, 20) items.
"""

from typing import Any

from app.services.news import NewsService


def stamp(minute: int) -> str:
    """Deterministic ISO-8601 UTC timestamp, `minute` minutes past midnight."""
    return f"2026-07-07T{minute // 60:02d}:{minute % 60:02d}:00+00:00"


def make_item(channel: str, seq: int, minute: int) -> dict[str, Any]:
    return {
        "id": f"{channel}/{seq}",
        "channel": channel,
        "timestamp": stamp(minute),
        "text": f"Post {seq}",
    }


def fill(service: NewsService, channel: str, seqs: range, first_minute: int) -> None:
    """Insert one item per seq, timestamps advancing a minute per post."""
    for offset, seq in enumerate(seqs):
        item = make_item(channel, seq, first_minute + offset)
        service._items[item["id"]] = item


def test_trim_keeps_slow_channels_and_caps_firehose_at_its_quota() -> None:
    # 3 channels -> per-channel quota is max(100 // 3, 20) = 33.
    service = NewsService(["firehose", "slowa", "slowb"], cache_seconds=1000)
    # Slow channels last posted before the firehose window even starts:
    # a global newest-100 cut would evict every one of their posts.
    fill(service, "slowa", range(1, 11), first_minute=0)
    fill(service, "slowb", range(1, 11), first_minute=10)
    fill(service, "firehose", range(1, 121), first_minute=100)

    service._trim()

    ids = set(service._items)
    assert {f"slowa/{i}" for i in range(1, 11)} <= ids
    assert {f"slowb/{i}" for i in range(1, 11)} <= ids
    # Firehose keeps exactly its newest 33; everything older is evicted.
    firehose_ids = {item_id for item_id in ids if item_id.startswith("firehose/")}
    assert firehose_ids == {f"firehose/{i}" for i in range(88, 121)}
    assert len(ids) == 33 + 10 + 10


def test_trim_leaves_cache_untouched_at_cap() -> None:
    # Exactly 100 items, lopsided split: if trim ran anyway, the two-channel
    # quota of 50 would cut the firehose from 90 down to 50.
    service = NewsService(["firehose", "slow"], cache_seconds=1000)
    fill(service, "firehose", range(1, 91), first_minute=100)
    fill(service, "slow", range(1, 11), first_minute=0)
    before = dict(service._items)

    service._trim()

    assert service._items == before


def test_trim_single_channel_keeps_newest_hundred() -> None:
    # One channel -> quota is max(100 // 1, 20) = 100, not the 20-item floor.
    service = NewsService(["solo"], cache_seconds=1000)
    fill(service, "solo", range(1, 131), first_minute=0)

    service._trim()

    assert set(service._items) == {f"solo/{i}" for i in range(31, 131)}


def test_feed_payload_orders_newest_first_after_trim() -> None:
    # Two channels alternating minute-by-minute; 120 items total, 50 kept each.
    service = NewsService(["evens", "odds"], cache_seconds=1000)
    for seq in range(1, 61):
        for channel, minute in (("evens", 2 * seq), ("odds", 2 * seq + 1)):
            item = make_item(channel, seq, minute)
            service._items[item["id"]] = item

    service._trim()
    payload = service.feed_payload()

    items = payload["items"]
    assert len(items) == 100
    timestamps = [item["timestamp"] for item in items]
    assert timestamps == sorted(timestamps, reverse=True)
    assert items[0]["id"] == "odds/60"
    assert items[-1]["id"] == "evens/11"
