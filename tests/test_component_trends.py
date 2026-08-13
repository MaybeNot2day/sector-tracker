"""Contract tests for the PCPartPicker component-trends scraper.

The source pages carry a JS gallery of daily-regenerated chart PNGs; the
parser must extract src+title pairs, decode JS unicode escapes in titles
(\\u002D is the hyphen PCPartPicker emits), dedupe repeated srcs (src and
thumb repeat the same URL), and ignore non-trend images.
"""

# ruff: noqa: E501
from __future__ import annotations

from app.services.component_trends import parse_trend_charts

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
