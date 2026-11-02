from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from mcp_route_master.places.links import build_links
from mcp_route_master.places.resolver import Resolver

D = date(2026, 11, 2)


@pytest.fixture(scope="module")
def resolver():
    return Resolver(overrides={})


def links_for(resolver, a, b):
    return {
        link.platform: link
        for link in build_links(resolver, resolver.resolve(a).hub, resolver.resolve(b).hub, D)
    }


def test_redbus_uses_ids_and_redbus_date_format(resolver):
    link = links_for(resolver, "Bangalore", "Hyderabad")["redBus"]
    assert link.kind == "dated_search"
    parts = urlsplit(link.url)
    assert parts.path == "/bus-tickets/bangalore-to-hyderabad"  # redBus slug, not "bengaluru"
    q = parse_qs(parts.query)
    assert q["fromCityId"] == ["122"] and q["toCityId"] == ["124"]
    assert q["onward"] == ["02-Nov-2026"] and q["doj"] == ["02-Nov-2026"]


def test_redbus_without_verified_ids_falls_back_to_homepage(resolver):
    link = links_for(resolver, "Pune", "Goa")["redBus"]
    assert link.kind == "home" and link.url == "https://www.redbus.in/"


def test_makemytrip_dated_search(resolver):
    link = links_for(resolver, "Delhi", "Manali")["MakeMyTrip"]
    assert link.url == "https://www.makemytrip.com/bus/search/Delhi/Manali/02-11-2026"


def test_makemytrip_quotes_multiword_names(resolver):
    link = links_for(resolver, "Thiruvananthapuram", "Chhatrapati Sambhajinagar")["MakeMyTrip"]
    assert " " not in link.url and "Chhatrapati%20Sambhajinagar" in link.url


def test_goibibo_needs_codes(resolver):
    assert links_for(resolver, "Delhi", "Manali")["Goibibo"].url == (
        "https://www.goibibo.com/bus/search?bid=bus-Delhi-Manali-20261102-0-0-0-0-GICC1199-GICC1543"
    )
    assert links_for(resolver, "Pune", "Goa")["Goibibo"].kind == "home"


def test_abhibus_route_page_only_when_known(resolver):
    assert (
        links_for(resolver, "Delhi", "Manali")["AbhiBus"].url
        == "https://www.abhibus.com/buses/108/Delhi-Manali"
    )
    assert links_for(resolver, "Manali", "Delhi")["AbhiBus"].kind == "home"


def test_links_are_https_and_sorted(resolver):
    links = build_links(resolver, resolver.resolve("Delhi").hub, resolver.resolve("Manali").hub, D)
    assert all(link.url.startswith("https://") for link in links)
    order = {"dated_search": 0, "route_page": 1, "home": 2}
    assert [order[link.kind] for link in links] == sorted(order[link.kind] for link in links)


def test_duplicate_named_city_uses_plain_name_on_makemytrip(resolver):
    hub = resolver.resolve("Bilaspur, Chhattisgarh").hub
    link = build_links(resolver, hub, resolver.resolve("Raipur").hub, D)
    mmt = next(lnk for lnk in link if lnk.platform == "MakeMyTrip")
    assert mmt.url == "https://www.makemytrip.com/bus/search/Bilaspur/Raipur/02-11-2026"
