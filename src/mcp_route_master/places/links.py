"""Booking links for each platform, using only formats tested on the live sites.

A link is one of:
  * "dated_search" - opens the search results for the exact route and date
  * "route_page"   - opens the route; the user picks the date on the site
  * "home"         - opens the platform; the user types the route and date
We never guess IDs: a wrong ID silently shows "0 buses" or another date, which
is worse than an honest homepage link.

The city/route IDs come from `Hub.providers` and `Resolver.routes` (see
`places/resolver.py`). A platform without a verified ID for both ends falls back to
its homepage. MakeMyTrip is the exception: its search URL takes plain city
names, so it always gets a dated link (with a note in case the name differs).
The server only builds URLs; it never requests them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal
from urllib.parse import quote, urlencode

from mcp_route_master.places.dates import goibibo_date, mmt_date, redbus_date
from mcp_route_master.places.resolver import Hub, Resolver

LinkKind = Literal["dated_search", "route_page", "home"]

IRCTC_BUS_URL = "https://www.bus.irctc.co.in/"


@dataclass(frozen=True)
class BookingLink:
    """A link to one booking platform; `note` tells the user what to do on arrival."""

    platform: str
    url: str
    kind: LinkKind
    note: str = ""


def redbus_link(origin: Hub, destination: Hub, d: date) -> BookingLink:
    """Dated redBus search; needs a verified numeric city ID and URL slug for both cities."""
    o, t = origin.providers.get("redbus"), destination.providers.get("redbus")
    if o and t and o.get("id") and t.get("id") and o.get("slug") and t.get("slug"):
        # Same query shape as redBus's own search URLs: the date goes in both "onward" and
        # "doj" (date of journey).
        params = urlencode(
            {
                "fromCityId": o["id"],
                "fromCityName": origin.name,
                "toCityId": t["id"],
                "toCityName": destination.name,
                "onward": redbus_date(d),
                "doj": redbus_date(d),
            }
        )
        url = f"https://www.redbus.in/bus-tickets/{o['slug']}-to-{t['slug']}?{params}"
        return BookingLink("redBus", url, "dated_search")
    return BookingLink(
        "redBus", "https://www.redbus.in/", "home", "Type the cities and pick the date on the site."
    )


def makemytrip_link(origin: Hub, destination: Hub, d: date) -> BookingLink:
    """Dated MakeMyTrip search by city name (an "mmt" provider entry overrides the hub name)."""
    o = origin.providers.get("mmt", origin.name)
    t = destination.providers.get("mmt", destination.name)
    url = f"https://www.makemytrip.com/bus/search/{quote(o)}/{quote(t)}/{mmt_date(d)}"
    return BookingLink("MakeMyTrip", url, "dated_search", "If it shows no city, search it on the site.")


def goibibo_link(origin: Hub, destination: Hub, d: date) -> BookingLink:
    """Dated Goibibo search; needs verified Goibibo city IDs for both cities."""
    o, t = origin.providers.get("goibibo"), destination.providers.get("goibibo")
    if o and t:
        # Goibibo packs the whole search into one "bid" value; the "0-0-0-0" part is copied
        # as-is from the format seen on the live site.
        bid = f"bus-{quote(origin.name)}-{quote(destination.name)}-{goibibo_date(d)}-0-0-0-0-{o}-{t}"
        return BookingLink("Goibibo", f"https://www.goibibo.com/bus/search?bid={bid}", "dated_search")
    return BookingLink(
        "Goibibo", "https://www.goibibo.com/bus/", "home", "Type the cities and pick the date on the site."
    )


def abhibus_link(resolver: Resolver, origin: Hub, destination: Hub) -> BookingLink:
    """AbhiBus route page (no date in the URL); needs a verified per-route ID."""
    route = resolver.route_id("abhibus", origin, destination)
    if route:
        url = f"https://www.abhibus.com/buses/{route}/{quote(origin.name)}-{quote(destination.name)}"
        return BookingLink("AbhiBus", url, "route_page", "Pick your date at the top of the page.")
    return BookingLink(
        "AbhiBus", "https://www.abhibus.com/", "home", "Type the cities and pick the date on the site."
    )


def build_links(resolver: Resolver, origin: Hub, destination: Hub, d: date) -> list[BookingLink]:
    """Best links first: exact dated searches, then route pages, then homepages."""
    links = [
        redbus_link(origin, destination, d),
        makemytrip_link(origin, destination, d),
        goibibo_link(origin, destination, d),
        abhibus_link(resolver, origin, destination),
        BookingLink(
            "Cleartrip",
            "https://www.cleartrip.com/bus",
            "home",
            "Type the cities and pick the date on the site.",
        ),
        BookingLink("FlixBus", "https://www.flixbus.in/", "home", "Only if FlixBus runs this route."),
    ]
    order = {"dated_search": 0, "route_page": 1, "home": 2}
    return sorted(links, key=lambda link: order[link.kind])  # stable: keeps platform order
