"""mcp-route-master: the FastMCP server (tools, instructions, CLI).

What it is: an MCP server that helps travellers in India find intercity buses, check that an
operator and a booking link are genuine, and book on the official site in their own browser.
It never scrapes, never logs in and never pays: the AI host does the clicking in the user's
browser, route-master supplies the knowledge and the checks, and the user pays.

Tools (all read-only; they never book, pay or store anything):
    plan_bus_trip        dated booking links for a route, filter tips, anti-scam advice
    rank_buses           rank buses the AI saw on a results page (trust, fit, price, seats)
    check_operator       trust signals for one operator (RTC vs copycat, rating vs reviews, ...)
    check_booking_safety scam check for a link, phone number, SMS or WhatsApp message
    booking_steps        click-by-click booking script for one bus, with stop points
    explain_seat_map     text seat diagram and seat suggestions from the seats on the page
    booking_summary      pre-payment check of the filled form; user replies "confirm"
    page_action          ready-made browser script for the next step (capture, select seats, ...)
    read_page            turns a captured redBus/MakeMyTrip page into the answer for that step
    rank_pasted_results  ranks a results page the user copied and pasted (ChatGPT, Gemini)
    search / fetch       standard knowledge tools (ChatGPT deep research): routes, safety, operators

Resources, resource templates and prompts are defined in extras.py; page parsing in browser/pages.py;
the browser scripts in browser/scripts.py.

Transports
  stdio (default)  Claude Desktop, Gemini CLI, Cursor, any local MCP client
  http             Streamable HTTP at /mcp for ChatGPT, claude.ai connectors,
                   Gemini custom apps and other remote clients
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from functools import cache
from typing import Annotated, Any, Literal, get_args
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.function_tool import FunctionTool
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_route_master import __version__, extras
from mcp_route_master.booking.checkout import BookingDraft, Passenger, check_draft, normalise_gender
from mcp_route_master.booking.guide import Site as GuideSite
from mcp_route_master.booking.guide import build as guide_build
from mcp_route_master.booking.guide import inputs_for, traveller_steps
from mcp_route_master.booking.ranking import (
    MAX_SHOWN,
    NEAR_MINUTES,
    Listing,
    Ranked,
    SortBy,
    TimeFormatError,
    arrival_text,
    fmt_clock,
    parse_clock,
    rank,
)
from mcp_route_master.booking.seats import (
    Front,
    Preference,
    Seat,
    deck_plan,
    describe,
    draw,
    layout,
    mixed,
    normalise_kind,
    normalise_reserved,
    normalise_status,
    suggest,
)
from mcp_route_master.browser import scripts as page_scripts
from mcp_route_master.browser.pages import (
    Bus,
    Capture,
    CaptureError,
    FormRead,
    Point,
    load_capture,
    page_kind,
    parse_mmt_form,
    parse_mmt_points,
    parse_mmt_results,
    parse_mmt_seats,
    parse_payment,
    parse_redbus_form,
    parse_redbus_points,
    parse_redbus_results,
    parse_redbus_seats,
    redbus_text_to_cards,
    route_of,
    site_of,
)
from mcp_route_master.common.schema import opt, opt_enum, opt_list
from mcp_route_master.common.store import PendingLocations, data_dir
from mcp_route_master.common.textutil import load_data
from mcp_route_master.http_options import build_middleware
from mcp_route_master.places.dates import IST, DateError, human_date, parse_travel_date, today_ist
from mcp_route_master.places.links import IRCTC_BUS_URL, build_links
from mcp_route_master.places.resolver import Hub, Resolution, Resolver
from mcp_route_master.trust.bus_types import BusPreference
from mcp_route_master.trust.operators import Assessment, assess
from mcp_route_master.trust.safety import MAX_INPUT_CHARS, REPORT_LINE, SafetyReport, check

log = logging.getLogger("mcp_route_master")

INSTRUCTIONS = """\
Helps travellers in India find intercity buses, book them on official sites, and avoid booking scams.

Flow (call the tools in this order):
1. plan_bus_trip: for any "bus from A to B" request. Convert "tomorrow"/"next Friday" to YYYY-MM-DD
   in India time (IST) first. Open its dated link in the browser if you can.
2. rank_buses: pass the buses the site shows, exactly as shown. Some sites group government buses
   (UPSRTC, HRTC, UTC...) under one "View buses" row; open it and include them, or mention them.
3. check_operator: for the bus the user picks, with the rating, review count and tracking shown.
4. booking_steps: when the user wants to book. It is a script for you: follow it, don't paste it.
5. explain_seat_map: read every seat on the site's seat map and pass them; show its diagram.
6. booking_summary: on the passenger page, after filling it. Show its summary. Go on only when the
   latest summary has no "Fix before paying" items and the user replies "confirm" (or a clear yes)
   to it. Then check the page still matches; if anything changed, run booking_summary again, show it
   and wait for a new "confirm" (an earlier one doesn't carry over). Press the site's continue
   button once and stop on the payment page.
7. Hand over with one short message: the total, how long the site holds the seat, and "Pay here:
   <link>" to that page on the official site (say which site). If the total is more than the
   approved fare plus tax (about 5%), say what was added and don't call it ready. If the site
   ignored your click, give the link to the page you stopped on and say which button to press.
   The user pays; you never do.
check_booking_safety: whenever the user shares a link, phone number, SMS or WhatsApp message about a
booking, payment or refund.

Browser shortcut (redBus and MakeMyTrip): on every page, call page_action(action="capture"), run the
script it gives in the user's tab, and pass the result to read_page; it returns the ranked buses, the
seat map, the numbered stops, what's missing on the form or the summary, or the payment hand-over.
For clicks use page_action (open_bus, select_seats, choose_points, decline_addons, continue) and
follow its note: some steps need a real click on the element it marks as "rm-target". You still type
the user's details yourself, after they agree.

Without browser control (ChatGPT, Gemini, or any app that can't click in the user's own browser):
- Give plan_bus_trip's links. Ask the user to open one, select all the results page (Ctrl+A / long-press
  "Select all"), copy it and paste it here; pass it to rank_pasted_results.
- For the chosen bus call booking_steps with for_traveller=true and show those steps: the user books and
  pays on the site themselves. Offer check_operator and check_booking_safety at any time.
- search / fetch let knowledge features (e.g. ChatGPT deep research) find routes, the safety checklist and
  government operators.

Talking to the user:
- Keep every reply short and plain: at most one small table or list, then one question.
- Show the tools' text as it is, including every row of a bus table (up to 10); don't shorten it.
  Don't add internal details (tool names, page code, test notes).
- Ask only for what the current step needs.

Booking in the user's browser (only if you can control one):
- Select only the seats the user names; after every click, read the page again to check the right
  seat or point got selected, and undo a wrong one.
- Ask before typing the user's name, age, gender, mobile or email. Ask about every paid add-on;
  never add one they didn't ask for. If the site pre-fills something (e.g. State of residence),
  ask the user to confirm it.
- Never log in, create accounts, or enter OTPs, passwords, UPI PIN or card details; if the site asks
  for any of these, stop and let the user do it.
- If a click is ignored (often the last continue button, because of bot checks), don't retry or
  work around it: give the user the page link.

Rules:
- This server does NOT fetch live seats or fares. Never invent buses, times, prices, seats or seat
  counts; only pass what the official site shows.
- The user pays on the official site themselves. Never ask for or handle payment, OTP, UPI PIN,
  card or ID details.
"""

DepartureWindow = Literal["any", "morning", "afternoon", "evening", "night"]
_TIME_WINDOWS = {
    "morning": "06:00-12:00",
    "afternoon": "12:00-18:00",
    "evening": "18:00-24:00 (most overnight buses leave in this window)",
    "night": "after 18:00 (most overnight buses leave between 18:00 and midnight)",
}
mcp: FastMCP = FastMCP(name="mcp-route-master", instructions=INSTRUCTIONS, version=__version__)
_pending = PendingLocations()
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def _strip_null_defaults(schema: Any) -> None:
    """Drop `"default": null` from tool parameter schemas; some hosts reject a null default on a typed field."""
    if isinstance(schema, dict):
        if "default" in schema and schema["default"] is None:
            del schema["default"]
        for value in schema.values():
            _strip_null_defaults(value)
    elif isinstance(schema, list):
        for value in schema:
            _strip_null_defaults(value)


def _tool(
    *, server: FastMCP | None = None, **kwargs: Any
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Like @mcp.tool, but publishes a parameter schema every host accepts.

    `server` registers the tool on another FastMCP server (the signed-in one, extension/app.py)."""

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        annotations = kwargs.pop("annotations", None)
        tool = FunctionTool.from_function(
            fn, annotations=ToolAnnotations(**annotations) if annotations else None, **kwargs
        )
        _strip_null_defaults(tool.parameters)
        (server or mcp).add_tool(tool)
        return fn

    return register


@cache
def get_resolver() -> Resolver:
    return Resolver()


async def _off_loop(fn: Callable[..., Any], *args: Any) -> Any:
    """Run CPU-bound parsing (big page captures, pasted pages) in a worker thread.

    Every tool is `async def`; this keeps one large capture from stalling other requests on the
    same server. The parsers are pure functions (no shared state), so threads are safe.
    """
    return await asyncio.to_thread(fn, *args)


def _parse_pasted_redbus(text: str):
    return parse_redbus_results(
        Capture(url="https://www.redbus.in/bus-tickets/", text=text, cards=redbus_text_to_cards(text))
    )


