"""MCP resources, resource templates and prompts (extras.py)."""

import json

import pytest
from fastmcp import Client

from mcp_route_master.places.dates import today_ist
from mcp_route_master.server import mcp

pytestmark = pytest.mark.anyio


async def test_lists_resources_templates_and_prompts():
    async with Client(mcp) as c:
        uris = {str(r.uri) for r in await c.list_resources()}
        templates = {t.uri_template for t in await c.list_resource_templates()}
        prompts = {p.name for p in await c.list_prompts()}
    assert uris == {
        "route-master://guide/how-it-works",
        "route-master://guide/safety-checklist",
        "route-master://guide/supported-sites",
        "route-master://scripts/capture.js",
        "route-master://data/government-operators",
        "route-master://data/booking-platforms",
    }
    assert templates == {
        "route-master://places/{name}",
        "route-master://routes/{origin}/{destination}/{date}",
        "route-master://sites/{site}/booking-steps",
    }
    assert prompts == {"find_bus", "book_bus", "check_scam"}


async def read(uri):
    async with Client(mcp) as c:
        return (await c.read_resource(uri))[0].text


async def test_static_resources():
    assert "page_action" in await read("route-master://guide/how-it-works")
    assert "1930" in await read("route-master://guide/safety-checklist")
    assert "CAPTURE" not in (js := await read("route-master://scripts/capture.js")) and "JSON.stringify" in js
    ops = json.loads(await read("route-master://data/government-operators"))
    assert any(o["short"] == "HRTC" for o in ops) and set(ops[0]) == {"short", "name", "state", "domains"}
    platforms = json.loads(await read("route-master://data/booking-platforms"))
    assert {"name": "redBus", "domains": ["redbus.in"]} in platforms


async def test_place_template():
    data = json.loads(await read("route-master://places/Kashmiri Gate"))
    assert (
        data["city"] == "Delhi"
        and data["matched_as"] == "terminal"
        and data["booking_site_ids"]["redbus"]["id"] == 733
    )
    varanasi = json.loads(await read("route-master://places/Banaras"))
    assert varanasi["city"] == "Varanasi" and varanasi["booking_site_ids"]["redbus"]["id"] == 70429
    with pytest.raises(Exception, match="didn't resolve"):
        await read("route-master://places/Qwxzyville")


async def test_route_and_steps_templates():
    from datetime import timedelta

    day = (today_ist() + timedelta(days=3)).isoformat()
    text = await read(f"route-master://routes/Dehradun/Varanasi/{day}")
    assert "dehradun-to-varanasi?fromCityId=777" in text and "toCityId=70429" in text
    with pytest.raises(Exception, match="past"):
        await read("route-master://routes/Delhi/Manali/2001-01-01")
    steps = await read("route-master://sites/redbus/booking-steps")
    assert steps.startswith("# Booking steps on redBus") and "⏸" in steps
    with pytest.raises(Exception, match="redBus or MakeMyTrip"):
        await read("route-master://sites/goibibo/booking-steps")


async def test_prompts():
    async with Client(mcp) as c:
        find = await c.get_prompt(
            "find_bus", {"origin": "Dehradun", "destination": "Varanasi", "date": "tomorrow"}
        )
        book = await c.get_prompt(
            "book_bus",
            {
                "operator": "Tiwari and Brothers",
                "departure": "17:00",
                "origin": "Dehradun",
                "destination": "Varanasi",
                "date": "2026-11-03",
            },
        )
        scam = await c.get_prompt("check_scam", {"message": "Pay 500 to agent@okaxis for refund"})
    assert "plan_bus_trip" in find.messages[0].content.text
    text = book.messages[0].content.text
    assert "on redBus" in text and "never log in" in text.lower() and "'confirm'" in text
    assert (
        "check_booking_safety" in scam.messages[0].content.text
        and "agent@okaxis" in scam.messages[0].content.text
    )
