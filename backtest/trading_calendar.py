"""Which days the market was open.

Uses the ``exchange_calendars`` package rather than "weekdays minus a hardcoded
holiday list", because the hardcoded version is wrong every time an exchange
closes unexpectedly — Hurricane Sandy in 2012, the national day of mourning in
December 2018 — and PRD §8.5 requires holidays to be simulated correctly.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import exchange_calendars as xcals


@lru_cache(maxsize=4)
def _calendar(name: str):
    return xcals.get_calendar(name)


def sessions(start: date, end: date, calendar: str = "XNYS") -> list[date]:
    """Trading sessions in ``[start, end]``, inclusive, in order."""
    cal = _calendar(calendar)
    index = cal.sessions_in_range(str(start), str(end))
    return [ts.date() for ts in index]


def is_session(day: date, calendar: str = "XNYS") -> bool:
    return _calendar(calendar).is_session(str(day))


def next_session(day: date, calendar: str = "XNYS") -> date:
    """The next trading day strictly after ``day``.

    This is what "market-on-open the day after a trigger fires" (§7.1) means on
    a Friday, and what it means the day before Thanksgiving.
    """
    return _calendar(calendar).next_session(str(day)).date()


def previous_session(day: date, calendar: str = "XNYS") -> date:
    return _calendar(calendar).previous_session(str(day)).date()


def sessions_between(start: date, end: date, calendar: str = "XNYS") -> int:
    """Count of trading sessions in the window — the unit §5.1 and §5.2 use."""
    return len(sessions(start, end, calendar))
