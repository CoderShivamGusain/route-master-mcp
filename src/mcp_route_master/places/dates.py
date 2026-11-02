"""Travel-date parsing and the date formats each booking site expects.

All "today" logic uses India Standard Time, not the server's clock: the server
may run anywhere (or in UTC), but buses depart on Indian dates, and near
midnight the two can differ by a day. Dates are validated up front (not in the
past, not beyond what sites sell) so a bad date fails with a clear message
instead of producing a link that opens an empty search.

The per-site formatters exist because each platform encodes the date
differently in its URL (see `places/links.py`).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
MAX_ADVANCE_DAYS = 180  # generous upper bound on how far ahead Indian bus sites sell tickets
# Hard-coded English month names so formatting doesn't depend on the system locale.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_RELATIVE = {"today": 0, "tonight": 0, "tomorrow": 1, "tmrw": 1, "day after tomorrow": 2}


class DateError(ValueError):
    """Raised when a travel date is missing, malformed or out of range."""


def today_ist() -> date:
    """Today's date in India, whatever timezone the server runs in."""
    return datetime.now(IST).date()


def parse_travel_date(value: str, today: date | None = None) -> date:
    """Accept YYYY-MM-DD (preferred), DD-MM-YYYY, DD/MM/YYYY, 'today' or 'tomorrow'.

    Day-first is assumed for numeric dates, as is usual in India; month-first
    (US style) is never assumed. Raises `DateError` for past dates or dates more
    than `MAX_ADVANCE_DAYS` ahead.
    """
    today = today or today_ist()
    text = re.sub(r"\s+", " ", (value or "").strip().lower())
    if not text:
        raise DateError("travel_date is required, e.g. 2026-11-02.")

    if text in _RELATIVE:
        parsed = today + timedelta(days=_RELATIVE[text])
    else:
        parsed = None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None:
            raise DateError(
                f"Could not read travel_date '{value}'. Use YYYY-MM-DD (for example "
                f"{(today + timedelta(days=1)).isoformat()})."
            )

    if parsed < today:
        raise DateError(f"{parsed.isoformat()} is in the past (today in India is {today.isoformat()}).")
    if parsed > today + timedelta(days=MAX_ADVANCE_DAYS):
        raise DateError(
            f"{parsed.isoformat()} is more than {MAX_ADVANCE_DAYS} days away; "
            "bus sites don't sell tickets that far ahead."
        )
    return parsed


def redbus_date(d: date) -> str:
    """redBus uses 31-Oct-2026 (locale-independent)."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def mmt_date(d: date) -> str:
    """MakeMyTrip uses 31-10-2026."""
    return d.strftime("%d-%m-%Y")


def goibibo_date(d: date) -> str:
    """Goibibo uses 20261031."""
    return d.strftime("%Y%m%d")


def human_date(d: date) -> str:
    """'Sat, 31 Oct 2026'."""
    weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[d.weekday()]
    return f"{weekday}, {d.day} {_MONTHS[d.month - 1]} {d.year}"
