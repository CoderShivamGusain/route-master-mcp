"""Rank bus listings that the AI host or the user already has in front of them.

The server never fetches listings itself (booking sites forbid scraping). A host
with a browser, or a user pasting results, passes what they see; we score each
bus for trust and fit, and attach the dated search link plus a "find" hint,
because the public sites have no per-bus URL (the seat map opens in-page).

Each bus gets:
  * a fit level against the bus-type preference (0 exact, 1 close substitute
    such as a semi-sleeper for a sleeper request, 2 type unknown, 3 mismatch);
  * a score out of 100: 55% trust (`trust.operators.assess`), 20% fit, 15% value
    (cheapest fare / this fare), 10% seats left (capped at 10), minus a penalty
    for buses leaving within `TIGHT_MINUTES` and, with `around_time`, for
    distance from the requested time;
  * short notes explaining anything the user should know.
Sorting is always by fit level first, so a better-scored mismatch never sits
above a matching bus; the chosen `sort_by` orders buses within each level.
Buses that leave today within `MISSED_MINUTES` (or already left) are returned
separately, not ranked.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from mcp_route_master.places.dates import IST
from mcp_route_master.places.links import BookingLink
from mcp_route_master.trust.bus_types import BusPreference, BusType, classify, matches
from mcp_route_master.trust.operators import Assessment, assess

SortBy = Literal["best", "earliest", "cheapest", "fastest"]
# "9:05", "23.58", "11:58 PM", "11:58 p.m." (hour, minute, optional am/pm).
_TIME = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*([ap]\.?m\.?)?\s*$", re.I)
TIGHT_MINUTES = 30  # leaving sooner than this: only catchable if already near the stop
MISSED_MINUTES = 5  # leaving in under 5 minutes: nobody can still book and board it
MAX_SHOWN = 10


class TimeFormatError(ValueError):
    """Raised when a departure/arrival time can't be read."""


def parse_clock(text: str) -> int:
    """'23:58', '9:05', '11:58 PM' -> minutes after midnight."""
    m = _TIME.match(text or "")
    if not m:
        raise TimeFormatError(f"Can't read time '{text}'. Use HH:MM, e.g. 23:58.")
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower().replace(".", "")
    if ampm:
        # "00:50 AM" (a 24-hour time with AM, as some apps write it) means 00:50.
        if not (1 <= hour <= 12 or (hour == 0 and ampm == "am")):
            raise TimeFormatError(f"Can't read time '{text}'.")
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        raise TimeFormatError(f"Can't read time '{text}'.")
    return hour * 60 + minute


