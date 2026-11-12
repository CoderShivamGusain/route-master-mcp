"""Turn a captured booking page into structured data.

The AI host runs `page_scripts.CAPTURE_JS` in the user's browser tab and passes the JSON it
returns to the `read_page` tool. This module works out which site and page it is, then
pulls out what matters for that step:

    results    -> the buses (operator, times, type, fare, rating, seats left)
    seats      -> every seat (number or position label, deck, type, price, sold/free)
    points     -> boarding and dropping points (time, place, which is selected)
    passenger  -> the form: what's filled, gender, add-ons, the amount
    payment    -> the total and the seat-hold timer

Layouts were checked live on redBus (desktop and mobile layout) and MakeMyTrip in
November 2026. If a site changes its layout the parser returns what it could read plus a
`problems` list, so the AI can fall back to reading the page itself.

Nothing here touches the network or stores data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from urllib.parse import parse_qs, unquote, urlsplit

from mcp_route_master.trust.operators import _HIGHWAY

Site = Literal["redBus", "MakeMyTrip"]
Kind = Literal["results", "seats", "points", "passenger", "payment", "unknown"]

MAX_CAPTURE_CHARS = 150_000
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_CLOCK = re.compile(r"^\d{1,2}:\d{2}$")
_MONEY = re.compile(r"₹\s*([\d,]+(?:\.\d+)?)")


class CaptureError(ValueError):
    """The capture can't be used (not JSON, not a supported site, too big)."""


# --------------------------------------------------------------------------- capture
@dataclass
class Capture:
    url: str
    title: str = ""
    text: str = ""
    cards: list[str] = field(default_factory=list)
    seats: list[dict] = field(default_factory=list)
    mmt_seats: list[dict] = field(default_factory=list)
    radios: list[dict] = field(default_factory=list)
    inputs: list[dict] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)
    choices: list[dict] = field(default_factory=list)
    active: list[str] = field(default_factory=list)

    @property
    def lines(self) -> list[str]:
        return [ln.strip() for ln in self.text.splitlines() if ln.strip()]


def load_capture(raw: str | dict) -> Capture:
    """Parse the JSON the capture script returned (tolerates it being JSON-encoded twice)."""
    if isinstance(raw, str):
        if len(raw) > MAX_CAPTURE_CHARS:
            raise CaptureError("The capture is too large; run the capture script again on one page.")
        try:
            data = json.loads(raw)
            if isinstance(data, str):  # some hosts hand back the script's string re-encoded
                data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise CaptureError(
                "The capture isn't valid JSON. Pass exactly what the capture script returned."
            ) from exc
    else:
        data = raw
    if not isinstance(data, dict) or not isinstance(data.get("url"), str):
        raise CaptureError("The capture has no page address ('url'). Run the capture script again.")
    known = Capture.__dataclass_fields__
    return Capture(**{k: v for k, v in data.items() if k in known and v is not None})


def site_of(url: str) -> Site | None:
    host = (urlsplit(url).hostname or "").lower()
    if host == "redbus.in" or host.endswith(".redbus.in"):
        return "redBus"
    if host == "makemytrip.com" or host.endswith(".makemytrip.com"):
        return "MakeMyTrip"
    return None


def page_kind(c: Capture, site: Site) -> Kind:
    parts = urlsplit(c.url)
    path, query = parts.path.lower(), parse_qs(parts.query)
    host = (parts.hostname or "").lower()
    if "pay" in path or "checkout" in path or host.startswith(("payment.", "payments.")):
        return "payment"
    if site == "redBus":
        step = (query.get("step") or [""])[0].upper()
        if step == "SL":
            return "seats"
        if step == "BPDP":
            return "points"
        if step == "CI":
            return "passenger"
        return "results" if c.cards or "/bus-tickets/" in path else "unknown"
    if "/bus/review" in path:
        return "passenger"
    if c.mmt_seats:
        return "seats"
    return "results" if "buses found" in c.text or "/bus/search" in path else "unknown"


