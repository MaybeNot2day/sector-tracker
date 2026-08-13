"""Contract tests for the PCPartPicker component-trends scraper.

The source pages carry a JS gallery of daily-regenerated chart PNGs; the
parser must extract src+title pairs, decode JS unicode escapes in titles
(\\u002D is the hyphen PCPartPicker emits), dedupe repeated srcs (src and
thumb repeat the same URL), and ignore non-trend images.
"""

# ruff: noqa: E501
from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import component_trends
from app.services.component_trends import (
    IMAGE_PREFIX,
    normalize_pushed_payload,
    parse_trend_charts,
)

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
    component_trends._cache.update({"at": 0.0, "payload": None})

    yield app.state

    component_trends._cache.update({"at": 0.0, "payload": None})
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
