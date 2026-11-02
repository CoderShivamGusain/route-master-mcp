"""Pre-payment check of a booking the AI host filled in on the official site.

Nothing here books, pays or stores anything. The host passes what the checkout
page shows (and what the user asked for); we check it, flag problems, and return
what the user should see before they reply "confirm" and the host opens payment.

Used by the booking_summary tool (step "Call booking_summary" in `booking/guide.py`).
`check_draft` splits findings into:
  * problems - block payment: an unofficial checkout host, a past date or
    departed bus, seats that differ from what the user asked for, seat and
    passenger mismatches, invalid names/ages/genders, a gender-reserved seat
    given to the wrong passenger, an invalid mobile or email;
  * warnings - shown but not blocking: likely sample contact details, missing
    boarding point, children under 5, or a total that doesn't add up (often an
    add-on the user never chose).
The checkout URL is judged by `safety.check` on scheme + host only, so a real
page can't be failed by text in its query string, and a lookalike host can't
pass.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urlsplit

from mcp_route_master.booking.ranking import TimeFormatError, parse_clock
from mcp_route_master.trust.safety import check

_MOBILE = re.compile(r"(?:\+?91|0)?([6-9][0-9]{9})")  # ASCII digits only; used with fullmatch
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}")  # loose shape check: user@domain.tld
_NAME_PUNCT = set(" .'’-")  # allowed between letters in a name (incl. the curly apostrophe)


@dataclass
class Passenger:
    """One traveller as entered on the checkout page."""

    name: str
    age: int
    gender: str
    seat: str


@dataclass
class BookingDraft:
    """Everything the checkout page shows, plus what the user asked for (`requested_seats`)."""

    page_url: str
    operator: str
    origin: str
    destination: str
    travel_date: date
    departure: str
    seats: list[str]
    passengers: list[Passenger]
    contact_mobile: str
    boarding_point: str | None = None
    dropping_point: str | None = None
    contact_email: str | None = None
    total_fare: float | None = None
    seat_prices: dict[str, float] = field(default_factory=dict)
    reserved_for: dict[str, str] = field(default_factory=dict)  # seat -> "female"/"male"
    requested_seats: list[str] = field(default_factory=list)
    add_ons: list[str] = field(default_factory=list)  # e.g. "Free Cancellation ₹140", as chosen on the page


@dataclass
class Check:
    """Result of `check_draft`; `ok` means no problems. `mobile` is the normalised 10 digits."""

    ok: bool
    problems: list[str]
    warnings: list[str]
    mobile: str | None


def seat_key(seat: str) -> str:
    """'l1 ' and 'L1' are the same seat."""
    return seat.strip().casefold()


def normalise_gender(text: str) -> str | None:
    """'M', 'Mr', 'female', ... -> 'male' / 'female' / 'other'; None if not recognised."""
    t = text.strip().lower().rstrip(".")
    if t in {"m", "male", "man", "boy", "mr"}:
        return "male"
    if t in {"f", "female", "woman", "girl", "ms", "mrs", "miss"}:
        return "female"
    if t in {"other", "o", "transgender", "x"}:
        return "other"
    return None


def normalise_mobile(text: str | None) -> str | None:
    """Indian mobile as 10 digits (drops +91 / 0 prefix and spacing); None if invalid."""
    m = _MOBILE.fullmatch(re.sub(r"[\s()-]", "", text or ""))
    return m.group(1) if m else None


_SAMPLE_EMAIL_USERS = {"abc", "test", "example", "xyz", "sample", "demo", "asdf", "qwerty", "user", "name"}
_SAMPLE_EMAIL_DOMAINS = {"example.com", "example.org", "test.com", "domain.com"}


def looks_like_sample_mobile(mobile: str) -> bool:
    """9876543210, 6789012345 or 9999999999: real numbers exist, but these are usually typed as filler."""
    # All one digit, or every step +1 / -1 (mod 10, so 9 -> 0 counts): a counting run.
    d = [int(c) for c in mobile]
    steps = {(b - a) % 10 for a, b in zip(d, d[1:], strict=False)}
    return len(set(d)) == 1 or steps in ({1}, {9})


def looks_like_sample_email(email: str) -> bool:
    """'test@...', 'abc@...' or '...@example.com': probably filler, not the user's address."""
    user, _, domain = email.strip().casefold().rpartition("@")
    return user in _SAMPLE_EMAIL_USERS or domain in _SAMPLE_EMAIL_DOMAINS


def valid_name(name: str) -> bool:
    """Letters in any script (incl. Devanagari/Tamil vowel signs), with spaces, . ' ’ - between words."""
    name = name.strip()
    if len(name) < 2:
        return False
    # Unicode categories L* (letters) and M* (combining marks, e.g. Indic vowel signs).
    letter = lambda c: unicodedata.category(c)[0] in "LM"  # noqa: E731
    if not letter(name[0]) or not (letter(name[-1]) or name[-1] == "."):
        return False
    if any(not letter(c) and c not in _NAME_PUNCT for c in name):
        return False
    # Reject doubled punctuation ("A..B", "A - -B") and double spaces.
    return not re.search(r"[.'’-]{2,}|\s{2,}|[.'’-]\s*[.'’-]", name)


