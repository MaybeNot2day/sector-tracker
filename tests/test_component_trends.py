"""Contract tests for the PCPartPicker component-trends scraper.

The source pages carry a JS gallery of daily-regenerated chart PNGs; the
parser must extract src+title pairs, decode JS unicode escapes in titles
(\\u002D is the hyphen PCPartPicker emits), dedupe repeated srcs (src and
thumb repeat the same URL), and ignore non-trend images.
"""

# ruff: noqa: E501
from __future__ import annotations

import copy
import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import component_trends
from app.services.component_trends import (
    IMAGE_PREFIX,
    normalize_pushed_payload,
    parse_trend_charts,
)

_PUSHER_SPEC = importlib.util.spec_from_file_location(
    "component_trends_pusher",
    Path(__file__).resolve().parents[1] / "scripts" / "component_trends_pusher.py",
)
assert _PUSHER_SPEC is not None and _PUSHER_SPEC.loader is not None
component_trends_pusher = importlib.util.module_from_spec(_PUSHER_SPEC)
_PUSHER_SPEC.loader.exec_module(component_trends_pusher)

PAGE_SNIPPET = """ noqa: E501
        var images = [
                {
                    src: "//cdna.pcpartpicker.com/static/forever/images/trends/2026.08.13.usd.ram.ddr4.3200.2x8192.5d58.png",
                    thumb: "//cdna.pcpartpicker.com/static/forever/images/trends/2026.08.13.usd.ram.ddr4.3200.2x8192.5d58.png",
                    heading: "PCPartPicker Price Trends",
                    title: "DDR4\\u002D3200 2x8GB"
                },
                {
                    src: "//cdna.pcpartpicker.com/static/forever/images/trends/2026.08.13.usd.ram.ddr5.6000.2x32768.b812.png",
                    thumb: "//cdna.pcpartpicker.com/static/forever/images/trends/2026.08.13.usd.ram.ddr5.6000.2x32768.b812.png",
                    heading: "PCPartPicker Price Trends",
                    title: "DDR5\\u002D6000 2x32GB"
                }
        ];
        var logo = { src: "//cdna.pcpartpicker.com/static/forever/img/pcpp-logo.svg", title: "logo" };
"""


def test_parser_extracts_titles_and_absolute_urls() -> None:
    charts = parse_trend_charts(PAGE_SNIPPET)
    assert charts == [
        {
            "title": "DDR4-3200 2x8GB",
            "image": "https://cdna.pcpartpicker.com/static/forever/images/trends/"
            "2026.08.13.usd.ram.ddr4.3200.2x8192.5d58.png",
        },
        {
            "title": "DDR5-6000 2x32GB",
            "image": "https://cdna.pcpartpicker.com/static/forever/images/trends/"
            "2026.08.13.usd.ram.ddr5.6000.2x32768.b812.png",
        },
    ]


def test_parser_survives_pages_without_a_gallery() -> None:
    assert parse_trend_charts("<html><body>maintenance</body></html>") == []


def test_parser_preserves_unicode_and_decodes_escaped_quotes() -> None:
    html = r"""
        var images = [{
            src: "//cdna.pcpartpicker.com/static/forever/images/trends/intel.png",
            title: "Intel® Core \"Ultra\""
        }];
    """
    assert parse_trend_charts(html) == [
        {
            "title": 'Intel® Core "Ultra"',
            "image": "https://cdna.pcpartpicker.com/static/forever/images/trends/intel.png",
        }
    ]


def test_parser_ignores_a_malformed_trailing_escape() -> None:
    html = r"""
        var images = [{
            src: "//cdna.pcpartpicker.com/static/forever/images/trends/broken.png",
            title: "Broken\"
        }];
    """
    assert parse_trend_charts(html) == []


@pytest.mark.asyncio
async def test_image_proxy_rejects_oversize_response_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={
                "content-type": "image/png",
                "content-length": str(component_trends.IMAGE_MAX_BYTES + 1),
            },
            content=b"not-read",
        )
    )

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("app.services.component_trends.httpx.AsyncClient", client_factory)
    component_trends._image_cache.clear()
    src = IMAGE_PREFIX + "oversize.png"
    assert await component_trends.fetch_trend_image(src) is None
    assert src not in component_trends._image_cache


def test_pusher_rejects_insecure_remote_url() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        component_trends_pusher.push("http://board.test", "sekrit", VALID_PUSH)


VALID_PUSH = {
    "categories": [
        {
            "slug": "memory",
            "label": "Memory",
            "url": "https://pcpartpicker.com/trends/price/memory/",
            "charts": [
                {
                    "title": "DDR5-6000 2x32GB",
                    "image": IMAGE_PREFIX + "2026.08.13.usd.ram.ddr5.png",
                }
            ],
        }
    ]
}


def test_normalize_accepts_valid_push_and_stamps_as_of() -> None:
    normalized = normalize_pushed_payload(VALID_PUSH)
    assert normalized is not None
    assert normalized["categories"] == VALID_PUSH["categories"]
    assert str(normalized["as_of"]).endswith("Z")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(categories=[]),
        lambda p: p["categories"][0].update(slug="Bad Slug!"),
        lambda p: p["categories"][0].update(url="https://evil.example/"),
        lambda p: p["categories"][0]["charts"][0].update(image="https://evil.example/x.png"),
        lambda p: p["categories"][0].update(charts=[]),
    ],
)
def test_normalize_rejects_malformed_pushes(mutate: Any) -> None:
    payload = copy.deepcopy(VALID_PUSH)
    mutate(payload)
    assert normalize_pushed_payload(payload) is None