def fmt_clock(minutes: int) -> str:
    """Minutes after midnight -> 'HH:MM'."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Listing:
    """One bus as shown on a results page. Only operator and departure are required."""

    operator: str
    departure: str
    arrival: str | None = None
    bus_type: str | None = None
    fare: float | None = None
    rating: float | None = None
    review_count: int | None = None
    seats_left: int | None = None
    has_live_tracking: bool | None = None
    boarding_point: str | None = None
    platform: str | None = None
    booking_url: str | None = None  # a verified link to this specific bus, when the site has one


@dataclass
class Ranked:
    """A listing with its parsed times, trust assessment, fit, score and booking link.

    `minutes_to_go` is set only when the bus runs today.
    """

    listing: Listing
    depart_min: int
    duration_min: int | None
    assessment: Assessment
    fit: bool | None
    score: float
    link: BookingLink
    notes: list[str] = field(default_factory=list)
    minutes_to_go: int | None = None
    level: int = 0  # 0 matches, 1 close substitute, 2 unknown type, 3 mismatch

    @property
    def direct_url(self) -> str | None:
        """Per-bus URL supplied by the caller, if any."""
        return self.listing.booking_url

    @property
    def tight(self) -> bool:
        """Leaves so soon that only someone already at the stop can catch it."""
        return self.minutes_to_go is not None and self.minutes_to_go < TIGHT_MINUTES


_SLEEPER_PREFS = {"sleeper", "ac_sleeper", "non_ac_sleeper"}


def _fit_level(fit: bool | None, bt: BusType | None, preference: BusPreference) -> int:
    """Map a bus to the fit levels described in the module docstring (0 best, 3 worst)."""
    if fit is True:
        return 0
    if bt is None or fit is None:
        return 2
    # No bed, but a reclining seat with the right AC setting is the closest thing.
    ac_ok = (
        preference == "sleeper"
        or (preference == "ac_sleeper" and bt.ac is not False)
        or (preference == "non_ac_sleeper" and bt.ac is not True)
    )
    if preference in _SLEEPER_PREFS and bt.berth == "semi_sleeper" and ac_ok:
        return 1
    return 3


def _pick_link(platform: str | None, links: list[BookingLink]) -> BookingLink:
    """The platform the bus was seen on, if we have a dated link for it; else the best dated link."""
    if platform:
        wanted = platform.strip().lower().replace(" ", "")
        for link in links:
            if link.platform.lower().replace(" ", "") == wanted and link.kind != "home":
                return link
    return links[0]


def rank(
    listings: list[Listing],
    travel_date: date,
    links: list[BookingLink],
    preference: BusPreference = "any",
    sort_by: SortBy = "best",
    is_known_terminal: Callable[[str], bool] | None = None,
    now: datetime | None = None,
    around_time: str | None = None,
) -> tuple[list[Ranked], list[Listing], list[str]]:
    """Return (ranked buses, buses that already left, input problems).

    `around_time` ("09:00") favours buses departing near the time the traveller asked for.
    """
    target = parse_clock(around_time) if around_time else None
    now = now or datetime.now(IST)
    is_today = travel_date == now.date()
    now_min = now.hour * 60 + now.minute
    ranked: list[Ranked] = []
    departed: list[Listing] = []
    problems: list[str] = []
    fares = [b.fare for b in listings if b.fare and b.fare > 0]
    cheapest = min(fares) if fares else None

    for bus in listings:
        try:
            dep = parse_clock(bus.departure)
            dur = None
            if bus.arrival:
                # Modulo a day handles overnight buses (23:00 -> 06:00 is 7 h). This assumes the
                # trip is under 24 h; an arrival equal to departure gives no duration.
                dur = (parse_clock(bus.arrival) - dep) % (24 * 60) or None
        except TimeFormatError as exc:
            problems.append(f"{bus.operator}: {exc}")
            continue
        try:
            a = assess(
                bus.operator,
                bus.rating,
                bus.review_count,
                bus.has_live_tracking,
                bus.boarding_point,
                bus.bus_type,
                is_known_terminal=is_known_terminal,
            )
        except ValueError as exc:
            problems.append(f"{bus.operator}: {exc}")
            continue

        to_go = dep - now_min if is_today else None
        if to_go is not None and to_go < MISSED_MINUTES:
            departed.append(bus)  # already left, or leaving right now
            continue

        bt = classify(bus.bus_type) if bus.bus_type else None
        fit = matches(bt, preference) if bt else (True if preference == "any" else None)
        level = _fit_level(fit, bt, preference)
        notes: list[str] = []
        if level == 3 and preference != "any":
            notes.append(f"not {preference.replace('_', ' ')}")
        if bt and bt.berth == "semi_sleeper":
            notes.append("semi-sleeper (reclining seat)")
        if bt and bt.berth == "seater_sleeper" and preference in _SLEEPER_PREFS:
            notes.append("seats + berths: pick a berth (berths cost more than this fare)")
        if a.lookalike_of:
            notes.append("name copies a government operator")
        if bus.review_count is not None and bus.review_count < 100:
            notes.append(f"only {bus.review_count} reviews" if bus.review_count else "no reviews")
        if bus.rating is not None and bus.rating < 3.5:
            notes.append("low rating")
        if any("highway" in w for w in a.warnings):
            notes.append("highway boarding point")
        if bus.seats_left is not None and bus.seats_left <= 3:
            notes.append(f"only {bus.seats_left} seats left")
        if to_go is not None and to_go < TIGHT_MINUTES:
            notes.append(f"leaves in {to_go} min")

        # Each component is on a 0-100 scale; missing data gets a neutral-ish middle value.
        trust = a.score if a.score is not None else 45  # unknown trust: just below neutral
        fit_points = {0: 100, 1: 60, 2: 50, 3: 0}[level]
        value = 100 * cheapest / bus.fare if cheapest and bus.fare else 50  # cheapest bus = 100
        seats = 50 if bus.seats_left is None else min(bus.seats_left, 10) * 10
        score = 0.55 * trust + 0.2 * fit_points + 0.15 * value + 0.1 * seats
        if to_go is not None and to_go < TIGHT_MINUTES:
            score -= 15  # very hard to catch
        if target is not None:
            gap = abs(dep - target)
            gap = min(gap, 24 * 60 - gap)  # around the clock: 23:30 is 1 h from 00:30
            # Nearby buses gain; beyond 3 hours every bus is equally "not near", so trust and
            # comfort decide (otherwise a weak 23:00 bus beats a better 21:30 one for "9 am").
            score -= 3 * min(gap, 180) / 60
            # Note gaps of 1.5 to 6 hours ("2h after your time"); smaller gaps don't need a note.
            if 90 <= gap < 360:
                hours = round(gap / 60)
                notes.append(f"{hours}h {'after' if (dep - target) % 1440 < 720 else 'before'} your time")
        ranked.append(
            Ranked(
                bus, dep, dur, a, fit, round(score, 1), _pick_link(bus.platform, links), notes, to_go, level
            )
        )

    # Exact matches first, then close substitutes (a semi-sleeper when a sleeper was asked for),
    # then listings with no bus type, then clear mismatches (a 3+2 seater for a sleeper request).
    def fit_rank(r: Ranked) -> int:
        return r.level

    keys: dict[str, Callable[[Ranked], tuple]] = {
        "best": lambda r: (fit_rank(r), -r.score, r.depart_min),
        # Earliest bus you can realistically still catch comes first.
        "earliest": lambda r: (fit_rank(r), r.tight, r.depart_min, -r.score),
        "cheapest": lambda r: (fit_rank(r), r.listing.fare or float("inf"), -r.score),
        "fastest": lambda r: (fit_rank(r), r.duration_min or 10**6, -r.score),
    }
    ranked.sort(key=keys[sort_by])
    return ranked, departed, problems


def describe_duration(minutes: int | None) -> str:
    """425 -> '7h 05m'; empty string when unknown."""
    if not minutes:
        return ""
    return f"{minutes // 60}h {minutes % 60:02d}m"


def arrival_text(r: Ranked) -> str:
    """Arrival as 'HH:MM', with ' +1' when it falls on the next day."""
    if r.duration_min is None:
        return r.listing.arrival or "?"
    arr = (r.depart_min + r.duration_min) % (24 * 60)
    next_day = r.depart_min + r.duration_min >= 24 * 60
    return fmt_clock(arr) + (" +1" if next_day else "")