# ------------------------------------------------------------------ formatting
def _resolution_problem(label: str, res: Resolution) -> str:
    if res.reason == "multiple":
        places = ", ".join(h.name for h in res.candidates)
        return f"{label} '{res.query}' names more than one place ({places}). Pass one place per field."
    if res.status == "ambiguous":
        options = "; ".join(h.label for h in res.candidates)
        return f"{label} '{res.query}' matches more than one place: {options}. Ask which one."
    hint = f" Did you mean: {', '.join(res.suggestions)}?" if res.suggestions else ""
    return (
        f"{label} '{res.query}' isn't in the place list.{hint} Ask the user for the nearest "
        "city with a bus stand, or search it directly on a booking site."
    )


def _resolution_note(label: str, res: Resolution) -> str | None:
    assert res.hub is not None
    if res.kind in ("terminal", "area") and res.place:
        return (
            f"{label}: '{res.place}' is in {res.hub.name}; search {res.hub.name} and choose "
            f"'{res.place}' as the {'boarding' if label == 'From' else 'drop'} point."
        )
    if res.fuzzy or res.assumed:
        return f"{label}: read '{res.query}' as {res.hub.label}. Correct me if that's wrong."
    return None


def _bus_type_tips(pref: BusPreference) -> list[str]:
    tips: list[str] = []
    if pref in ("ac", "ac_sleeper"):
        tips.append("Tick the **AC** filter.")
    if pref in ("non_ac", "non_ac_sleeper"):
        tips.append("Tick the **Non-AC** filter.")
    if pref in ("sleeper", "ac_sleeper", "non_ac_sleeper"):
        tips.append("Tick **Sleeper**. A 'Semi-Sleeper' is a reclining seat, not a bed.")
    if pref == "semi_sleeper":
        tips.append("Look for 'Semi-Sleeper' in the bus name (reclining seat).")
    if pref == "seater":
        tips.append("Tick **Seater**.")
    if pref == "volvo":
        tips.append("Look for 'Volvo' or 'Multi-Axle' in the bus name, or tick a Volvo filter.")
    return tips


@cache
def _gov_operators_by_state() -> dict[str, list[dict]]:
    by_state: dict[str, list[dict]] = {}
    for op in load_data("operators.json")["government_operators"]:
        by_state.setdefault(op["state"], []).append(op)
    return by_state


def _op_label(op: dict) -> str:
    """'HRTC (Himachal Pradesh)', but 'KSRTC (Karnataka)' rather than 'KSRTC (Karnataka) (Karnataka)'."""
    return op["short"] if op["state"] in op["short"] else f"{op['short']} ({op['state']})"


def _government_lines(origin: Hub, destination: Hub) -> list[str]:
    lines = [
        "**Government (RTC) buses:**",
        f"- [IRCTC Bus]({IRCTC_BUS_URL}): government booking portal for state buses.",
    ]
    seen: set[str] = set()
    for state in dict.fromkeys([origin.state, destination.state]):
        for op in _gov_operators_by_state().get(state, []):
            if op["short"] in seen:
                continue
            seen.add(op["short"])
            if op["domains"]:
                sites = ", ".join(f"https://{d}" for d in op["domains"])
                lines.append(f"- {_op_label(op)}: {sites}")
            else:
                lines.append(
                    f"- {_op_label(op)}: book via IRCTC Bus or the RTC section on redBus/MakeMyTrip."
                )
    lines.append(
        "- On redBus/MakeMyTrip, government buses appear as a group (e.g. 'HRTC "
        "Buses') near the top of the results."
    )
    return lines


def format_plan(
    resolver: Resolver,
    origin_res: Resolution,
    dest_res: Resolution,
    d: date,
    bus_type: BusPreference,
    departure_window: DepartureWindow,
    government_only: bool,
) -> str:
    origin, destination = origin_res.hub, dest_res.hub
    assert origin is not None and destination is not None
    out = [f"## {origin.name} → {destination.name} · {human_date(d)}"]
    notes = [n for n in (_resolution_note("From", origin_res), _resolution_note("To", dest_res)) if n]
    out += [f"_{n}_" for n in notes]

    if government_only:
        out += [""] + _government_lines(origin, destination)

    links = build_links(resolver, origin, destination, d)
    dated = [link for link in links if link.kind == "dated_search"]
    others = [link for link in links if link.kind != "dated_search"]
    out += ["", "**See the buses for this date:**" if dated else "**Book on:**"]
    out += [f"- [{link.platform}]({link.url})" for link in (dated or others)]
    if dated and others:
        out.append("Also (choose the date there): " + " · ".join(f"[{x.platform}]({x.url})" for x in others))
    if not government_only:
        out.append(f"Government buses: [IRCTC Bus]({IRCTC_BUS_URL})")

    tips = _bus_type_tips(bus_type)
    if departure_window in _TIME_WINDOWS:
        tips.append(f"Departure filter: {_TIME_WINDOWS[departure_window]}.")
    if tips:
        out += ["", "**Tip:** " + " ".join(tips)]
    out += [
        "",
        "_Seats and fares are on the site. Pay only on the site itself; nobody needs your OTP or UPI PIN._",
    ]
    return "\n".join(out)


def format_assessment(a: Assessment) -> str:
    headline = {
        "HIGH": "Strong trust signals",
        "MODERATE": "Mixed signals",
        "CAUTION": "Be careful",
        "NOT_ENOUGH_INFO": "Not enough information yet",
    }[a.level]
    score = f" ({a.score}/100)" if a.score is not None else ""
    out = [f"## Operator check: {a.operator}", f"**{headline}**{score}"]
    out += [f"- Good: {p}" for p in a.positives]
    out += [f"- Watch out: {w}" for w in a.warnings]
    if a.bus_type:
        out.append(f"- Bus type: {a.bus_type.describe()}.")
    if a.level == "NOT_ENOUGH_INFO" or (a.score is not None and len(a.positives) + len(a.warnings) < 2):
        out.append(
            "_For a better check, tell me the rating, review count, live tracking, boarding point and bus type._"
        )
    out.append("_Signals from the listing, not a guarantee._")
    return "\n".join(out)


def format_safety(r: SafetyReport) -> str:
    headline = {
        "HIGH_RISK": "HIGH RISK: do not pay or share anything",
        "CAUTION": "Caution: check before you continue",
        "LOOKS_OFFICIAL": "Looks like an official booking site",
        "NO_RED_FLAGS_FOUND": "No obvious red flags found",
    }[r.verdict]
    out = [f"## Booking safety check: {headline}"]
    for f in r.findings:
        out.append(f"- {'RED FLAG' if f.severity == 'high' else 'Caution'}: {f.message}")
    if r.official_hosts:
        out.append(f"- Official: {', '.join(r.official_hosts)}")
    if r.verdict == "NO_RED_FLAGS_FOUND":
        out.append(
            "- Nothing matched known scam patterns, but people and phone numbers can't be verified offline."
        )
    out += [
        "",
        "**Stay safe:** type the site address yourself or use the official app; pay only on its "
        "checkout page; never share OTP, UPI PIN or card CVV; refunds never need a QR scan.",
        REPORT_LINE,
    ]
    return "\n".join(out)


# ----------------------------------------------------------------------- tools
@_tool(annotations={**_READ_ONLY, "title": "Plan a bus trip"}, output_schema=None)
async def plan_bus_trip(
    origin: Annotated[
        str,
        Field(
            description="Where the traveller starts: a city or a known boarding point, as the user "
            "said it (e.g. 'Delhi', 'Bangalore', 'Kashmiri Gate').",
            max_length=100,
        ),
    ],
    destination: Annotated[
        str, Field(description="Where the traveller is going: a city or a known drop point.", max_length=100)
    ],
    travel_date: Annotated[
        str,
        Field(
            description="Departure date as YYYY-MM-DD in India time. Convert 'tomorrow' or 'next "
            "Monday' to a date before calling.",
            max_length=30,
        ),
    ],
    bus_type: Annotated[
        BusPreference,
        Field(
            description="Seat/AC preference. 'sleeper' means a bed (berth); 'semi_sleeper' is a "
            "reclining seat."
        ),
    ] = "any",
    departure_window: Annotated[DepartureWindow, Field(description="Preferred departure time.")] = "any",
    government_only: Annotated[
        bool, Field(description="True when the user wants government (state RTC) buses only.")
    ] = False,
) -> str:
    """Plan an intercity bus trip in India.

    Resolves the two places, then returns booking links for this route and date on official
    sites (redBus, MakeMyTrip, Goibibo, AbhiBus, Cleartrip, FlixBus, IRCTC Bus), filter tips for
    the requested bus type and time, and anti-scam advice. It does not return live seats or
    fares: the user sees those on the linked site.
    """
    try:
        d = parse_travel_date(travel_date)
    except DateError as exc:
        raise ToolError(str(exc)) from exc

    resolver = get_resolver()
    origin_res, dest_res = resolver.resolve(origin), resolver.resolve(destination)
    problems = []
    for label, res in (("Origin", origin_res), ("Destination", dest_res)):
        if not res.ok:
            if res.status == "not_found":
                await _pending.record(res.query)
            problems.append(_resolution_problem(label, res))
    if problems:
        raise ToolError(" ".join(problems))
    if origin_res.hub is dest_res.hub:
        raise ToolError(
            f"Origin and destination both resolve to {origin_res.hub.name}. "
            "Intercity buses need two different cities."
        )
    return format_plan(resolver, origin_res, dest_res, d, bus_type, departure_window, government_only)


