"""Stdlib-only Fringe grammar shared by ledger ingest and standalone upload validation."""

from __future__ import annotations

import re
from collections.abc import Iterator

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SECTION_TITLE = re.compile(r"fringe", re.IGNORECASE)
_BULLET = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}\.)\s+(.*\S)\s*$")
_ACTION = re.compile(
    r"^(?i:(?P<action>OPEN|HOLD|CLOSE))\s+(?i:(?P<direction>LONG|SHORT))\s+"
    r"(?P<ticker>[A-Z0-9.\-=]{1,15})"
    r"(?:\s*[—–:]\s*|\s+-+\s+|\s*$)"
    r"(?P<text>.*)$"
)


def has_fringe_section(body: str) -> bool:
    return any(
        _SECTION_TITLE.search(heading.group(2)) is not None
        for line in body.splitlines()
        if (heading := _HEADING.match(line)) is not None
    )


def iter_fringe_actions(body: str) -> Iterator[tuple[str, str, str, str]]:
    """Yield action, direction, ticker and raw trailing text in report order."""
    in_section = False
    section_level = 0
    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            level, text = len(heading.group(1)), heading.group(2)
            if in_section and level > section_level:
                continue
            in_section = _SECTION_TITLE.search(text) is not None
            if in_section:
                section_level = level
            continue
        if not in_section:
            continue
        bullet = _BULLET.match(line)
        if bullet is None:
            continue
        match = _ACTION.match(bullet.group(1))
        if match is not None:
            yield (
                match.group("action").lower(),
                match.group("direction").lower(),
                match.group("ticker"),
                match.group("text"),
            )
