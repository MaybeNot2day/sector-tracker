"""Extract structured key dates from agent-written markdown reports.

Agent briefs may carry a ``## Key Dates`` section listing upcoming market
events, one bullet per line:

    ## Key Dates

    - 2026-07-15 08:30 ET — PPI — Producer Price Index (June) [MACRO]
    - 2026-07-17 — US monthly options expiration (opex) [OPEX]
    - 2026-07-22 AMC — TSLA earnings [EARNINGS]
    - 2026-07-16 — ARB unlock — 92.6M ARB (1.4% of circ supply) [CRYPTO]

Grammar per bullet: ISO date (optionally ``**bold**``), an optional time
(``HH:MM`` plus an optional timezone word, or a session token like AMC/BMO),
a dash/colon separator, the title, and an optional trailing ``[CATEGORY]``
tag. Non-matching lines are prose and skipped silently — the parser must
never reject a whole report over one malformed bullet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# Categories mirror the terminal calendars agents crib from; anything else
# is kept verbatim (uppercased) so a new tag renders without a code change.
DEFAULT_CATEGORY = "EVENT"
MAX_EVENTS = 100
_MAX_TITLE = 200
_MAX_TIME = 16
_MAX_CATEGORY = 16

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SECTION_TITLE = re.compile(r"key\s+dates", re.IGNORECASE)
_BULLET = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}\.)\s+(.*\S)\s*$")
_EVENT = re.compile(
    r"""
    ^\*{0,2}(?P<date>\d{4}-\d{2}-\d{2})\*{0,2}          # 2026-07-15, or **2026-07-15**
    (?:\s+(?P<time>
        \d{1,2}:\d{2}(?:\s?[A-Za-z]{2,4})?              # 08:30, 22:00 ET, 9:45CET
        | AMC | BMO                                     # session tokens
    ))?
    \s*(?:[\u2014\u2013:]|-)\s+                         # — – - : separator
    (?P<title>.+?)
    (?:\s*[\[(](?P<category>[A-Za-z][A-Za-z0-9 &/-]{0,15})[\])])?  # [MACRO]
    $
    """,
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class KeyDate:
    date: str
    time: str | None
    title: str
    category: str


def parse_key_dates(body: str) -> list[KeyDate]:
    """All valid key-date bullets under ``Key Dates`` headings, capped.

    Duplicate (date, title) bullets within one report collapse to the first
    occurrence; agents that echo an event in two sections must not create
    two calendar rows.
    """
    events: list[KeyDate] = []
    seen: set[tuple[str, str]] = set()
    for line in _section_lines(body):
        bullet = _BULLET.match(line)
        if bullet is None:
            continue
        event = _parse_bullet(bullet.group(1))
        if event is None:
            continue
        key = (event.date, event.title.casefold())
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
        if len(events) >= MAX_EVENTS:
            break
    return events


def _section_lines(body: str) -> list[str]:
    """Lines belonging to every ``Key Dates`` section, headings excluded."""
    lines: list[str] = []
    in_section = False
    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            in_section = _SECTION_TITLE.search(heading.group(2)) is not None
            continue
        if in_section:
            lines.append(line)
    return lines


def _parse_bullet(text: str) -> KeyDate | None:
    match = _EVENT.match(text)
    if match is None:
        return None
    date_text = match.group("date")
    try:
        date.fromisoformat(date_text)
    except ValueError:
        # The regex admits non-calendar dates like 2026-02-31; drop them.
        return None
    title = " ".join(match.group("title").split())[:_MAX_TITLE].rstrip()
    if not title:
        return None
    time_text = match.group("time")
    category = match.group("category")
    normalized_category = (
        " ".join(category.upper().split())[:_MAX_CATEGORY] if category else DEFAULT_CATEGORY
    )
    return KeyDate(
        date=date_text,
        time=" ".join(time_text.split())[:_MAX_TIME] if time_text else None,
        title=title,
        category=normalized_category,
    )
