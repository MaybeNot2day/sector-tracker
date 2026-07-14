"""Extract structured key dates from agent-written markdown reports.

Hermes briefs already carry economic calendars; this parser reads them
as-is. Any section whose heading mentions "calendar" or "key dates" is
scanned — e.g. ``## Economic Calendar (CEST)`` or ``### Today's Calendar
— CET`` — and two shapes feed the board:

Markdown tables (the Hermes cron format), first column when, second what::

    | Time (CEST) | Event | Forecast | Implication |
    |---|---|---|---|
    | **14:30** | US June CPI (M-o-M) | -0.1% | ... |
    | 11:00 Wed | Euro Area industrial production | -0.6% | ... |
    | **Thu Jul 16 14:30** | US June Retail Sales | +0.3% | ... |

The when-cell may carry a bare time, a weekday, a month-day, an ISO date,
or free text with any of those embedded. Dates resolve against the report
date: a ``### Tuesday, July 14, 2026`` subheading pins following rows,
weekdays roll forward to the next occurrence, and bare times mean the
report's own day. A timezone token in the section heading (CEST, ET, ...)
is appended to bare times so the board shows the zone the agent wrote.

Explicit bullets (any agent, no table required)::

    - 2026-07-22 AMC — TSLA earnings [EARNINGS]
    - 2026-07-17 — US monthly options expiration (opex) [OPEX]

Without a ``[CATEGORY]`` tag the title infers one (earnings / opex /
holiday / unlock); tables default to MACRO, bullets to EVENT. Prose and
malformed rows are skipped silently — the parser must never reject a
whole report over one odd line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

DEFAULT_CATEGORY = "EVENT"
TABLE_CATEGORY = "MACRO"
MAX_EVENTS = 100
_MAX_TITLE = 200
_MAX_TIME = 16
_MAX_CATEGORY = 16

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SECTION_TITLE = re.compile(r"key\s+dates|calendar", re.IGNORECASE)
_ZONE = re.compile(r"\b(CEST|CET|EST|EDT|ET|UTC|GMT|BST|JST)\b")
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
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_TIME = re.compile(r"\b(\d{1,2}:\d{2})\b")
_YEAR = re.compile(r"\b(20\d{2})\b")
_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b",
    re.IGNORECASE,
)
_MONTH_NUMBERS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
# Exact token alternatives so "Monetary"/"Monthly" never read as Monday.
_WEEKDAY = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|tues|thurs|thur|weds|mon|tue|wed|thu|fri|sat|sun)\b",
    re.IGNORECASE,
)
_WEEKDAY_NUMBERS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_TABLE_SEPARATOR = re.compile(r"^[-: ]+$")
_MD_MARKS = str.maketrans({"*": None, "`": None})
_CATEGORY_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bearnings\b", re.IGNORECASE), "EARNINGS"),
    (re.compile(r"\bopex\b|options?\s+expiration", re.IGNORECASE), "OPEX"),
    (re.compile(r"\bholiday\b|\bclosed\b", re.IGNORECASE), "HOLIDAY"),
    (re.compile(r"\bunlock\b|\bairdrop\b", re.IGNORECASE), "CRYPTO"),
)


@dataclass(frozen=True, slots=True)
class KeyDate:
    date: str
    time: str | None
    title: str
    category: str


def parse_key_dates(body: str, *, default_date: str | None = None) -> list[KeyDate]:
    """All valid key-date rows under calendar/key-dates headings, capped.

    ``default_date`` (the report's date) anchors relative dates; rows that
    need an anchor are dropped without one. Duplicate (date, title) rows
    collapse to the first occurrence.
    """
    anchor = _parse_iso(default_date)
    events: list[KeyDate] = []
    seen: set[tuple[str, str]] = set()
    in_section = False
    section_level = 0
    zone: str | None = None
    current = anchor
    lines = body.splitlines()
    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        if heading is not None:
            level, text = len(heading.group(1)), heading.group(2)
            if in_section and level > section_level:
                # Subheading inside the section (e.g. "Tuesday, July 14,
                # 2026") pins the date for the rows that follow it.
                current = _heading_date(text, anchor) or current
                continue
            in_section = _SECTION_TITLE.search(text) is not None
            if in_section:
                section_level = level
                zone_match = _ZONE.search(text)
                zone = zone_match.group(1) if zone_match else None
                current = anchor
            continue
        if not in_section:
            continue
        # A table header is the row right above the |---|---| separator;
        # detecting it structurally keeps when-cells containing words like
        # "time" ("exact time not verified") parseable as data.
        if (
            line.lstrip().startswith("|")
            and index + 1 < len(lines)
            and _is_table_separator(lines[index + 1])
        ):
            continue
        event = _parse_section_line(line, current, zone)
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


def _parse_section_line(line: str, current: date | None, zone: str | None) -> KeyDate | None:
    bullet = _BULLET.match(line)
    if bullet is not None:
        return _parse_bullet(bullet.group(1))
    return _parse_table_row(line, current, zone)


def _parse_bullet(text: str) -> KeyDate | None:
    match = _EVENT.match(text)
    if match is None:
        return None
    date_text = match.group("date")
    if _parse_iso(date_text) is None:
        # The regex admits non-calendar dates like 2026-02-31; drop them.
        return None
    title = _clean_title(match.group("title"))
    if not title:
        return None
    time_text = match.group("time")
    category = match.group("category")
    normalized_category = (
        " ".join(category.upper().split())[:_MAX_CATEGORY]
        if category
        else _infer_category(title, DEFAULT_CATEGORY)
    )
    return KeyDate(
        date=date_text,
        time=" ".join(time_text.split())[:_MAX_TIME] if time_text else None,
        title=title,
        category=normalized_category,
    )


def _is_table_separator(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return all(_TABLE_SEPARATOR.fullmatch(cell) for cell in cells)


def _parse_table_row(line: str, current: date | None, zone: str | None) -> KeyDate | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if len(cells) < 2:
        return None
    if all(_TABLE_SEPARATOR.fullmatch(cell) for cell in cells):
        return None
    when, what = cells[0], cells[1]
    if what.lower() in {"event", "events"}:
        return None  # header row of a separator-less (malformed) table
    title = _clean_title(what)
    if not title or not title.strip("-\u2013\u2014 "):
        return None  # placeholder rows like "—"
    event_date = _cell_date(when, current)
    if event_date is None:
        return None
    time_match = _TIME.search(when)
    time_text: str | None = None
    if time_match is not None:
        time_text = f"{time_match.group(1)} {zone}" if zone else time_match.group(1)
    return KeyDate(
        date=event_date.isoformat(),
        time=time_text[:_MAX_TIME] if time_text else None,
        title=title,
        category=_infer_category(title, TABLE_CATEGORY),
    )


def _cell_date(text: str, current: date | None) -> date | None:
    """Resolve a when-cell to a calendar date against the running anchor.

    Precedence: ISO date, then month-day (Hermes writes "Wed Jul 16" with
    the weekday occasionally wrong — the explicit date wins), then weekday
    rolled forward from the anchor, then the anchor itself.
    """
    iso = _ISO_DATE.search(text)
    if iso is not None:
        return _parse_iso(iso.group(1))
    month_day = _MONTH_DAY.search(text)
    if month_day is not None:
        if current is None:
            return None
        month = _MONTH_NUMBERS[month_day.group(1).lower()[:3]]
        try:
            candidate = date(current.year, month, int(month_day.group(2)))
            # A calendar looks days ahead, never seasons back: a date far in
            # the anchor's past is next year's (December brief listing January).
            if (current - candidate).days > 90:
                candidate = candidate.replace(year=current.year + 1)
        except ValueError:
            # Non-calendar day, or Feb 29 rolled into a non-leap year.
            return None
        return candidate
    weekday = _WEEKDAY.search(text)
    if weekday is not None:
        if current is None:
            return None
        target = _WEEKDAY_NUMBERS[weekday.group(1).lower()[:3]]
        return current + timedelta(days=(target - current.weekday()) % 7)
    return current


def _heading_date(text: str, anchor: date | None) -> date | None:
    """Date pinned by a section subheading like "Tuesday, July 14, 2026"."""
    iso = _ISO_DATE.search(text)
    if iso is not None:
        return _parse_iso(iso.group(1))
    month_day = _MONTH_DAY.search(text)
    if month_day is None:
        return None
    year_match = _YEAR.search(text)
    month = _MONTH_NUMBERS[month_day.group(1).lower()[:3]]
    try:
        if year_match is not None:
            return date(int(year_match.group(1)), month, int(month_day.group(2)))
        if anchor is None:
            return None
        pinned = date(anchor.year, month, int(month_day.group(2)))
        # Same forward-looking rule as when-cells: a yearless heading months
        # in the anchor's past is next year's (December brief pinning January).
        return pinned.replace(year=anchor.year + 1) if (anchor - pinned).days > 90 else pinned
    except ValueError:
        # Non-calendar day, or Feb 29 rolled into a non-leap year.
        return None


def _clean_title(text: str) -> str:
    without_links = _LINK.sub(r"\1", text)
    plain = without_links.translate(_MD_MARKS)
    return " ".join(plain.split())[:_MAX_TITLE].rstrip()


def _infer_category(title: str, fallback: str) -> str:
    for pattern, category in _CATEGORY_HINTS:
        if pattern.search(title):
            return category
    return fallback


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