@dataclass
class Route:
    origin: str | None = None
    destination: str | None = None
    travel_date: date | None = None


def route_of(c: Capture, site: Site) -> Route:
    """Cities and date from the page address (both sites put them there)."""
    parts = urlsplit(c.url)
    if site == "redBus":
        q = parse_qs(parts.query)
        origin = (q.get("fromCityName") or [None])[0]
        dest = (q.get("toCityName") or [None])[0]
        d = _redbus_date((q.get("doj") or q.get("onward") or [""])[0])
        return Route(_clean_city(origin), _clean_city(dest), d)
    m = re.search(r"/bus/search/([^/]+)/([^/]+)/(\d{2})-(\d{2})-(\d{4})", parts.path)
    if not m:  # review page: ?from=..&to=..&date=2026-11-01T18:30:00.000Z (UTC; India is +5:30)
        q = parse_qs(parts.query)
        when = None
        if dm := re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", (q.get("date") or [""])[0]):
            y, mo, dd, hh, mi = map(int, dm.groups())
            utc = datetime(y, mo, dd, hh, mi, tzinfo=timezone.utc)
            when = utc.astimezone(timezone(timedelta(hours=5, minutes=30))).date()
        return Route(_clean_city((q.get("from") or [None])[0]), _clean_city((q.get("to") or [None])[0]), when)
    o, t, dd, mm, yy = m.groups()
    return Route(_clean_city(unquote(o)), _clean_city(unquote(t)), _safe_date(int(yy), int(mm), int(dd)))


def _clean_city(name: str | None) -> str | None:
    """'Prayagraj(Uttar Pradesh)' -> 'Prayagraj'; 'Allahabad (Prayagraj)' -> 'Prayagraj'."""
    if not name:
        return None
    name = name.strip()
    inner = re.search(r"\(([^)]+)\)", name)
    outer = re.sub(r"\s*\([^)]*\)", "", name).strip()
    # MakeMyTrip writes 'Allahabad (Prayagraj)': the bracket holds the current name.
    if inner and not re.search(
        r"pradesh|nadu|bengal|kashmir|rajasthan|gujarat|kerala|bihar|haryana|punjab|"
        r"karnataka|maharashtra|odisha|assam|uttarakhand|himachal|goa|telangana|"
        r"andhra|jharkhand|chhattisgarh|state",
        inner.group(1),
        re.I,
    ):
        return inner.group(1).strip()
    return outer or name