# Optional fields use a plain type with a None default (no "anyOf"/"null" in the JSON
# schema), which every host's function-calling format accepts, including Gemini.
@_tool(annotations={**_READ_ONLY, "title": "Check a bus operator"}, output_schema=None)
async def check_operator(
    operator_name: Annotated[
        str,
        Field(
            description="Operator name exactly as shown on the booking site, e.g. 'HRTC' or "
            "'Laxmi Holidays Pvt Ltd'.",
            min_length=2,
            max_length=120,
        ),
    ],
    rating: opt(float, description="Star rating shown on the listing (0-5), if known.", ge=0, le=5) = None,
    review_count: opt(int, description="Number of ratings/reviews shown, if known.", ge=0) = None,
    has_live_tracking: opt(bool, description="Whether the listing shows live tracking, if known.") = None,
    boarding_point: opt(
        str, description="Boarding point shown on the listing, if known.", max_length=150
    ) = None,
    bus_type: opt(
        str,
        description="Bus type text from the listing, e.g. 'Volvo 9600 A/C Semi Sleeper (2+2)'.",
        max_length=150,
    ) = None,
) -> str:
    """Check whether a bus operator/listing looks trustworthy.

    Detects real government (RTC) operators and copycat names, weighs the rating by the number
    of reviews, and flags highway boarding points and 'semi-sleeper' listings sold as sleepers.
    Uses only what the user can see on the listing; it does not look anything up online.
    """
    resolver = get_resolver()
    try:
        result = assess(
            operator_name,
            rating,
            review_count,
            has_live_tracking,
            boarding_point,
            bus_type,
            is_known_terminal=resolver.is_known_terminal,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return format_assessment(result)


@_tool(annotations={**_READ_ONLY, "title": "Check a booking link or message for scams"}, output_schema=None)
async def check_booking_safety(
    content: Annotated[
        str,
        Field(
            description="A link, phone number, SMS, WhatsApp message or email text about a bus "
            "booking, payment or refund, pasted as-is.",
            min_length=3,
            max_length=MAX_INPUT_CHARS,
        ),
    ],
) -> str:
    """Check a booking link, phone number or message for common bus-booking scams in India.

    Flags fake or look-alike websites, requests for OTP/UPI PIN/card details, payment to personal
    UPI IDs, WhatsApp bookings, remote-access apps and refund tricks. Works offline; a clean result
    means no known red flags, not a guarantee.
    """
    return format_safety(check(content))


# Optional fields use plain types with a None default (no "anyOf"/"null" in the JSON schema),
# which every host's function-calling format accepts, including Gemini.
class BusListing(BaseModel):
    """One bus exactly as shown on a booking site's results page."""

    operator: Annotated[
        str, Field(description="Operator name as shown, e.g. 'KSAPHA TRAVELS'.", min_length=2, max_length=120)
    ]
    departure: Annotated[str, Field(description="Departure time, HH:MM (24h) or '11:58 PM'.", max_length=12)]
    arrival: opt(str, description="Arrival time as shown, HH:MM.", max_length=12) = None
    bus_type: opt(
        str, description="Bus type text, e.g. 'Volvo 9600 A/C Semi Sleeper (2+2)'.", max_length=150
    ) = None
    fare: opt(float, description="Fare in rupees.", ge=0) = None
    rating: opt(float, description="Star rating 0-5.", ge=0, le=5) = None
    review_count: opt(int, description="Number of ratings/reviews.", ge=0) = None
    seats_left: opt(int, description="Seats left.", ge=0) = None
    has_live_tracking: opt(bool, description="Listing shows live tracking.") = None
    boarding_point: opt(str, description="Boarding point, if shown.", max_length=150) = None
    platform: opt(
        str,
        description="Site it was seen on: redBus, MakeMyTrip, Goibibo, AbhiBus, Cleartrip, FlixBus.",
        max_length=30,
    ) = None
    booking_url: opt(
        str,
        description="A link that opens THIS bus (its seat selection or ride page), only if the site "
        "showed one. Must be the address actually seen; never construct or guess it.",
        max_length=600,
    ) = None


def _book_cell(r: Ranked) -> str:
    if r.direct_url:
        return f"[This bus]({r.direct_url})"
    name = r.listing.operator.replace("|", "/").replace("[", "(").replace("]", ")")
    return f"[{r.link.platform}]({r.link.url}), find {name} {fmt_clock(r.depart_min)}"


def _verified_booking_url(url: str | None) -> tuple[str | None, str | None]:
    """Keep a per-bus link only if it is https on an official booking/operator domain."""
    if not url:
        return None, None
    url = url.strip()
    report = check(url)
    if (
        url.lower().startswith("https://")
        and report.verdict == "LOOKS_OFFICIAL"
        and len(report.hosts_checked) == 1
    ):
        return url, None
    return None, "its link isn't an official booking site, so it was replaced"


def format_ranking(
    origin: Hub,
    destination: Hub,
    d: date,
    ranked: list[Ranked],
    departed: list[Listing],
    problems: list[str],
    sort_by: SortBy,
    bus_type: BusPreference = "any",
) -> str:
    order = {
        "best": "best first",
        "earliest": "earliest you can catch first",
        "cheapest": "cheapest first",
        "fastest": "fastest first",
    }[sort_by]
    shown = ranked[:MAX_SHOWN]
    noun = "bus" if len(ranked) == 1 else "buses"
    out = [f"## {origin.name} → {destination.name} · {human_date(d)} · {len(ranked)} {noun} ({order})"]
    if bus_type != "any" and ranked and not any(r.level == 0 for r in ranked):
        wanted = bus_type.replace("_", " ")
        out.append(f"_None of these is clearly {wanted}; they're ranked as the closest options._")
        for r in ranked:
            r.notes = [n for n in r.notes if n != f"not {wanted}"]
    near = sum(1 for r in ranked if r.near)
    if near:  # (when none is near, rank_buses says which bus is closest)
        out.append(
            f"_{near} of these leave within {NEAR_MINUTES // 60} hours of your time; they're listed first._"
        )
    if shown:
        top = next((r for r in shown if not r.tight), shown[0])
        why = [
            f"trust {top.assessment.score}/100" if top.assessment.score is not None else "",
            f"₹{top.listing.fare:g}" if top.listing.fare else "",
            f"{top.listing.seats_left} seats left" if top.listing.seats_left is not None else "",
        ]
        if len(shown) > 1:
            out.append(
                f"**Top pick:** {top.listing.operator}, {fmt_clock(top.depart_min)}"
                f" ({', '.join(w for w in why if w)})."
            )
        main = shown[0].link
        same_site = all(
            r.link.url == main.url
            and not r.direct_url
            and (r.listing.platform or main.platform).strip().casefold().replace(" ", "")
            == main.platform.casefold().replace(" ", "")
            for r in shown
        )
        if same_site:
            out.append(
                f"Book on [{main.platform}]({main.url}): find the operator and time, then Select Seats."
            )
            book_col = False
        else:
            book_col = True
        out += [
            "",
            "| # | Time | Bus | Fare | Trust | Note" + (" | Book |" if book_col else " |"),
            "|---|---|---|---|---|---" + ("|---|" if book_col else "|"),
        ]
        for i, r in enumerate(shown, 1):
            b = r.listing
            trust = f"{r.assessment.score}" if r.assessment.score is not None else "?"
            bus = f"{b.operator}" + (f" · {b.bus_type}" if b.bus_type else "")
            row = (
                f"| {i} | {fmt_clock(r.depart_min)} → {arrival_text(r)} | {bus.replace('|', '/')} | "
                f"{f'₹{b.fare:g}' if b.fare else '?'} | {trust} | {'; '.join(r.notes) or '-'} |"
            )
            out.append(row + (f" {_book_cell(r)} |" if book_col else ""))
    if len(ranked) > MAX_SHOWN:
        out.append(f"_{len(ranked) - MAX_SHOWN} lower-ranked buses not shown._")
    if departed:
        out.append(
            "Already left or leaving now: " + ", ".join(f"{b.operator} {b.departure}" for b in departed) + "."
        )
    if problems:
        out.append("Skipped (couldn't read): " + "; ".join(problems))
    out.append("_Trust (0-100) comes from the listing, not a guarantee._")
    return "\n".join(out)


@_tool(annotations={**_READ_ONLY, "title": "Rank bus listings"}, output_schema=None)
async def rank_buses(
    origin: Annotated[str, Field(description="Starting city, as for plan_bus_trip.", max_length=100)],
    destination: Annotated[str, Field(description="Destination city.", max_length=100)],
    travel_date: Annotated[str, Field(description="Departure date as YYYY-MM-DD (IST).", max_length=30)],
    buses: Annotated[
        list[BusListing],
        Field(
            description="Buses copied from a booking site's results page (what you or the user can see). "
            "Never invent entries.",
            min_length=1,
            max_length=40,
        ),
    ],
    bus_type: Annotated[BusPreference, Field(description="Seat/AC preference to rank by.")] = "any",
    sort_by: Annotated[
        SortBy,
        Field(
            description="'best' balances trust, fit, price and seats; "
            "'earliest' puts the soonest catchable bus first."
        ),
    ] = "best",
    around_time: opt(
        str,
        description="Time the traveller wants to leave, HH:MM (e.g. '09:00' for 'around 9 am'). "
        "Buses near it rank higher.",
        max_length=12,
    ) = None,
) -> str:
    """Rank real bus listings for the traveller and give each one a booking link.

    Use after plan_bus_trip, once real results are visible (opened in a browser or pasted by the
    user). Scores each bus for trust (government operator, copycat names, rating weighted by
    reviews, live tracking, boarding point), checks the bus type (semi-sleeper is not a sleeper),
    drops buses that already left today, and returns a ranked table with a link per bus.
    """
    try:
        d = parse_travel_date(travel_date)
    except DateError as exc:
        raise ToolError(str(exc)) from exc
    resolver = get_resolver()
    origin_res, dest_res = resolver.resolve(origin), resolver.resolve(destination)
    problems = [
        _resolution_problem(label, res)
        for label, res in (("Origin", origin_res), ("Destination", dest_res))
        if not res.ok
    ]
    if problems:
        raise ToolError(" ".join(problems))
    links = build_links(resolver, origin_res.hub, dest_res.hub, d)
    listings = []
    url_warnings: dict[int, str] = {}
    for b in buses:
        listing = Listing(**b.model_dump())
        listing.booking_url, warning = _verified_booking_url(listing.booking_url)
        if warning:
            url_warnings[id(listing)] = warning
        listings.append(listing)
    try:
        target = parse_clock(around_time) if around_time else None
    except TimeFormatError as exc:
        raise ToolError(str(exc)) from exc
    ranked, departed, bad = rank(
        listings,
        d,
        links,
        bus_type,
        sort_by,
        is_known_terminal=resolver.is_known_terminal,
        around_time=around_time,
    )
    if not ranked and not departed:
        raise ToolError("None of the buses could be read. " + "; ".join(bad))
    for r in ranked:
        if id(r.listing) in url_warnings:
            r.notes.append("its own link wasn't an official site, so it was dropped")
    text = format_ranking(origin_res.hub, dest_res.hub, d, ranked, departed, bad, sort_by, bus_type)
    if target is not None and ranked:
        gaps = [min(abs(r.depart_min - target), 1440 - abs(r.depart_min - target)) for r in ranked]
        if min(gaps) > NEAR_MINUTES:
            closest = ranked[gaps.index(min(gaps))]
            note = (
                f"_No bus leaves near {fmt_clock(target)}; the closest is "
                f"{closest.listing.operator} at {fmt_clock(closest.depart_min)}._"
            )
            head, _, rest = text.partition("\n")
            text = f"{head}\n{note}\n{rest}"
    return text


class SeatInput(BaseModel):
    """One seat exactly as the official site shows it."""

    number: Annotated[
        str,
        Field(description="Seat number/label as shown, e.g. '15', 'L4', 'UB3'.", min_length=1, max_length=8),
    ]
    deck: opt(str, description="'lower' or 'upper' (default lower).", max_length=10) = None
    seat_type: opt(str, description="seater, semi-sleeper or sleeper.", max_length=20) = None
    status: opt(str, description="available, sold or selected.", max_length=20) = None
    price: opt(float, description="Seat price in rupees.", ge=0) = None
    reserved_for: opt(str, description="'female' or 'male' if the seat is reserved.", max_length=30) = None
    x: opt(float, description="On-screen x position of the seat (left edge), if known.") = None
    y: opt(float, description="On-screen y position of the seat (top edge), if known.") = None


class PassengerInput(BaseModel):
    """One passenger exactly as typed on the checkout page."""

    name: Annotated[str, Field(description="Full name as on photo ID.", max_length=80)]
    age: Annotated[int, Field(description="Age in years.", ge=0, le=120)]
    gender: Annotated[str, Field(description="male, female or other.", max_length=12)]
    seat: Annotated[str, Field(description="Seat number this passenger sits in.", max_length=8)]


@_tool(annotations={**_READ_ONLY, "title": "Show a bus seat map"}, output_schema=None)
async def explain_seat_map(
    operator: Annotated[str, Field(description="Operator name as shown.", max_length=120)],
    departure: Annotated[str, Field(description="Departure time, HH:MM.", max_length=12)],
    seats: Annotated[
        list[SeatInput],
        Field(
            description="Every seat read from the site's seat map. Never invent seats.",
            min_length=1,
            max_length=120,
        ),
    ],
    seats_wanted: opt(int, description="How many seats the user needs (default 1).", ge=1, le=6) = None,
    preferences: opt_list(
        Preference,
        {"type": "string", "enum": list(get_args(Preference))},
        description="What the user prefers: window, aisle, front, back, together, lower, upper, cheapest, "
        "sleeper (a berth, on buses with both), seater.",
        max_length=8,
    ) = None,
    site: opt(str, description="Site the map was read from, e.g. redBus.", max_length=30) = None,
    labels_are_positions: opt(
        bool,
        description="True when the site shows no seat numbers and you labelled seats by position "
        "(always the case on MakeMyTrip).",
    ) = None,
    front: opt_enum(
        Front,
        description="Where the front of the bus (driver) is on screen: top, bottom, left or right. "
        "Leave empty to detect.",
    ) = None,
) -> str:
    """Draw a bus's seat map with seat numbers, prices and booked seats, and suggest seats.

    Use when the user asks about one bus. The seat data must be read from the official site's seat
    selection page; this tool does not fetch it. The user then names the seats they want.
    """
    wanted = seats_wanted or 1
    parsed = [
        Seat(
            number=s.number.strip(),
            deck=(s.deck or "lower").strip().lower() or "lower",
            kind=normalise_kind(s.seat_type),
            status=normalise_status(s.status),
            price=s.price,
            reserved_for=normalise_reserved(s.reserved_for),
            x=s.x,
            y=s.y,
        )
        for s in seats
    ]
    if any(not p.number for p in parsed):
        raise ToolError("A seat has no number; re-read the seat map.")
    if len({(p.deck, p.number) for p in parsed}) != len(parsed):
        raise ToolError("Some seat numbers appear twice on the same deck; re-read the seat map.")
    decks = layout(parsed, front or "auto")
    free = [p for p in parsed if p.status == "available"]
    prices = sorted({p.price for p in free if p.price is not None})
    if prices:
        price_text = f"₹{prices[0]:g}" if len(prices) == 1 else f"₹{prices[0]:g}–₹{prices[-1]:g}"
    else:
        price_text = ""
    out = [f"## Seats: {operator}" + (f", {departure}" if departure else "") + (f" ({site})" if site else "")]
    facts = [f"{len(free)} of {len(parsed)} free"]
    kinds = {p.kind for p in parsed} - {"unknown"}
    if "sleeper" in kinds and len(kinds) > 1:
        berths = sum(1 for p in free if p.kind == "sleeper")
        sitting = sum(1 for p in free if p.kind in ("seater", "semi_sleeper"))
        facts.append(f"{berths} sleeper berth(s) + {sitting} seat(s)")
    if price_text:
        facts.append(price_text)
    out += [" · ".join(facts), ""]
    for d in decks:
        plan = deck_plan(d)
        head = f"**{d.deck.title()} deck**" if len(decks) > 1 else ""
        if plan:
            head = f"{head} — {plan}" if head else f"_{plan}_"
        if head:
            out.append(head)
        out += ["```", draw(d), "```"]
    out.append(
        (
            "`B` sleeper berth (lie down; as long as two seats) · `S` seat · "
            if any(mixed(d) for d in decks)
            else ""
        )
        + "`--` sold · `OK` selected"
        + (" · F/M women/men only" if any(p.reserved_for for p in parsed) else "")
        + (
            " · labels are positions; the site shows its own number once a seat is selected"
            if labels_are_positions or _is_makemytrip(site)
            else ""
        )
    )
    prefs = list(preferences or [])
    if wanted > 1 and "together" not in prefs:
        prefs.append("together")
    picks = suggest(parsed, wanted, prefs, decks)
    if picks:
        out.append(f"\n**Suggested ({wanted}):** " + " + ".join(describe(p) for p in picks))
        if "sleeper" in prefs and any(p.kind != "sleeper" for p in picks):
            out.append(f"_Not enough free unreserved berths for {wanted}; this suggestion includes seats._")
    elif free:
        out.append(f"\nNot enough unreserved free seats for {wanted}.")
    else:
        out.append("\nThis bus is sold out.")
    if picks:
        out.append("Which seat do you want?")
    return "\n".join(out)


@_tool(annotations={**_READ_ONLY, "title": "Check booking details before payment"}, output_schema=None)
async def booking_summary(
    page_url: Annotated[
        str, Field(description="Address of the checkout page currently open.", max_length=800)
    ],
    operator: Annotated[str, Field(description="Operator as shown on the checkout page.", max_length=120)],
    origin: Annotated[str, Field(description="From city.", max_length=100)],
    destination: Annotated[str, Field(description="To city.", max_length=100)],
    travel_date: Annotated[str, Field(description="Travel date, YYYY-MM-DD.", max_length=30)],
    departure: Annotated[str, Field(description="Departure time, HH:MM.", max_length=12)],
    seats: Annotated[
        list[str], Field(description="Seat numbers selected on the page.", min_length=1, max_length=6)
    ],
    passengers: Annotated[
        list[PassengerInput],
        Field(description="Passengers as typed on the page, one per seat.", min_length=1, max_length=6),
    ],
    contact_mobile: Annotated[str, Field(description="Mobile number entered on the page.", max_length=20)],
    requested_seats: opt_list(
        str, {"type": "string"}, description="Seat numbers the user asked for in chat.", max_length=6
    ) = None,
    boarding_point: opt(str, description="Boarding point chosen.", max_length=150) = None,
    dropping_point: opt(str, description="Dropping point chosen.", max_length=150) = None,
    contact_email: opt(str, description="Email entered, if any.", max_length=120) = None,
    total_fare: opt(float, description="Total amount shown before payment, rupees.", ge=0) = None,
    seat_prices: opt_list(
        float,
        {"type": "number"},
        description="Price of each selected seat, same order as seats.",
        max_length=6,
    ) = None,
    seat_reserved_for: opt_list(
        str, {"type": "string"}, description="Per selected seat: 'female', 'male' or ''.", max_length=6
    ) = None,
    add_ons: opt_list(
        str,
        {"type": "string"},
        description="Paid extras the user chose on the page, with price, e.g. 'Free Cancellation ₹140'. "
        "Empty if none.",
        max_length=8,
    ) = None,
) -> str:
    """Check a filled-in booking on the official site before the user pays.

    Verifies the page is a real booking site, that seats match what the user asked, that every seat
    has one passenger with a valid name/age/gender (and respects women/men-only seats), the mobile and
    email, and that the total has no surprise add-ons. Returns a short summary; open payment only after
    the user replies "confirm" to it. Nothing is booked, paid or stored by this tool.
    """
    try:
        d = parse_travel_date(travel_date)
    except DateError as exc:
        raise ToolError(str(exc)) from exc
    seat_list = [s.strip() for s in seats]
    for name, values in (("seat_prices", seat_prices), ("seat_reserved_for", seat_reserved_for)):
        if values and len(values) != len(seat_list):
            raise ToolError(
                f"{name} must have one entry per seat ({len(seat_list)}), in the same order as seats."
            )
    prices = {s: p for s, p in zip(seat_list, seat_prices or [], strict=False) if p is not None}
    reserved = {
        s: r
        for s, r in zip(seat_list, (normalise_reserved(x) for x in (seat_reserved_for or [])), strict=False)
        if r
    }
    draft = BookingDraft(
        page_url=page_url.strip(),
        operator=operator.strip(),
        origin=origin.strip(),
        destination=destination.strip(),
        travel_date=d,
        departure=departure.strip(),
        seats=seat_list,
        passengers=[Passenger(p.name.strip(), p.age, p.gender, p.seat.strip()) for p in passengers],
        contact_mobile=contact_mobile,
        boarding_point=boarding_point,
        dropping_point=dropping_point,
        contact_email=contact_email,
        total_fare=total_fare,
        seat_prices=prices,
        reserved_for=reserved,
        requested_seats=[s.strip() for s in requested_seats or []],
        add_ons=[a.strip() for a in add_ons or [] if a and a.strip()],
    )
    result = check_draft(draft, datetime.now(IST))
    site = (check(draft.page_url).official_hosts or ["unverified site"])[0]
    out = [
        f"## Your booking: {draft.operator}",
        f"- **{draft.origin} → {draft.destination}**, {human_date(d)}, {draft.departure}",
    ]
    if boarding_point or dropping_point:
        out.append(f"- **Board:** {boarding_point or '?'} · **Get off:** {dropping_point or '?'}")
    for p in draft.passengers:
        out.append(f"- **Seat {p.seat}:** {p.name}, {p.age}, {normalise_gender(p.gender) or p.gender}")
    out.append(
        f"- **Ticket to:** {result.mobile or contact_mobile}"
        + (f", {contact_email}" if contact_email else "")
    )
    out.append(f"- **Add-ons:** {', '.join(draft.add_ons) if draft.add_ons else 'none'}")
    if total_fare is not None:
        out.append(f"- **Fare:** ₹{total_fare:g} (the site may add tax on the payment page)")
    out.append(f"- **Site:** {site}")
    if result.problems:
        out += ["", "**Fix before paying:**"] + [f"- {p}" for p in result.problems]
    if result.warnings:
        out += ["", "**Please check:**"] + [f"- {w}" for w in result.warnings]
    if result.ok:
        terms = "MakeMyTrip" if _is_makemytrip(site) else "the site"
        ask = "If the points above are fine, reply" if result.warnings else "All good? Reply"
        out += [
            "",
            f"{ask} **confirm** and I'll open the payment page (this accepts {terms}'s terms). "
            "You pay there yourself.",
        ]
    elif site == "unverified site":
        out += ["", "_Leave this page; book only on the official site._"]
    else:
        out += ["", "_Fix these on the site first; then I'll check again._"]
    return "\n".join(out)


@_tool(annotations={**_READ_ONLY, "title": "Booking steps for a chosen bus"}, output_schema=None)
async def booking_steps(
    origin: Annotated[str, Field(description="From city.", max_length=100)],
    destination: Annotated[str, Field(description="To city.", max_length=100)],
    travel_date: Annotated[str, Field(description="Travel date, YYYY-MM-DD (IST).", max_length=30)],
    operator: Annotated[str, Field(description="Operator of the chosen bus, as shown.", max_length=120)],
    departure: Annotated[str, Field(description="Departure time of the chosen bus, HH:MM.", max_length=12)],
    seats: Annotated[
        list[str], Field(description="Seat numbers the user wants, if already chosen.", max_length=6)
    ] = [],  # noqa: B006 - read-only default
    site: opt_enum(GuideSite, description="Booking site to use (default redBus).") = None,
    for_traveller: opt(
        bool,
        description="True when you can't control the user's browser (e.g. ChatGPT, Gemini): returns short "
        "steps for the user to follow themselves instead of a script for you.",
    ) = None,
) -> str:
    """Step-by-step script to book the chosen bus on the official site in the user's own browser.

    Use when the user wants to book a bus from rank_buses. A script for the assistant (don't paste it to
    the user): the details to collect and every click in order, with STOP points where the user decides
    (seats, boarding point, permission to enter details, "confirm" on the summary, and payment).
    """
    try:
        d = parse_travel_date(travel_date)
        dep = parse_clock(departure)
    except (DateError, TimeFormatError) as exc:
        raise ToolError(str(exc)) from exc
    resolver = get_resolver()
    origin_res, dest_res = resolver.resolve(origin), resolver.resolve(destination)
    problems = [
        _resolution_problem(label, res)
        for label, res in (("Origin", origin_res), ("Destination", dest_res))
        if not res.ok
    ]
    if problems:
        raise ToolError(" ".join(problems))
    chosen_site: GuideSite = site or "redBus"
    seat_list = [s.strip() for s in seats if s and s.strip()]
    links = {link.platform: link for link in build_links(resolver, origin_res.hub, dest_res.hub, d)}
    start = links.get(chosen_site) if chosen_site != "other" else None
    title = (
        f"## Booking steps: {operator.strip()}, {fmt_clock(dep)}, {origin_res.hub.name} → "
        f"{dest_res.hub.name}, {human_date(d)}"
    )
    if for_traveller:
        out = [title]
        if start:
            note = (
                "opens this route and date"
                if start.kind == "dated_search"
                else "then search your route and date"
            )
            out.append(f"Book on [{start.platform}]({start.url}) ({note}).")
        if d == datetime.now(IST).date() and dep < datetime.now(IST).hour * 60 + datetime.now(IST).minute:
            out.append("**This bus has already left today.** Pick another bus.")
        out += [
            f"{i}. {t}"
            for i, t in enumerate(traveller_steps(chosen_site, operator.strip(), fmt_clock(dep)), 1)
        ]
        return "\n".join(out)
    out = [
        title,
        "_For the assistant: follow these steps; don't paste them to the user. On redBus and MakeMyTrip, use "
        "browser_action / browser_read if you have them, otherwise page_action + read_page on each page, instead "
        "of reading or clicking the page yourself._",
    ]
    if start and start.kind == "dated_search":
        out.append(f"Start here: [{start.platform}]({start.url}) (opens this route and date).")
    elif start:
        out.append(
            f"Start here: [{start.platform}]({start.url}), then search {origin_res.hub.name} → "
            f"{dest_res.hub.name} for {human_date(d)} on the site."
        )
    if d == datetime.now(IST).date() and dep < datetime.now(IST).hour * 60 + datetime.now(IST).minute:
        out.append("**This bus has already left today.** Pick another bus.")
    out += ["", "**Details to collect from the user (only when you reach that step):**"]
    out += [f"- {item}" for item in inputs_for(chosen_site)]
    out += ["", "**Steps** (⏸ = stop and wait for the user):"]
    for i, step in enumerate(
        guide_build(chosen_site, operator.strip(), fmt_clock(dep), seat_list or ["not chosen yet"]), 1
    ):
        out.append(f"{i}. {'⏸ ' if step.stop else ''}{step.text}")
    out += [
        "",
        "**Never:** log in or create an account, enter OTPs, passwords, UPI PIN or card details, add paid "
        "extras the user didn't choose, or pay. Seats are only confirmed when the user pays on the site.",
    ]
    return "\n".join(out)


def _is_makemytrip(site: str | None) -> bool:
    s = (site or "").strip().casefold()
    return "makemytrip" in s or s == "mmt"


# ------------------------------------------------------------------ page tools (option 3)
# The AI host keeps the browser (the user's own tab). These two tools remove the guesswork:
# page_action hands it a tested script for the next step, and read_page turns a captured page
# into the answer for that step. See browser/scripts.py (the scripts) and browser/pages.py (the parsers).

PageActionName = Literal["capture", "open_bus", "select_seats", "choose_points", "decline_addons", "continue"]
PageSite = Literal["redBus", "MakeMyTrip"]

_AFTER_SCRIPT = "Then pass the JSON it returns to read_page."
_REAL_CLICK = (
    "It marks the button: then search the page for 'rm-target' (your find / page-search tool) and click "
    "that match with a real click. Scripted clicks are ignored there."
)


@_tool(annotations={**_READ_ONLY, "title": "Script for the next browser step"}, output_schema=None)
async def page_action(
    action: Annotated[
        PageActionName,
        Field(
            description="capture: read the current page (then call read_page). open_bus: open one bus's seat "
            "map. select_seats: click seats. choose_points: pick boarding and drop points. decline_addons: "
            "say no to every paid extra. continue: the site's next-step button."
        ),
    ],
    site: opt_enum(
        PageSite, description="redBus or MakeMyTrip (needed for every action but capture)."
    ) = None,
    operator: opt(str, description="open_bus: operator name as shown.", max_length=120) = None,
    departure: opt(str, description="open_bus: departure time as shown, HH:MM.", max_length=12) = None,
    arrival: opt(
        str,
        description="open_bus: arrival time as shown, HH:MM (tells apart two buses at one time).",
        max_length=12,
    ) = None,
    seats: opt_list(
        str,
        {"type": "string", "maxLength": 8},
        description="select_seats on redBus: seat numbers, e.g. ['3U'].",
        max_length=6,
    ) = None,
    seat_indexes: opt_list(
        int,
        {"type": "integer", "minimum": 0},
        description="select_seats on MakeMyTrip: the click numbers read_page listed for the chosen seats.",
        max_length=6,
    ) = None,
    boarding: opt(
        str,
        description="choose_points: the boarding point's number from read_page, or its name.",
        max_length=80,
    ) = None,
    dropping: opt(
        str, description="choose_points: the drop point's number from read_page, or its name.", max_length=80
    ) = None,
) -> str:
    """Get a ready-made browser script for the next booking step (redBus, MakeMyTrip).

    Run the returned JavaScript in the user's open tab (your browser's run-JavaScript tool), then do
    what the reply says: usually call read_page with the result, or click the element it marked.
    Saves you writing page code. Never use it to type personal details or payment data.
    """
    if action == "capture":
        js, then = page_scripts.CAPTURE_JS, _AFTER_SCRIPT
    else:
        if site is None:
            raise ToolError("Say which site: redBus or MakeMyTrip.")
        if action == "open_bus":
            if not operator or not departure:
                raise ToolError("open_bus needs the operator and the departure time.")
            js, then = (
                page_scripts.open_bus_js(site, operator, departure, arrival),
                _REAL_CLICK + " Then capture the seat map.",
            )
        elif action == "select_seats":
            if site == "MakeMyTrip":
                if not seat_indexes:
                    raise ToolError("On MakeMyTrip pass seat_indexes (the click numbers read_page listed).")
                js = page_scripts.select_seats_js(site, [], list(seat_indexes))
            else:
                if not seats:
                    raise ToolError("Pass the seat numbers the user chose.")
                js = page_scripts.select_seats_js(site, [x.strip() for x in seats if x.strip()])
            then = (
                "It returns the selected seats; check they're exactly the user's choice. On MakeMyTrip, "
                "'mmt_selected' shows the site's own seat number: use it from now on."
            )
        elif action == "choose_points":
            if not boarding or not dropping:
                raise ToolError("choose_points needs both the boarding and the dropping point.")
            js = page_scripts.choose_points_js(site, boarding, dropping)
            then = "It returns the selected points; check both are the user's choice."
        elif action == "decline_addons":
            js = page_scripts.DECLINE_ADDONS_JS
            then = "Only after the user said no to the paid extras. It lists what it declined."
        else:
            js, then = page_scripts.continue_js(site), _REAL_CLICK + " Then capture the new page."
    return f"Run in the user's tab:\n```js\n{js}\n```\n{then}"


def _points_text(points: list[Point]) -> list[str]:
    out = []
    for kind, title in (("boarding", "Board at"), ("dropping", "Get off at")):
        group = [p for p in points if p.kind == kind]
        if not group:
            continue
        out.append(f"**{title}:**")
        for k, p in enumerate(group, 1):
            when = f"{p.time}" + (f", {p.day}" if p.day else "")
            extra = f" ({p.detail})" if p.detail and p.detail.lower() != p.place.lower() else ""
            flags = (" ✓ selected" if p.selected else "") + (" ⚠ highway/petrol-pump stop" if p.risky else "")
            out.append(f"{k}. {when} · {p.place}{extra}{flags}")
    return out


def _form_gaps(f: FormRead, site: str) -> list[str]:
    """What's missing or unreadable on the passenger page. Anything here blocks the summary,
    so the user is never asked to "confirm" a booking with unknown parts."""
    gaps = []
    if not f.seats:
        gaps.append("seat numbers (not readable on the page)")
    elif f.name_fields and f.name_fields != len(f.seats):
        gaps.append(f"a name box for every seat (page shows {f.name_fields} for {len(f.seats)} seats)")
    if not f.departure or not f.operator:
        gaps.append("the bus and departure time (not readable on the page)")
    for p in f.passengers:
        who = f"seat {p.seat}" if p.seat else "passenger"
        if not p.name.strip():
            gaps.append(f"name ({who})")
        if not str(p.age).strip():
            gaps.append(f"age ({who})")
        if not p.gender:
            gaps.append(f"gender ({who})")
    if not f.mobile:
        gaps.append("mobile number")
    if site == "MakeMyTrip" and not f.email:
        gaps.append("email")
    if site == "redBus" and not f.state:
        gaps.append("state of residence")
    gaps += [f"{name}: add or not" for name, chosen in f.add_ons.items() if chosen is None]
    return gaps


@_tool(annotations={**_READ_ONLY, "title": "Read a captured booking page"}, output_schema=None)
async def read_page(
    capture: Annotated[
        str,
        Field(
            description="Exactly the JSON the capture script returned (page_action action='capture').",
            max_length=150_000,
        ),
    ],
    bus_type: Annotated[BusPreference, Field(description="Results page: what the user wants.")] = "any",
    around_time: opt(str, description="Results page: preferred departure, HH:MM.", max_length=12) = None,
    sort_by: Annotated[SortBy, Field(description="Results page: how to sort.")] = "best",
    operator: opt(str, description="Seat page: the bus's operator, for the heading.", max_length=120) = None,
    departure: opt(str, description="Seat page: the bus's departure time.", max_length=12) = None,
    seats_wanted: opt(int, description="Seat page: how many seats the user needs.", ge=1, le=6) = None,
    preferences: opt_list(
        Preference,
        {"type": "string", "enum": list(get_args(Preference))},
        description="Seat page: window, aisle, front, back, together, lower, upper, cheapest, sleeper, seater.",
        max_length=8,
    ) = None,
    requested_seats: opt_list(
        str,
        {"type": "string", "maxLength": 8},
        description="Passenger page: the seats the user chose, in the site's own numbering.",
        max_length=6,
    ) = None,
    approved_fare: opt(float, description="Payment page: the fare the user confirmed.", ge=0) = None,
) -> str:
    """Read a booking page captured from the user's browser and return what it means for this step.

    Works on redBus and MakeMyTrip. Results page: ranked bus list. Seat page: seat diagram (plus the
    boarding/drop points on MakeMyTrip). Points page: numbered boarding and drop points. Passenger
    page: what's still missing, or the booking summary to confirm. Payment page: the total, the
    hold time and the "Pay here" link. Nothing is stored.
    """
    try:
        c = await _off_loop(load_capture, capture)
    except CaptureError as exc:
        raise ToolError(str(exc)) from exc
    site = site_of(c.url)
    if site is not None and urlsplit(c.url).scheme != "https":
        raise ToolError("This page isn't on a secure (https) address; don't enter details or pay there.")
    if site is None:
        raise ToolError(
            "read_page reads redBus and MakeMyTrip pages only. On other sites, read the page yourself."
        )
    kind = page_kind(c, site)
    route = route_of(c, site)
    if kind == "results":
        return await _read_results(c, site, route, bus_type, around_time, sort_by)
    if kind == "seats":
        return await _read_seats(c, site, operator, departure, seats_wanted, preferences)
    if kind == "points":
        points = await _off_loop(parse_redbus_points, c)
        if not points:
            raise ToolError("No boarding or drop points found on this page.")
        return "\n".join(
            ["## Boarding and drop points", *_points_text(points), "", "Which boarding and drop point?"]
        )
    if kind == "passenger":
        return await _read_form(c, site, route, requested_seats)
    if kind == "payment":
        return await _read_payment(c, site, approved_fare)
    raise ToolError(
        "This page isn't a results, seat, boarding/drop, passenger or payment page. Open one of those, then "
        "capture again."
    )


async def _read_results(
    c: Capture, site: str, route, bus_type, around_time, sort_by, more: Sequence[tuple[Capture, str]] = ()
) -> str:
    """Rank the buses on a results page; `more` adds other sites' pages for the same route and date,
    so the user sees every option in one table (the Book column says where each bus is)."""
    if not route.origin or not route.destination or not route.travel_date:
        raise ToolError("Couldn't tell the route or date from the page address; use rank_buses instead.")
    pages = [(c, site), *more]
    several = len({s for _, s in pages}) > 1
    head: list[str] = []
    listings: list[tuple[str, Bus]] = []
    for page, page_site in pages:
        parser = parse_redbus_results if page_site == "redBus" else parse_mmt_results
        buses, groups, problems = await _off_loop(parser, page)
        who = f"{page_site}: " if several else ""
        total = re.search(r"\b(\d+) buses\b", page.text)
        if total and page_site == "redBus" and int(total.group(1)) > len(buses):
            head.append(
                f"_{who}read the {len(buses)} buses loaded so far of {total.group(1)}. "
                + (
                    "MakeMyTrip's list below is complete; for more on redBus use its filters (Sleeper, AC, time)._"
                    if several
                    else "For all of them, open this route's MakeMyTrip link (it lists every bus), or use redBus's "
                    "filters (Sleeper, AC, time)._"
                )
            )
        if groups:
            head.append(
                f"_{who}government buses grouped on the site: "
                + "; ".join(f"{g.name} ({g.count} buses, from ₹{g.fare_from:g})" for g in groups)
                + ". Open that row to see them._"
            )
        if problems:
            head.append(f"_{who}{len(problems)} listing(s) couldn't be read._")
        listings += [(page_site, b) for b in buses]
    if not listings:
        raise ToolError("No buses could be read from this page. " + " ".join(head))
    if several:
        # The same bus on both sites (same operator, departure and arrival): keep the cheaper listing.
        # Two buses of one operator at the same time on the same site are different buses: both stay.
        def key(b: Bus) -> tuple:
            return (re.sub(r"[^a-z0-9]", "", b.operator.lower()), b.departure, b.arrival)

        first_site = pages[0][1]
        first = {key(b): i for i, (ps, b) in enumerate(listings) if ps == first_site}
        drop: set[int] = set()
        for i, (ps, b) in enumerate(listings):
            j = first.get(key(b)) if ps != first_site else None
            if j is not None:
                drop.add(j if (b.fare or 1e9) < (listings[j][1].fare or 1e9) else i)
        twins = len(drop)
        listings = [x for i, x in enumerate(listings) if i not in drop]
        if twins:
            head.append(f"_{twins} bus(es) are on both sites; the cheaper listing is shown._")
    # MakeMyTrip lists buses leaving just after midnight ("00:50 13 NOV") on the day before's page.
    # Ranked as today's 00:50 they'd look already gone, so they're shown as next-day buses instead.
    day = route.travel_date
    later = [x for x in listings if x[1].day is not None and x[1].day != day.day]
    same = [x for x in listings if x not in later]
    if later and same:
        head.append(
            f"_{len(later)} more bus(es) leave after midnight ({human_date(day + timedelta(days=1))}); "
            "they're on the next day's page._"
        )
    elif later:
        same, day = later, day + timedelta(days=1)
    ranked = await rank_buses(
        origin=route.origin,
        destination=route.destination,
        travel_date=day.isoformat(),
        buses=[
            BusListing(platform=ps, **{k: v for k, v in vars(b).items() if v is not None and k != "day"})
            for ps, b in same
        ],
        bus_type=bus_type,
        sort_by=sort_by,
        around_time=around_time,
    )
    first, _, rest = ranked.partition("\n")
    return "\n".join([first, *head, rest]) if head else ranked


async def read_results_pages(
    captures: Sequence[str], bus_type: str = "any", around_time: str | None = None, sort_by: str = "best"
) -> str | None:
    """One ranked table from several sites' results pages for the same route and date (the first page
    leads). None when the pages aren't all results pages for the same trip."""
    pages = []
    for raw in captures:
        try:
            c = await _off_loop(load_capture, raw)
        except CaptureError:
            return None
        site = site_of(c.url)
        if site is None or urlsplit(c.url).scheme != "https" or page_kind(c, site) != "results":
            return None
        pages.append((c, site, route_of(c, site)))
    (c0, s0, r0), rest = pages[0], pages[1:]
    res = get_resolver()

    def hub(name):
        found = res.resolve(name) if name else None
        return found.hub.name if found and found.ok and not found.fuzzy and not found.assumed else None

    o0, d0 = hub(r0.origin), hub(r0.destination)
    same_trip = [
        (c, s)
        for c, s, r in rest
        if r.travel_date == r0.travel_date and o0 and d0 and (hub(r.origin), hub(r.destination)) == (o0, d0)
    ]
    return await _read_results(c0, s0, r0, bus_type, around_time, sort_by, more=same_trip)


async def _read_seats(c: Capture, site: str, operator, departure, seats_wanted, preferences) -> str:
    if site == "redBus":
        seats, problems = await _off_loop(parse_redbus_seats, c)
        site_numbers = None
    else:
        seats, problems, site_numbers = await _off_loop(parse_mmt_seats, c)
    if not seats:
        raise ToolError("No seats found. Open the bus's seat map, then capture again. " + " ".join(problems))
    text = await explain_seat_map(
        operator=operator or "this bus",
        departure=departure or "",
        seats=[
            SeatInput(
                number=s.number,
                deck=s.deck,
                seat_type=s.seat_type,
                price=s.price,
                status=s.status,
                reserved_for=s.reserved_for,
                x=s.x,
                y=s.y,
            )
            for s in seats
        ],
        seats_wanted=seats_wanted,
        preferences=preferences,
        site=site,
        labels_are_positions=site == "MakeMyTrip",
        front="top",
    )
    out = [text]
    if site == "MakeMyTrip":
        free = [s for s in seats if s.status != "sold"]
        out.append(
            "_For the assistant: seat_indexes (click numbers) for select_seats: "
            + ", ".join(f"{s.number}={s.dom_index}" for s in free)
            + "._"
        )
        if site_numbers:
            out.append(f"Selected on the site: seat **{site_numbers}**.")
            points = await _off_loop(parse_mmt_points, c)
            if points:
                out += ["", *_points_text(points), "", "Which boarding and drop point?"]
    if problems:
        out.append(f"_{len(problems)} seat(s) couldn't be read._")
    return "\n".join(out)


async def _read_form(c: Capture, site: str, route, requested_seats) -> str:
    f = await _off_loop(parse_redbus_form if site == "redBus" else parse_mmt_form, c)
    gaps = _form_gaps(f, site)
    facts = []
    if site == "redBus":
        wa = (
            "on"
            if f.whatsapp
            else "off"
            if f.whatsapp is False
            else "not shown (open Edit on the contact block to check)"
        )
        facts.append(f"State: {f.state or '—'} · WhatsApp updates: {wa}")
    if gaps:
        return "\n".join(
            [
                "## Passenger form",
                *facts,
                "Still needed: " + ", ".join(gaps) + ".",
                "_Ask the user only for these, fill them in, then capture again._",
            ]
        )
    if not route.travel_date or not route.origin or not route.destination:
        raise ToolError("Couldn't tell the route or date from the page address; use booking_summary instead.")
    summary = await booking_summary(
        page_url=c.url,
        operator=f.operator,
        origin=route.origin,
        destination=route.destination,
        travel_date=route.travel_date.isoformat(),
        departure=f.departure,
        seats=f.seats,
        passengers=[
            PassengerInput(name=p.name, age=int(p.age), gender=p.gender or "", seat=p.seat or "")
            for p in f.passengers
        ],
        contact_mobile=f.mobile,
        contact_email=f.email or None,
        boarding_point=f.boarding or None,
        dropping_point=f.dropping or None,
        total_fare=f.amount,
        add_ons=[name for name, chosen in f.add_ons.items() if chosen],
        requested_seats=list(requested_seats) if requested_seats else None,
        seat_reserved_for=[f.reserved.get(x, "") for x in f.seats] if f.reserved else None,
    )
    if facts:
        lines = summary.splitlines()
        at = next((k for k, x in enumerate(lines) if x.startswith("- **Ticket to:**")), 1)
        lines.insert(at + 1, f"- {facts[0]}")
        return "\n".join(lines)
    return summary


async def _read_payment(c: Capture, site: str, approved_fare) -> str:
    pay = await _off_loop(parse_payment, c)
    host = urlsplit(c.url).hostname or site
    out = []
    if pay.total is None:
        out.append("Couldn't read the total; check it on the page before paying.")
    else:
        line = f"**Total: ₹{pay.total:,.2f}**"
        if pay.timer and (m := re.match(r"(\d+):(\d{2})", pay.timer)):
            line += f" · seat held about {int(m.group(1))} min"
        out.append(line)
        if approved_fare and pay.total > approved_fare * 1.06 + 5:
            out.append(
                f"⚠ That's ₹{pay.total - approved_fare:,.2f} more than the confirmed fare (₹{approved_fare:g}); more "
                "than tax. Check what was added before paying."
            )
    out.append(f"**Pay here ({host.removeprefix('www.')}):** {c.url}")
    out.append("_Pay on that page yourself. Never share your OTP or UPI PIN._")
    return "\n".join(out)


# ------------------------------------------------------------------ apps without browser control
@_tool(annotations={**_READ_ONLY, "title": "Rank pasted bus results"}, output_schema=None)
async def rank_pasted_results(
    text: Annotated[
        str,
        Field(
            description="The results page text the user copied from redBus or MakeMyTrip (select all, copy, "
            "paste). Pass it as pasted.",
            max_length=60_000,
        ),
    ],
    origin: Annotated[str, Field(description="Starting city.", max_length=100)],
    destination: Annotated[str, Field(description="Destination city.", max_length=100)],
    travel_date: Annotated[str, Field(description="Travel date, YYYY-MM-DD (India time).", max_length=30)],
    site: opt_enum(PageSite, description="Where it was copied from; detected if left out.") = None,
    bus_type: Annotated[BusPreference, Field(description="What the user wants.")] = "any",
    around_time: opt(str, description="Preferred departure, HH:MM.", max_length=12) = None,
    sort_by: Annotated[SortBy, Field(description="How to sort.")] = "best",
) -> str:
    """Rank buses from a results page the user copied and pasted (redBus or MakeMyTrip).

    For apps that can't open the user's browser (ChatGPT, Gemini): the user opens plan_bus_trip's link,
    copies the whole page and pastes it. Returns the same ranked table as rank_buses.
    """
    which = site or ("MakeMyTrip" if re.search(r"buses found|SELECT SEATS", text) else "redBus")
    if which == "MakeMyTrip":
        c = Capture(url="https://www.makemytrip.com/bus/search", text=text)
        buses, groups, _ = await _off_loop(parse_mmt_results, c)
    else:
        buses, groups, _ = await _off_loop(_parse_pasted_redbus, text)
    if not buses:
        raise ToolError(
            "No buses found in the pasted text. Copy the whole results page (select all, then copy) from "
            "redBus or MakeMyTrip and paste it again, or type the buses you see."
        )
    ranked = await rank_buses(
        origin=origin,
        destination=destination,
        travel_date=travel_date,
        buses=[
            BusListing(platform=which, **{k: v for k, v in vars(b).items() if v is not None}) for b in buses
        ],
        bus_type=bus_type,
        sort_by=sort_by,
        around_time=around_time,
    )
    if groups:
        first, _, rest = ranked.partition("\n")
        note = "; ".join(f"{g.name} ({g.count} buses, from ₹{g.fare_from:g})" for g in groups)
        ranked = (
            f"{first}\n_Government buses grouped on the site: {note}. Open that row to see them._\n{rest}"
        )
    return ranked


_ROUTE_QUERY = re.compile(
    r"^(?:(?:a |the )?bus(?:es)?\s+)?(?:from\s+)?(?P<o>.+?)\s+(?:to|->|→|se)\s+(?P<d>.+?)"
    r"(?:\s+(?:on|for)\s+(?P<when>.+))?$",
    re.I,
)
_WHEN_WORDS = re.compile(
    r"\s+(today|tomorrow|tonight|day after tomorrow|\d{4}-\d{2}-\d{2}|\d{1,2}[-/]\d{1,2}[-/]\d{4})$", re.I
)


def _route_from_query(query: str) -> tuple[str, str, date] | None:
    q = re.sub(
        r"\b(sleeper|ac|non[- ]ac|volvo|seater|night|evening|morning|bus(es)?|tickets?)\b",
        " ",
        query,
        flags=re.I,
    )
    m = _ROUTE_QUERY.match(re.sub(r"\s+", " ", q).strip(" ?."))
    if not m:
        return None
    origin, dest, when = m.group("o"), m.group("d"), m.group("when")
    if not when and (w := _WHEN_WORDS.search(dest)):
        dest, when = dest[: w.start()], w.group(1)
    when = "today" if (when or "").lower() == "tonight" else when
    try:
        d = parse_travel_date(when) if when else today_ist() + timedelta(days=1)
    except DateError:
        return None
    resolver = get_resolver()
    o, t = resolver.resolve(origin), resolver.resolve(dest)
    if not (o.ok and t.ok and o.hub and t.hub) or o.hub.name == t.hub.name:
        return None
    return o.hub.name, t.hub.name, d


@_tool(annotations={**_READ_ONLY, "title": "Search route-master"}, output_schema=None)
async def search(
    query: Annotated[
        str,
        Field(
            description="What to look for, e.g. 'Delhi to Manali tomorrow', 'HRTC', 'refund scam'.",
            max_length=200,
        ),
    ],
) -> str:
    """Search route-master's knowledge: bus routes (booking links), the pay-safely checklist and
    government (RTC) operators. Returns JSON {"results": [{"id", "title", "url"}]}; open one with fetch.
    Standard search tool for ChatGPT connectors and deep research."""
    results = []
    if route := _route_from_query(query):
        o, t, d = route
        link = next(
            iter(build_links(get_resolver(), get_resolver().resolve(o).hub, get_resolver().resolve(t).hub, d))
        )
        results.append(
            {
                "id": f"route:{o}|{t}|{d.isoformat()}",
                "title": f"Buses {o} → {t}, {human_date(d)}",
                "url": link.url,
            }
        )
    words = query.lower()
    if re.search(r"safe|scam|fraud|fake|refund|otp|upi|pay|cheat|report", words):
        results.append(
            {
                "id": "guide:safety",
                "title": "Before you pay: bus booking safety checklist",
                "url": "https://cybercrime.gov.in/",
            }
        )
    for op in load_data("operators.json")["government_operators"]:
        names = [op["short"].lower(), op["name"].lower(), op["state"].lower(), *op.get("names", [])]
        if any(n and (n in words or words in n) for n in names if len(n) > 2) or re.search(
            r"\b(rtc|government|state transport)\b", words
        ):
            url = f"https://{op['domains'][0]}/" if op["domains"] else IRCTC_BUS_URL
            results.append(
                {
                    "id": f"operator:{op['short']}",
                    "title": f"{op['short']}: {op['name']} ({op['state']})",
                    "url": url,
                }
            )
        if len(results) >= 10:
            break
    return json.dumps({"results": results[:10]}, ensure_ascii=False)


@_tool(annotations={**_READ_ONLY, "title": "Open a route-master result"}, output_schema=None)
async def fetch(
    id: Annotated[str, Field(description="An id returned by search.", max_length=200)],  # noqa: A002 - standard name
) -> str:
    """Open one search result by id. Returns JSON {"id", "title", "text", "url", "metadata"}.
    Standard fetch tool for ChatGPT connectors and deep research."""
    kind, _, key = id.partition(":")
    if kind == "route":
        try:
            o, t, d = key.split("|")
        except ValueError as exc:
            raise ToolError("Unknown id; use an id from search.") from exc
        text = await plan_bus_trip(origin=o, destination=t, travel_date=d)
        link = next(
            iter(
                build_links(
                    get_resolver(),
                    get_resolver().resolve(o).hub,
                    get_resolver().resolve(t).hub,
                    parse_travel_date(d),
                )
            )
        )
        doc = {"id": id, "title": f"Buses {o} → {t}", "text": text, "url": link.url, "metadata": {"date": d}}
    elif id == "guide:safety":
        doc = {
            "id": id,
            "title": "Before you pay: bus booking safety checklist",
            "text": extras.SAFETY_CHECKLIST,
            "url": "https://cybercrime.gov.in/",
            "metadata": {},
        }
    elif kind == "operator":
        op = next((x for x in load_data("operators.json")["government_operators"] if x["short"] == key), None)
        if op is None:
            raise ToolError("Unknown operator id; use an id from search.")
        sites = (
            ", ".join(f"https://{d}" for d in op["domains"])
            or "no verified website; book via IRCTC Bus, redBus or MakeMyTrip"
        )
        text = (
            f"{op['name']} ({op['short']}) is the state transport operator of {op['state']}. Official site: {sites}. "
            "Buses also appear grouped under its name on redBus and MakeMyTrip. Check any other site that uses "
            "this name with check_booking_safety; copycat names are common."
        )
        url = f"https://{op['domains'][0]}/" if op["domains"] else IRCTC_BUS_URL
        doc = {
            "id": id,
            "title": f"{op['short']}: {op['name']}",
            "text": text,
            "url": url,
            "metadata": {"state": op["state"]},
        }
    else:
        raise ToolError("Unknown id; use an id from search.")
    return json.dumps(doc, ensure_ascii=False)


# Resources, resource templates and prompts live in extras.py.
extras.register(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "name": "mcp-route-master", "version": __version__})