def _official_page(url: str) -> bool:
    """https on an official booking/operator host, default port. Only scheme+host are judged,
    so a phone number or 'helpline' in a real checkout query string doesn't block payment."""
    try:
        parts = urlsplit(url.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or not host or port not in (None, 443) or "@" in parts.netloc:
        return False
    # The rebuilt host must be the only host found, and it must be official.
    report = check(f"https://{host}/")
    return report.verdict == "LOOKS_OFFICIAL" and len(report.hosts_checked) == 1


def check_draft(draft: BookingDraft, now: datetime) -> Check:
    """Check a filled-in checkout page before payment. `now` should be in IST."""
    problems: list[str] = []
    warnings: list[str] = []
    today = now.date()

    if not _official_page(draft.page_url):
        problems.append(
            "The checkout page is not a verified official booking site. Do not enter details or pay here."
        )

    if draft.travel_date < today:
        problems.append(f"Travel date {draft.travel_date.isoformat()} is in the past.")
    elif draft.travel_date == today:
        try:
            dep = parse_clock(draft.departure)
            if dep < now.hour * 60 + now.minute:
                problems.append(f"This bus left at {draft.departure} today.")
        except TimeFormatError:
            warnings.append(f"Couldn't read the departure time '{draft.departure}'.")

    seats = [seat_key(s) for s in draft.seats if s.strip()]
    if not seats:
        problems.append("No seats selected.")
    if len(set(seats)) != len(seats):
        problems.append("The same seat is listed twice.")
    asked = {seat_key(s) for s in draft.requested_seats if s.strip()}
    if asked and asked != set(seats):
        problems.append(
            f"Seats on the page ({', '.join(sorted(seats)).upper() or 'none'}) differ from the seats you asked "
            f"for ({', '.join(sorted(asked)).upper()})."
        )

    if len(draft.passengers) != len(seats):
        problems.append(f"{len(seats)} seat(s) but {len(draft.passengers)} passenger(s).")
    assigned = [seat_key(p.seat) for p in draft.passengers]
    stray = set(assigned) - set(seats)
    if stray:
        problems.append(
            f"Passenger seat(s) {', '.join(sorted(stray)).upper()} aren't among the selected seats."
        )
    if len(set(assigned)) != len(assigned):
        problems.append("Two passengers are on the same seat.")

    reserved = {seat_key(s): r for s, r in draft.reserved_for.items()}
    for p in draft.passengers:
        label = p.name.strip() or f"Passenger on seat {p.seat}"
        if not valid_name(p.name):
            problems.append(f"{label}: name looks wrong. Use the name as on a photo ID.")
        if not 1 <= p.age <= 110:
            problems.append(f"{label}: age {p.age} is not valid.")
        gender = normalise_gender(p.gender)
        if gender is None:
            problems.append(f"{label}: gender '{p.gender}' not recognised.")
        rule = reserved.get(seat_key(p.seat))
        if rule and gender and gender != rule:
            problems.append(f"{label}: seat {p.seat.strip()} is reserved for {rule} passengers.")
        if 0 < p.age < 5:
            warnings.append(
                f"{label}: children under 5 often travel free without a seat; check the site's rule."
            )

    mobile = normalise_mobile(draft.contact_mobile)
    if not mobile:
        problems.append(
            "Mobile number must be a 10-digit Indian number (starting 6-9). The ticket is sent to it."
        )
    elif looks_like_sample_mobile(mobile):
        warnings.append(
            f"Mobile {mobile} looks like a sample number; the ticket and the driver's calls go there."
        )
    if draft.contact_email and not _EMAIL.fullmatch(draft.contact_email.strip()):
        problems.append("Email address looks wrong.")
    elif draft.contact_email and looks_like_sample_email(draft.contact_email):
        warnings.append(f"Email {draft.contact_email.strip()} looks like a sample address.")

    if not draft.boarding_point:
        warnings.append("Boarding point not shown; pick it on the site before paying.")
    prices = {seat_key(s): v for s, v in draft.seat_prices.items()}
    # Prices of add-ons the user chose, read from their labels ("Free Cancellation ₹140").
    add_on_total = sum(
        float(a) for item in draft.add_ons for a in re.findall(r"₹\s*([0-9]+(?:\.[0-9]+)?)", item)
    )
    if prices and draft.total_fare is not None:
        seat_sum = sum(prices.get(s, 0) for s in seats)
        expected = seat_sum + add_on_total
        if seat_sum and draft.total_fare < seat_sum - 1:  # ₹1 slack for rounding
            warnings.append(
                f"Total ₹{draft.total_fare:g} is below the seat prices (₹{seat_sum:g}); a discount may apply."
            )
        elif seat_sum and draft.total_fare > expected * 1.02 + 15:  # 2% + ₹15 allowance for fees/rounding
            chosen = f" plus chosen add-ons ₹{add_on_total:g}" if add_on_total else ""
            warnings.append(
                f"Total ₹{draft.total_fare:g} is more than the seats (₹{seat_sum:g}){chosen}. "
                "Check for add-ons (free cancellation, trip guarantee, insurance, donations) you didn't choose."
            )

    return Check(ok=not problems, problems=problems, warnings=warnings, mobile=mobile)