def _redbus_date(text: str) -> date | None:
    m = re.match(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", text or "")
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    return _safe_date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _money(text: str) -> float | None:
    m = _MONEY.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None


# --------------------------------------------------------------------------- results
@dataclass
class Bus:
    operator: str
    departure: str
    arrival: str | None = None
    bus_type: str | None = None
    fare: float | None = None
    rating: float | None = None
    review_count: int | None = None
    seats_left: int | None = None
    has_live_tracking: bool | None = None
    day: int | None = None  # MakeMyTrip shows the departure's day ("00:50 13 NOV"): after-midnight buses


@dataclass
class Group:
    """A grouped row such as 'HRTC Buses · 9 buses · from ₹895' (government buses)."""

    name: str
    count: int | None
    fare_from: float | None


def parse_redbus_results(c: Capture) -> tuple[list[Bus], list[Group], list[str]]:
    buses, problems = [], []
    for card in c.cards:
        bus = _redbus_card(card)
        if bus:
            buses.append(bus)
        else:
            problems.append(f"couldn't read: {card[:60]}")
    groups = [
        Group(m.group(1).strip(), int(m.group(2)), float(m.group(3).replace(",", "")))
        for m in re.finditer(
            r"([^\n]{2,40}) Buses\n(?:[^\n]*\n)?(\d+) buses\nstarting from\n₹\s*([\d,]+)", c.text
        )
    ]
    return buses, groups, problems


def redbus_text_to_cards(text: str) -> list[str]:
    """Split text copied from a redBus results page (one item per line) into card lines.

    A card starts at a departure time that is followed by an arrival time; promo tags just
    before it ("Try new", "10% OFF") belong to it and are harmless to the card parser."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    starts = [
        k
        for k in range(len(lines) - 1)
        if _CLOCK.match(lines[k]) and _CLOCK.match(lines[k + 1]) and "Onwards" in lines[k + 2 : k + 12]
    ]
    return [
        " | ".join(lines[a : (starts[i + 1] if i + 1 < len(starts) else len(lines))])
        for i, a in enumerate(starts)
    ]


def _redbus_card(card: str) -> Bus | None:
    """'20:00 | 08:00 | 12h | 36 Seats | ₹1,600 | Onwards | Operator | Ad | Type | 3.7 | 29 | ...'"""
    tok = [t.strip() for t in card.split("|") if t.strip()]
    i = next((k for k, t in enumerate(tok) if _CLOCK.match(t)), None)
    if i is None or i + 1 >= len(tok) or "Onwards" not in tok:
        return None
    dep, arr = tok[i], tok[i + 1] if _CLOCK.match(tok[i + 1]) else None
    on = tok.index("Onwards")
    prices = [_money(t) for t in tok[i:on] if t.startswith("₹")]
    seats = next((int(m.group(1)) for t in tok[i:on] if (m := re.match(r"(\d+) Seats?$", t))), None)
    rest = [t for t in tok[on + 1 :] if t not in ("Ad",)]
    if not rest:
        return None
    operator = rest[0]
    bus_type = rest[1] if len(rest) > 1 and not re.match(r"^\d(\.\d)?$", rest[1]) else None
    nums = rest[2:] if bus_type else rest[1:]
    rating = float(nums[0]) if nums and re.match(r"^\d(\.\d)?$", nums[0]) else None
    count = int(nums[1]) if rating is not None and len(nums) > 1 and nums[1].isdigit() else None
    return Bus(
        operator=operator,
        departure=dep,
        arrival=arr,
        bus_type=bus_type,
        fare=prices[-1] if prices else None,
        rating=rating,
        review_count=count,
        seats_left=seats,
        has_live_tracking=True if "Live Tracking" in tok else None,
    )


def parse_mmt_results(c: Capture) -> tuple[list[Bus], list[Group], list[str]]:
    lines = c.lines
    if "Departure" in lines:  # skip the 'SORT BY ... Departure' header
        lines = lines[lines.index("Departure") + 1 :]
    buses, groups, problems = [], [], []
    block: list[str] = []
    for ln in lines:
        if ln in ("SELECT SEATS", "VIEW BUSES"):
            if ln == "VIEW BUSES":
                g = _mmt_group(block)
                if g:
                    groups.append(g)
            else:
                bus = _mmt_bus(block)
                if bus:
                    buses.append(bus)
                elif block:
                    problems.append(f"couldn't read: {' '.join(block)[:60]}")
            block = []
        elif ln not in ("Policies", "Photos", "Amenities", "Pickups & Drops", "Reviews", "End of buses"):
            block.append(ln)
    return buses, groups, problems


_MMT_TIME = re.compile(r"^(\d{1,2}:\d{2})\s*(\d{1,2})\s*[A-Z]{3}$")


def _mmt_bus(block: list[str]) -> Bus | None:
    idx = [k for k, ln in enumerate(block) if _MMT_TIME.match(ln)]
    if len(idx) < 2 or idx[0] < 2:
        return None
    operator, bus_type = block[idx[0] - 2], block[idx[0] - 1]
    after = block[idx[1] + 1 :]
    rating_at = next((k for k, x in enumerate(after) if re.fullmatch(r"\d\.\d", x)), len(after))
    # A discounted card shows the struck-out price first: the fare is the last price before the rating.
    prices = [float(x.replace(",", "")) for x in after[:rating_at] if re.fullmatch(r"[\d,]+", x)]
    fare = prices[-1] if prices else None
    rating = next((float(x) for x in after if re.fullmatch(r"\d\.\d", x)), None)
    count = next((int(m.group(1)) for x in after if (m := re.fullmatch(r"(\d+) Ratings?", x))), None)
    seats = next((int(m.group(1)) for x in after if (m := re.fullmatch(r"(\d+) Seats? Left", x))), None)
    return Bus(
        operator=operator,
        departure=_MMT_TIME.match(block[idx[0]]).group(1),
        arrival=_MMT_TIME.match(block[idx[1]]).group(1),
        bus_type=bus_type,
        fare=fare,
        rating=rating,
        review_count=count,
        seats_left=seats,
        has_live_tracking=True if "Live Tracking" in after else None,
        day=int(_MMT_TIME.match(block[idx[0]]).group(2)),
    )


def _mmt_group(block: list[str]) -> Group | None:
    count_at = next((k for k, x in enumerate(block) if re.fullmatch(r"\d+ Buses", x)), None)
    if count_at is None or count_at == 0:
        return None
    name = block[max(0, count_at - 2)]
    fare = next((_money(x) for x in block[count_at:] if "₹" in x), None)
    return Group(name, int(block[count_at].split()[0]), fare)


# --------------------------------------------------------------------------- seats
@dataclass
class SeatRead:
    number: str
    deck: str = "lower"
    seat_type: str | None = None
    price: float | None = None
    status: str = "available"
    reserved_for: str | None = None
    x: float | None = None
    y: float | None = None
    dom_index: int | None = None  # MakeMyTrip: which seat icon to click


_REDBUS_SEAT = re.compile(
    r"Seat number ([^,]+), (\w+) deck, seat type ([\w -]+), berth ([^,]+), price (\d+(?:\.\d+)?) rupees, "
    r"availability (\w+)(?:, currently (\w+))?"
)


def parse_redbus_seats(c: Capture) -> tuple[list[SeatRead], list[str]]:
    seats, problems = [], []
    for s in c.seats:
        m = _REDBUS_SEAT.search(s.get("l", ""))
        if not m:
            problems.append(f"couldn't read seat label: {s.get('l', '')[:50]}")
            continue
        num, deck, kind, berth, price, avail, current = m.groups()
        status = "selected" if current == "selected" else ("available" if avail == "available" else "sold")
        reserved = None
        if status != "sold" and (r := re.search(r"reserved for (male|female)", berth)):
            reserved = r.group(1)  # on sold seats this only says who booked it
        x, y = (s.get("p") or [None, None])[:2]
        seats.append(SeatRead(num, deck, kind.strip(), float(price), status, reserved, x, y))
    return seats, problems


def parse_mmt_seats(c: Capture) -> tuple[list[SeatRead], list[str], str | None]:
    """MakeMyTrip draws icons without numbers: label them L1.. (lower) and U1.. (upper),
    front to back, left to right, and keep each icon's DOM index for clicking."""
    lower_n = re.search(r"LOWER BERTH \((\d+)\)", c.text)
    upper_n = re.search(r"UPPER BERTH \((\d+)\)", c.text)
    problems: list[str] = []
    if lower_n:
        n_lower = int(lower_n.group(1))
    elif upper_n:
        n_lower = len(c.mmt_seats) - int(upper_n.group(1))
    else:
        n_lower = len(c.mmt_seats)  # one deck (seater bus) unless the map shows sleepers
        if any("sleeper" in s.get("t", "") for s in c.mmt_seats):
            problems.append("couldn't tell the lower and upper deck apart; all seats are shown as one deck")
    raw = []
    for s in c.mmt_seats:
        t, cls = s.get("t", ""), s.get("c", "")
        status = "sold" if "Disabled" in cls else "selected" if "Selected" in cls else "available"
        reserved = "male" if cls.startswith("male") else "female" if cls.startswith("female") else None
        x, y = (s.get("p") or [None, None])[:2]
        i = int(s.get("i", len(raw)))
        raw.append(
            SeatRead(
                "",
                "lower" if i < n_lower else "upper",
                "sleeper" if "sleeper" in t else "seater",
                _money(s.get("price", "")),
                status,
                reserved if status != "sold" else None,
                x,
                y,
                i,
            )
        )
    for deck, prefix in (("lower", "L"), ("upper", "U")):
        on_deck = sorted(
            (s for s in raw if s.deck == deck),
            key=lambda s: (s.y is None, s.y or 0, s.x or 0, s.dom_index or 0),
        )
        for k, s in enumerate(on_deck, 1):
            s.number = f"{prefix}{k}"
    m = re.search(r"Selected Seats\n([^\n]+)\n(₹[^\n]+)", c.text)
    site_numbers = m.group(1) if m and not m.group(1).startswith("No Seats") else None
    if not raw:
        problems.append("no seat icons found; open the bus's seat map first")
    return raw, problems, site_numbers


# --------------------------------------------------------------------------- points
@dataclass
class Point:
    kind: Literal["boarding", "dropping"]
    time: str
    place: str
    detail: str = ""
    day: str = ""
    selected: bool = False

    def __post_init__(self) -> None:
        # Sites often repeat the name at the start of the address: keep only the new part.
        if self.detail.lower().startswith(self.place.lower()):
            self.detail = self.detail[len(self.place) :].strip(" ,-()")

    @property
    def risky(self) -> bool:
        return bool(_HIGHWAY.search(f"{self.place} {self.detail}".lower()))


def parse_redbus_points(c: Capture) -> list[Point]:
    points = []
    for r in c.radios:
        name = r.get("n", "")
        if not (name.startswith("bp_") or name.startswith("dp_")):
            continue
        parts = [p.strip() for p in r.get("t", "").split("|") if p.strip()]
        if not parts:
            continue
        day = parts[1] if len(parts) > 2 and re.fullmatch(r"\d{1,2} [A-Za-z]{3}", parts[1]) else ""
        rest = parts[2:] if day else parts[1:]
        points.append(
            Point(
                "boarding" if name.startswith("bp_") else "dropping",
                parts[0],
                rest[0] if rest else "",
                rest[1] if len(rest) > 1 and rest[1] != rest[0] else "",
                day,
                bool(r.get("on")),
            )
        )
    return points


_MMT_POINT_TIME = re.compile(r"^(\d{1,2}:\d{2}), (\d{1,2} [A-Z]{3})$")


def parse_mmt_points(c: Capture) -> list[Point]:
    lines = c.lines
    points: list[Point] = []
    kind: Literal["boarding", "dropping"] | None = None
    for k, ln in enumerate(lines):
        if re.fullmatch(r"PICK ?UP POINT", ln, re.I):
            kind = "boarding"
        elif re.fullmatch(r"DROP POINT", ln, re.I):
            kind = "dropping"
        elif kind and (m := _MMT_POINT_TIME.match(ln)) and k + 1 < len(lines):
            detail = lines[k + 2] if k + 2 < len(lines) and not _MMT_POINT_TIME.match(lines[k + 2]) else ""
            if re.fullmatch(r"DROP POINT|Selected Seats", detail, re.I):
                detail = ""
            points.append(Point(kind, m.group(1), lines[k + 1], detail, m.group(2).title()))
        elif ln.startswith("Selected Seats"):
            kind = None
    chosen = " ".join(c.selected).lower()
    for p in points:
        p.selected = bool(chosen) and p.time in chosen and p.place.lower()[:12] in chosen
    return points


# --------------------------------------------------------------------------- passenger form
@dataclass
class FormPassenger:
    seat: str | None
    name: str = ""
    age: str = ""
    gender: str | None = None


@dataclass
class FormRead:
    seats: list[str] = field(default_factory=list)
    passengers: list[FormPassenger] = field(default_factory=list)
    mobile: str = ""
    email: str = ""
    state: str = ""
    whatsapp: bool | None = None
    boarding: str = ""
    dropping: str = ""
    departure: str = ""
    operator: str = ""
    add_ons: dict[str, bool | None] = field(default_factory=dict)  # name -> chosen? (None = not answered)
    reserved: dict[str, str] = field(default_factory=dict)  # seat -> "male"/"female" (men/women-only seats)
    name_fields: int = 0  # how many passenger name boxes the page has (should equal the seats)
    amount: float | None = None
    amount_note: str = ""


def _input(
    c: Capture, *, ids: tuple[str, ...] = (), types: tuple[str, ...] = (), label: str = ""
) -> list[dict]:
    out = []
    for f in c.inputs:
        fid, ftype, flabel = str(f.get("id", "")), f.get("type", ""), str(f.get("label", "")).lower()
        if (ids and fid in ids) or (types and ftype in types) or (label and label in flabel):
            out.append(f)
    return out


def _age(value: object) -> str:
    """'31', '31 yrs' -> '31'; anything else -> '' (so it shows up as missing, not as age 0)."""
    m = re.match(r"\s*(\d{1,3})\b", str(value or ""))
    return m.group(1) if m else ""


def parse_redbus_form(c: Capture) -> FormRead:
    f = FormRead()
    t = c.text
    if m := re.search(
        r"\n(\d{1,2} [A-Za-z]{3}) · (\d{1,2}:\d{2})\n([^\n]+)\n(\d{1,2} [A-Za-z]{3}) · (\d{1,2}:\d{2})\n([^\n]+)",
        t,
    ):
        f.departure, f.boarding = m.group(2), f"{m.group(3)} ({m.group(2)})"
        f.dropping = f"{m.group(6)} ({m.group(5)}, {m.group(4)})"
        before = t[: m.start()].strip().splitlines()
        f.operator = before[-1].strip() if before else ""
    f.seats = re.findall(r"Passenger \d+\nSeat ([^,\n]+)", t)
    names = _input(c, label="your name") or _input(c, ids=("0_4",))
    ages = _input(c, label="age")
    genders = re.findall(
        r"Male\n, 1 of 2\n, (Selected|Not selected)\nFemale\n, 2 of 2\n, (Selected|Not selected)", t
    )
    f.name_fields = len(names)
    for k, seat in enumerate(f.seats or [None]):
        g = genders[k] if k < len(genders) else None
        gender = None if not g else "male" if g[0] == "Selected" else "female" if g[1] == "Selected" else None
        f.passengers.append(
            FormPassenger(
                seat,
                str(names[k]["v"]) if k < len(names) else "",
                _age(ages[k]["v"]) if k < len(ages) else "",
                gender,
            )
        )
    phone, email, state = _input(c, types=("tel",)), _input(c, types=("email",)), _input(c, ids=("0_201",))
    wa = _input(c, ids=("whatsapp_toggle",))
    if phone or email:
        f.mobile = str(phone[0]["v"]) if phone else ""
        f.email = str(email[0]["v"]) if email else ""
        f.state = str(state[0]["v"]) if state else ""
        f.whatsapp = bool(wa[0]["v"]) if wa else None
    elif m := re.search(
        r"Ticket details will be sent to\n(?:Edit\n)?\+91 ?(\d{10})\n([^\n@]+@[^\n]+)\n([^\n]+)\n(WhatsApp[^\n]*)",
        t,
    ):
        # Saved contact shown as text (collapsed): redBus remembers it from an earlier booking.
        f.mobile, f.email, f.state = m.group(1), m.group(2), m.group(3)
        f.whatsapp = None  # the collapsed text said "enabled" while the real switch was off: check in Edit
    for r in c.radios:
        text = r.get("t", "")
        if m := re.match(r"(?:Add|Don.?t add) (Free Cancellation|Trip Guarantee|Travel Insurance)", text):
            name = m.group(1)
            f.add_ons.setdefault(name, None)
            if r.get("on"):
                f.add_ons[name] = text.startswith("Add")
    if m := re.search(r"Amount\n\(Tax excluded\)\n(₹[\d,]+)", t):
        f.amount, f.amount_note = _money(m.group(1)), "before tax"
    return f


def parse_mmt_form(c: Capture) -> FormRead:
    f = FormRead()
    t = c.text
    if m := re.search(r"Seat No:\s*([^\n]+)", t):
        f.seats = [s.strip() for s in m.group(1).split(",") if s.strip()]
    if m := re.search(
        r"(\d{1,2}:\d{2})\s+(\d{1,2} [A-Za-z]{3})' ?\d{2}, \w+\n[^\n]+\n([^\n]+)\n([^\n]+)\n.*?\n(\d{1,2}:\d{2})\s+(\d{1,2} [A-Za-z]{3})' ?\d{2}, \w+\n[^\n]+\n[^\n]+\n([^\n]+)",
        t,
        re.S,
    ):
        f.departure, f.boarding = m.group(1), f"{m.group(3)}, {m.group(4)} ({m.group(1)})"
        f.dropping = f"{m.group(7)} ({m.group(5)}, {m.group(6)})"
    if m := re.search(r"Complete your booking\n(?:[^\n]+\n)?([^\n]+)\nSeat No:", t):
        f.operator = m.group(1)
    names, ages = _input(c, ids=("fname",)), _input(c, ids=("age",))
    f.name_fields = len(names)
    # One active Male/Female tab per traveller, in page order. If the count doesn't match the
    # travellers, gender can't be tied to a person: leave it unknown (asked again), never guessed.
    picked = [
        a.split()[0].lower() for a in c.active if a.split() and a.split()[0].lower() in ("male", "female")
    ]
    seats = f.seats or [None]
    for k, seat in enumerate(seats):
        f.passengers.append(
            FormPassenger(
                seat,
                str(names[k]["v"]) if k < len(names) else "",
                _age(ages[k]["v"]) if k < len(ages) else "",
                picked[k] if len(picked) == len(seats) else None,
            )
        )
    for seat, who in re.findall(r"\nSeat ([^\n]+)\n\((Male|Female) only\)", t):
        f.reserved[seat.strip()] = who.lower()
    mobile, email = _input(c, ids=("mobileNumber",)), _input(c, ids=("contactEmail",))
    f.mobile = str(mobile[0]["v"]) if mobile else ""
    f.email = str(email[0]["v"]) if email else ""
    if c.choices:
        yes = any(x.get("on") for x in c.choices if x.get("t", "").startswith("Add Free Cancellation"))
        no = any(x.get("on") for x in c.choices if x.get("t", "").startswith("No I"))
        f.add_ons["Free Cancellation"] = True if yes else False if no else None
    insurance = [
        i
        for i in c.inputs
        if i.get("type") == "checkbox" and re.search(r"insur|secure your trip", str(i.get("label", "")), re.I)
    ]
    if insurance:
        f.add_ons["Trip insurance"] = bool(insurance[0].get("v"))
    if m := re.search(r"Amount\n(₹\s?[\d,]+)", t):
        f.amount, f.amount_note = _money(m.group(1)), "MakeMyTrip updates it on the next page"
    return f


# --------------------------------------------------------------------------- payment
@dataclass
class PaymentRead:
    total: float | None
    timer: str | None


def parse_payment(c: Capture) -> PaymentRead:
    t = c.text
    total = _money(m.group(0)) if (m := re.search(r"Pay\s*₹\s*[\d,]+(?:\.\d+)?", t)) else None
    if total is None and (
        m := re.search(r"(?:Total|Amount|You pay)[^\n₹]*\n?\s*₹\s*[\d,]+(?:\.\d+)?", t, re.I)
    ):
        total = _money(m.group(0))
    # The hold timer sits on the line right after "Pay ₹…" (redBus), or next to a word like
    # "expires"/"left". Any other clock on the page (e.g. the departure) is not a timer.
    timer = None
    if m and (tm := re.match(r"\s*\n\s*(\d{1,2}:\d{2})\s*(?:\n|$)", t[m.end() :])):
        timer = tm.group(1)
    elif tm := re.search(r"(?:expires?|left|remaining|held|time out|timer)\D{0,20}(\d{1,2}:\d{2})", t, re.I):
        timer = tm.group(1)
    return PaymentRead(total, timer)
