from __future__ import annotations

from typing import Any, cast

import pytest

from app.services.news import NewsService
from app.services.news_map import cluster_news


def _item(
    ident: str,
    text: str,
    timestamp: str,
    *,
    channel: str = "marketfeed",
    title: str = "Market Feed",
) -> dict[str, Any]:
    return {
        "id": ident,
        "channel": channel,
        "channel_title": title,
        "text": text,
        "timestamp": timestamp,
        "link": f"https://t.me/{ident}",
    }


def test_cluster_news_groups_semantically_same_story() -> None:
    items = [
        _item("marketfeed/1", "Fed cuts interest rates by 25bp", "2026-08-20T12:00:00+00:00"),
        _item(
            "walter/2",
            "Fed cuts interest rates by 25bp",
            "2026-08-20T12:02:00+00:00",
            channel="walter",
            title="Walter Bloomberg",
        ),
        _item(
            "marketfeed/3",
            "Nvidia reports record quarterly revenue",
            "2026-08-20T12:10:00+00:00",
        ),
    ]

    payload = cluster_news(items)

    clusters = cast(list[dict[str, Any]], payload["clusters"])
    fed = next(cluster for cluster in clusters if len(cluster["item_ids"]) == 2)
    nvidia = next(cluster for cluster in clusters if cluster["count"] == 1)
    assert fed["label"].lower() == "fed / cuts"
    assert fed["item_ids"] == ["walter/2", "marketfeed/1"]
    assert fed["channels"] == 2
    assert fed["latest_seen"] == "2026-08-20T12:02:00+00:00"
    assert nvidia["item_ids"] == ["marketfeed/3"]
    assert nvidia["label"] == "Nvidia Record Quarterly Revenue"
    assert cast(int, payload["singletons"]) == 1


def test_cluster_news_separates_different_stories_and_labels_russian() -> None:
    items = [
        _item("marketfeed/1", "Рынок нефти растёт", "2026-08-20T12:00:00+00:00"),
        _item("marketfeed/2", "Bitcoin reaches new high", "2026-08-20T12:01:00+00:00"),
    ]

    payload = cluster_news(items)

    assert len(cast(list[object], payload["clusters"])) == 2
    labels = {cluster["label"] for cluster in cast(list[dict[str, Any]], payload["clusters"])}
    assert "Рынок Нефти Растёт" in labels
    assert any("Bitcoin" in label for label in labels)


def test_cluster_news_ignores_items_without_semantic_text() -> None:
    payload = cluster_news(
        [
            _item("marketfeed/1", "!!!", "2026-08-20T12:00:00+00:00"),
            _item("marketfeed/2", "Fed cuts rates", "2026-08-20T12:01:00+00:00"),
        ]
    )
    clusters = cast(list[dict[str, Any]], payload["clusters"])
    assert len(clusters) == 1
    assert clusters[0]["item_ids"] == ["marketfeed/2"]


def test_news_service_map_payload_caches_until_feed_revision_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = NewsService(["alpha"], cache_seconds=60)
    items = [
        _item("alpha/1", "Fed cuts interest rates", "2026-08-20T12:00:00+00:00"),
    ]
    service._items = {item["id"]: item for item in items}
    calls = 0

    def counted_cluster(values: list[dict[str, Any]]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return cluster_news(values)

    monkeypatch.setattr("app.services.news.cluster_news", counted_cluster)

    assert service.map_payload()["clusters"]
    assert service.map_payload()["clusters"]
    assert calls == 1

    service._items["alpha/2"] = _item(
        "alpha/2", "Bitcoin reaches new high", "2026-08-20T12:02:00+00:00"
    )
    service._map_cache = None
    assert len(cast(list[object], service.map_payload()["clusters"])) == 2
    assert calls == 2
