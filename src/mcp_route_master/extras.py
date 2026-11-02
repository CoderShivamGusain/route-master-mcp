"""MCP resources, resource templates and prompts for route-master.

Tools do the work; these make the server easier to use from any MCP client:

Resources (fixed documents the AI or the user can open):
    route-master://guide/how-it-works        the booking flow and the safety rules (Markdown)
    route-master://guide/safety-checklist    what to check before paying, and where to report fraud
    route-master://guide/supported-sites     what read_page/page_action handle and when it was checked
    route-master://scripts/capture.js        the page-capture script (also given by page_action)
    route-master://data/government-operators state transport (RTC) operators and their verified sites
    route-master://data/booking-platforms    official booking sites and their domains

Resource templates (filled in per request):
    route-master://places/{name}                       how a place name resolves (city, state, aliases)
    route-master://routes/{origin}/{destination}/{date} booking links for a route and date (YYYY-MM-DD)
    route-master://sites/{site}/booking-steps          the click-by-click script for redBus or MakeMyTrip

Prompts (ready-made starting messages, shown as slash commands in some clients):
    find_bus     search, rank and explain buses for a trip
    book_bus     book a chosen bus up to the payment page
    check_scam   check a link, number or message for booking fraud

Hosts that don't support resources or prompts (several chat apps) simply ignore them; every
feature is also reachable through the tools.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastmcp.exceptions import ResourceError

from mcp_route_master.booking.guide import build as guide_build
from mcp_route_master.booking.guide import inputs_for
from mcp_route_master.browser import scripts as page_scripts
from mcp_route_master.common.textutil import load_data
from mcp_route_master.trust.safety import REPORT_LINE

if TYPE_CHECKING:
    from fastmcp import FastMCP

SAFETY_CHECKLIST = f"""# Before you pay for a bus ticket

1. **Book only on the official site or app**: redbus.in, makemytrip.com, goibibo.com, abhibus.com,
   or the state transport (RTC) site. Type the address yourself; don't follow links from calls,
   SMS or WhatsApp.
2. **Check the address bar**: https, and exactly the site's name (not redbus-pay.in, not
   makemytrip.com.offer.in).
3. **Pay only on the site's own payment page.** Never pay to a personal UPI ID, a QR code sent in
   chat, or an "agent".
4. **Never share** your OTP, UPI PIN, card CVV or passwords. Nobody needs them to book or refund.
5. **Refunds never need you to pay or scan anything.** "Scan to receive refund" is a scam.
6. **No remote-access apps** (AnyDesk, TeamViewer, QuickSupport) for "help with a refund".
7. **Check the operator**: rating with enough reviews, live tracking, a proper boarding point
   (bus stand or operator office, not a highway petrol pump if you can avoid it).

{REPORT_LINE}
"""

SUPPORTED_SITES = """# What route-master can read and click

| Site | read_page | page_action | Checked live |
|---|---|---|---|
| redBus | results, seat map, boarding/drop points, passenger form, payment page | open bus*, seats, points, decline extras, continue* | 1-2 Nov 2026 (desktop and mobile layout) |
| MakeMyTrip | results, seat map + pickup/drop, review form | open bus*, seats (by click number), points, decline extras, continue* | 2 Nov 2026 |
| Others (Goibibo, AbhiBus, Cleartrip, FlixBus, IRCTC Bus) | - | - | links only: the AI reads the page itself |

**Apps without browser control (ChatGPT, Gemini):** copy the redBus or MakeMyTrip results page and
paste it; `rank_pasted_results` ranks it. `booking_steps` with `for_traveller` gives steps you follow
yourself.

\\* The script marks the element as "rm-target"; the AI clicks it with a real click, because the
site ignores scripted clicks there.

