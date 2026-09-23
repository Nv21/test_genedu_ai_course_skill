"""Date parsing helpers for the Wikimedia pageviews REST API.

The API takes dates as an 8-digit ``YYYYMMDD`` string. Users (and the agent
relaying user requests) will naturally write "2024-03", "2024-03-15" or
"last two years" style ranges, so this module normalizes the common forms
and expands relative ranges.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

_YYYY_MM_DD = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_YYYY_MM = re.compile(r"^(\d{4})-(\d{2})$")
_YYYYMMDD = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def parse_boundary(value: str, *, is_end: bool) -> date:
    """Parse a user-supplied date string into a `date`.

    - "YYYY-MM-DD" -> that exact day.
    - "YYYY-MM"    -> the 1st of the month (start) or last day (end).
    - "YYYYMMDD"   -> passthrough.
    """
    value = value.strip()
    m = _YYYY_MM_DD.match(value)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return date(y, mo, d)
    m = _YYYY_MM.match(value)
    if m:
        y, mo = (int(g) for g in m.groups())
        if is_end:
            last_day = calendar.monthrange(y, mo)[1]
            return date(y, mo, last_day)
        return date(y, mo, 1)
    m = _YYYYMMDD.match(value)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return date(y, mo, d)
    raise ValueError(
        f"Unrecognized date '{value}'. Use YYYY-MM-DD or YYYY-MM."
    )


def to_api_str(d: date) -> str:
    return d.strftime("%Y%m%d")


def today_minus_years(years: float) -> date:
    return date.today() - timedelta(days=int(round(years * 365.25)))