# ------------------------------------------------------------------------ CLI
def _default_host() -> str:
    """HOST if set; 0.0.0.0 when a cloud platform assigns PORT (it routes traffic in from outside); else local."""
    if os.environ.get("HOST", "").strip():
        return os.environ["HOST"].strip()
    return "0.0.0.0" if os.environ.get("PORT", "").strip() else "127.0.0.1"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp-route-master",
        description="Bus trip planning and booking-safety MCP server for India.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="stdio for local clients (default); http for remote clients.",
    )
    parser.add_argument(
        "--host",
        default=_default_host(),
        help="HTTP bind address. Defaults to 0.0.0.0 when the host platform sets PORT (Render, Fly.io, "
        "Cloud Run, Heroku), else 127.0.0.1. Also read from HOST.",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--path", default=os.environ.get("MCP_PATH", "/mcp"))
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help="Public hostname allowed to reach the HTTP server (repeatable). "
        "Also read from MCP_ALLOWED_HOSTS (comma-separated).",
    )
    parser.add_argument(
        "--show-data-dir",
        action="store_true",
        help="Print the folder for locations.json / pending_locations.json and exit.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.show_data_dir:
        print(data_dir())
        return
    # stdout carries the MCP protocol in stdio mode, so logs must go to stderr.
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "WARNING"),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    get_resolver()  # load data now so a broken data file fails at startup, not mid-chat

    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
        return

    allowed = args.allowed_host or [
        h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]
    # Signed-in mode (Google sign-in at /app/mcp for the Chrome extension) when GOOGLE_CLIENT_ID is set.
    from mcp_route_master.extension.app import build_combined_app, settings_from_env

    cfg = settings_from_env()
    if cfg is not None:
        import uvicorn

        app = build_combined_app(cfg, open_path=args.path, allowed_hosts=allowed or None)
        logging.getLogger(__name__).warning(
            "mcp-route-master %s: open MCP at %s, signed-in MCP at /app/mcp, extension at /ext/ws",
            __version__,
            args.path,
        )
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            lifespan="on",
            ws="websockets-sansio",
            timeout_graceful_shutdown=2,
            log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        )
        return
    mcp.run(
        transport="http",
        host=args.host,
        port=args.port,
        path=args.path,
        stateless_http=True,
        allowed_hosts=allowed or None,
        middleware=build_middleware(),  # optional bearer token and CORS, see http_options.py
        show_banner=False,
    )


if __name__ == "__main__":
    main()