Never automated: logging in, OTPs, passwords, UPI PIN, card details, payment. The user does those.
If a site changes its layout, read_page reports what it couldn't read and the AI falls back to
reading the page itself.
"""


def register(mcp: FastMCP) -> None:
    """Add the resources, templates and prompts to the server (called once from server.py)."""
    # Imported here: server.py imports this module after defining its tools.
    from mcp_route_master.server import INSTRUCTIONS, get_resolver, plan_bus_trip

    # ---------------------------------------------------------------- resources
    @mcp.resource(
        "route-master://guide/how-it-works",
        name="how-it-works",
        title="How route-master works",
        description="The booking flow (which tool when) and the safety rules.",
        mime_type="text/markdown",
    )
    async def how_it_works() -> str:
        return "# How route-master works\n\n" + INSTRUCTIONS

    @mcp.resource(
        "route-master://guide/safety-checklist",
        name="safety-checklist",
        title="Before you pay: safety checklist",
        description="What to check before paying for a bus ticket in India, and where to report fraud.",
        mime_type="text/markdown",
    )
    async def safety_checklist() -> str:
        return SAFETY_CHECKLIST

    @mcp.resource(
        "route-master://guide/supported-sites",
        name="supported-sites",
        title="Supported booking sites",
        description="Which pages read_page and page_action handle on redBus and MakeMyTrip.",
        mime_type="text/markdown",
    )
    async def supported_sites() -> str:
        return SUPPORTED_SITES

    @mcp.resource(
        "route-master://scripts/capture.js",
        name="capture-script",
        title="Page capture script",
        description="JavaScript that reads the open booking page into JSON for read_page.",
        mime_type="text/javascript",
    )
    async def capture_script() -> str:
        return page_scripts.CAPTURE_JS

    @mcp.resource(
        "route-master://data/government-operators",
        name="government-operators",
        title="Government (RTC) bus operators",
        description="State transport operators known to route-master, with only verified websites.",
        mime_type="application/json",
    )
    async def government_operators() -> str:
        ops = load_data("operators.json")["government_operators"]
        return json.dumps(
            [{k: op[k] for k in ("short", "name", "state", "domains")} for op in ops],
            ensure_ascii=False,
            indent=1,
        )

    @mcp.resource(
        "route-master://data/booking-platforms",
        name="booking-platforms",
        title="Official booking platforms",
        description="Booking sites route-master treats as official, with their domains.",
        mime_type="application/json",
    )
    async def booking_platforms() -> str:
        return json.dumps(load_data("operators.json")["booking_platforms"], ensure_ascii=False, indent=1)

    # ---------------------------------------------------------------- templates
    @mcp.resource(
        "route-master://places/{name}",
        name="place",
        title="Place lookup",
        description="How a place name resolves: city, state, aliases, bus stands, booking-site IDs.",
        mime_type="application/json",
    )
    async def place(name: str) -> str:
        res = get_resolver().resolve(name)
        if not res.ok or res.hub is None:
            options = [h.label for h in res.candidates] or list(res.suggestions)
            raise ResourceError(
                f"'{name}' didn't resolve to one place." + (f" Options: {options}" if options else "")
            )
        h = res.hub
        return json.dumps(
            {
                "query": name,
                "city": h.name,
                "state": h.state,
                "matched_as": res.kind,
                "aliases": list(getattr(h, "aliases", [])),
                "bus_stands": list(getattr(h, "terminals", [])),
                "booking_site_ids": dict(getattr(h, "providers", {})),
            },
            ensure_ascii=False,
            indent=1,
        )

    @mcp.resource(
        "route-master://routes/{origin}/{destination}/{date}",
        name="route",
        title="Route booking links",
        description="Official booking links for a route and date (date as YYYY-MM-DD, India time).",
        mime_type="text/markdown",
    )
    async def route(origin: str, destination: str, date: str) -> str:
        try:
            return await plan_bus_trip(origin=origin, destination=destination, travel_date=date)
        except Exception as exc:  # ToolError etc.: surface as a resource error with the same message
            raise ResourceError(str(exc)) from exc

    @mcp.resource(
        "route-master://sites/{site}/booking-steps",
        name="site-booking-steps",
        title="Booking steps for a site",
        description="Click-by-click booking script for redBus or MakeMyTrip, with the stop points.",
        mime_type="text/markdown",
    )
    async def site_steps(site: str) -> str:
        key = {"redbus": "redBus", "makemytrip": "MakeMyTrip", "mmt": "MakeMyTrip"}.get(site.strip().lower())
        if key is None:
            raise ResourceError("Use redBus or MakeMyTrip.")
        steps = guide_build(key, "<operator>", "<departure>", ["<seats>"])
        out = [f"# Booking steps on {key}", "", "**Details to collect:**"]
        out += [f"- {x}" for x in inputs_for(key)]
        out += ["", "**Steps** (⏸ = wait for the user):"]
        out += [f"{i}. {'⏸ ' if s.stop else ''}{s.text}" for i, s in enumerate(steps, 1)]
        return "\n".join(out)

    # ---------------------------------------------------------------- prompts
    @mcp.prompt(
        name="find_bus",
        title="Find a bus",
        description="Search, rank and explain buses for a trip.",
    )
    async def find_bus(origin: str, destination: str, date: str, preferences: str = "") -> str:
        """origin/destination: places; date: e.g. 'tomorrow' or 2026-11-03; preferences: e.g. 'AC sleeper, night'."""
        prefs = f" Preferences: {preferences}." if preferences.strip() else ""
        return (
            f"Find buses from {origin} to {destination} on {date}.{prefs}\n"
            "Use route-master: plan_bus_trip first (convert the date to YYYY-MM-DD in India time). If you can "
            "use a browser, open the dated link, then page_action(capture) + read_page to rank the real buses; "
            "otherwise give me the links and ask me to copy-paste the results page, then rank it with "
            "rank_pasted_results. Check the best option with check_operator. Keep the answer short: one table "
            "and your pick, then ask which bus I want."
        )

    @mcp.prompt(
        name="book_bus",
        title="Book a bus",
        description="Book a chosen bus on the official site up to the payment page; you pay yourself.",
    )
    async def book_bus(
        operator: str, departure: str, origin: str, destination: str, date: str, site: str = "redBus"
    ) -> str:
        return (
            f"Book the {operator} bus leaving {departure} from {origin} to {destination} on {date}, on {site}.\n"
            "Use route-master: booking_steps for the plan, page_action + read_page on each page. Show me the "
            "seat map and wait for my seat choice; show the boarding/drop points and wait for my choice; ask "
            "before typing my details and ask about each paid extra; show the booking_summary and wait for my "
            "'confirm'. Then open the payment page and give me the total, the hold time and the 'Pay here' "
            "link. If you can't control my browser, call booking_steps with for_traveller=true and give me "
            "those steps instead. Never log in, enter OTPs, passwords, UPI PIN or card details, or pay."
        )

    @mcp.prompt(
        name="check_scam",
        title="Check for booking fraud",
        description="Check a link, phone number, SMS or WhatsApp message about a bus booking or refund.",
    )
    async def check_scam(message: str) -> str:
        return (
            "Is this safe? Check it with route-master's check_booking_safety and tell me plainly what to do:\n\n"
            f"{message}"
        )