@pytest.fixture
def push_app(tmp_path: Path) -> Iterator[Any]:
    """Token-guarded app settings + a clean component-trends memory cache."""
    had_settings = hasattr(app.state, "settings")
    original = app.state.settings if had_settings else None
    app.state.settings = SimpleNamespace(
        edit_token="sekrit", database_path=tmp_path / "board.sqlite3"
    )
    component_trends._cache.update({"at": 0.0, "payload": None, "failed_at": None})

    yield app.state

    component_trends._cache.update({"at": 0.0, "payload": None, "failed_at": None})
    if had_settings:
        app.state.settings = original
    else:
        del app.state.settings


def test_push_requires_token_and_round_trips_to_get(push_app: Any) -> None:
    client = TestClient(app)
    denied = client.post("/api/component-trends", json=VALID_PUSH)
    assert denied.status_code == 401

    accepted = client.post(
        "/api/component-trends", json=VALID_PUSH, headers={"X-Edit-Token": "sekrit"}
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"status": "ok", "categories": 1}

    served = client.get("/api/component-trends").json()
    assert served["categories"] == VALID_PUSH["categories"]


def test_push_rejects_malformed_payload(push_app: Any) -> None:
    response = TestClient(app).post(
        "/api/component-trends",
        json={"categories": "nope"},
        headers={"X-Edit-Token": "sekrit"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_failed_scrape_round_is_negatively_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    component_trends._cache.update({"at": 0.0, "payload": None, "failed_at": None})

    async def fail_category(client: object, slug: str, label: str) -> None:
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(component_trends, "_fetch_category", fail_category)

    first = await component_trends.component_trends_payload()
    second = await component_trends.component_trends_payload()

    assert first["categories"] == second["categories"] == []
    assert calls == len(component_trends.CATEGORIES)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_source", ["memory", "pushed"])
async def test_partial_scrape_keeps_failed_categories_and_retries_soon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fallback_source: str
) -> None:
    now = 100_000.0
    as_of = "2026-10-07T12:00:00Z"
    cpu_healthy = False
    requested: list[str] = []
    previous = {
        "as_of": "2000-01-01T00:00:00Z",
        "source": "https://pcpartpicker.com/trends/",
        "categories": [
            {
                "slug": slug,
                "label": label,
                "url": f"https://pcpartpicker.com/trends/price/{slug}/",
                "charts": [{"title": f"Old {label}", "image": IMAGE_PREFIX + f"old-{slug}.png"}],
            }
            for slug, label in component_trends.CATEGORIES
        ],
    }
    monkeypatch.setattr(component_trends, "monotonic", lambda: now)
    monkeypatch.setattr(component_trends, "_now_iso", lambda: as_of)
    monkeypatch.setattr(
        component_trends,
        "_cache",
        {"at": 0.0, "payload": previous if fallback_source == "memory" else None, "failed_at": None},
    )
    store_path = tmp_path / "trends.json" if fallback_source == "pushed" else None
    if store_path is not None:
        store_path.write_text(json.dumps(previous), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        slug = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        requested.append(slug)
        if slug == "cpu" and not cpu_healthy:
            return httpx.Response(503)
        return httpx.Response(
            200,
            text=(
                'var images = [{ src: "//cdna.pcpartpicker.com/static/forever/images/trends/'
                f'fresh-{slug}.png", title: "Fresh {slug}" }}];'
            ),
        )

    original_client = httpx.AsyncClient

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(component_trends.httpx, "AsyncClient", client_factory)
    partial = await component_trends.component_trends_payload(store_path)
    categories = {category["slug"]: category for category in partial["categories"]}
    assert list(categories) == [slug for slug, _label in component_trends.CATEGORIES]
    assert categories["cpu"]["charts"] == previous["categories"][1]["charts"]
    assert categories["cpu"]["as_of"] == previous["as_of"]
    assert categories["cpu"]["stale"] is True
    assert categories["memory"]["charts"][0]["image"] == IMAGE_PREFIX + "fresh-memory.png"
    assert categories["memory"]["as_of"] == as_of
    assert categories["memory"]["stale"] is False
    assert partial["stale"] is True
    assert partial["failed_categories"] == ["cpu"]
    assert partial["as_of"] == previous["as_of"]
    assert await component_trends.component_trends_payload(store_path) == partial
    assert len(requested) == len(component_trends.CATEGORIES)

    now += component_trends.FAILURE_RETRY_SECONDS + 1
    as_of = "2026-10-07T12:05:01Z"
    cpu_healthy = True
    recovered = await component_trends.component_trends_payload(store_path)
    assert len(requested) == 2 * len(component_trends.CATEGORIES)
    assert recovered["stale"] is False
    assert recovered["failed_categories"] == []
    assert recovered["as_of"] == as_of
    assert all(not category["stale"] for category in recovered["categories"])
    assert await component_trends.component_trends_payload(store_path) == recovered
    assert len(requested) == 2 * len(component_trends.CATEGORIES)


@pytest.mark.asyncio
async def test_full_scrape_failure_marks_existing_charts_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = {
        "as_of": "2000-01-01T00:00:00Z",
        "source": "https://pcpartpicker.com/trends/",
        "categories": copy.deepcopy(VALID_PUSH["categories"]),
    }
    monkeypatch.setattr(
        component_trends, "_cache", {"at": 0.0, "payload": previous, "failed_at": None}
    )
    monkeypatch.setattr(component_trends, "monotonic", lambda: 100_000.0)

    async def fail_category(client: object, slug: str, label: str) -> None:
        return None

    monkeypatch.setattr(component_trends, "_fetch_category", fail_category)
    stale = await component_trends.component_trends_payload()
    assert stale["as_of"] == previous["as_of"]
    assert stale["stale"] is True
    assert stale["categories"][0]["charts"] == previous["categories"][0]["charts"]
    assert stale["categories"][0]["stale"] is True
    assert "stale" not in previous["categories"][0]
