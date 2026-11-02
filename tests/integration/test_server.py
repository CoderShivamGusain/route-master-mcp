"""MCP contract tests through an in-memory FastMCP client."""

from datetime import timedelta

import pytest
from fastmcp import Client

from mcp_route_master.places.dates import today_ist
from mcp_route_master.server import mcp

pytestmark = pytest.mark.anyio
TOMORROW = (today_ist() + timedelta(days=1)).isoformat()


async def call(name, args):
    async with Client(mcp) as client:
        return await client.call_tool(name, args, raise_on_error=False)


async def test_tools_are_listed_with_portable_schemas():
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {
        "plan_bus_trip",
        "rank_buses",
        "explain_seat_map",
        "booking_summary",
        "check_operator",
        "check_booking_safety",
        "booking_steps",
        "page_action",
        "read_page",
        "rank_pasted_results",
        "search",
        "fetch",
    }
    for tool in tools.values():
        assert tool.description and len(tool.description) < 1024
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.output_schema is None  # plain text output works in every host
        schema = tool.input_schema
        assert schema["type"] == "object" and "$ref" not in str(schema)
    for tool in tools.values():
        assert "anyOf" not in str(tool.input_schema) and "null" not in str(tool.input_schema).replace(
            "None", ""
        )
    plan = tools["plan_bus_trip"].input_schema
    assert set(plan["required"]) == {"origin", "destination", "travel_date"}
    assert "ac_sleeper" in plan["properties"]["bus_type"]["enum"]
    assert plan["properties"]["departure_window"]["enum"] == [
        "any",
        "morning",
        "afternoon",
        "evening",
        "night",
    ]


async def test_server_instructions_forbid_inventing_buses():
    async with Client(mcp) as client:
        instructions = client.instructions
    assert "Never invent buses" in instructions


async def test_plan_bus_trip_happy_path():
    r = await call(
        "plan_bus_trip",
        {
            "origin": "Delhi",
            "destination": "Manali",
            "travel_date": TOMORROW,
            "bus_type": "ac_sleeper",
            "departure_window": "night",
        },
    )
    assert not r.is_error
    text = r.content[0].text
    assert "## Delhi → Manali" in text
    assert "https://www.redbus.in/bus-tickets/delhi-to-manali?fromCityId=733" in text
    assert "Semi-Sleeper" in text and "IRCTC Bus" in text
    assert "Seats and fares are on the site" in text
    assert len(text) < 1500  # roughly under 800 tokens


async def test_plan_bus_trip_government_only_lists_state_operators():
    r = await call(
        "plan_bus_trip",
        {
            "origin": "Kashmiri Gate",
            "destination": "Manali",
            "travel_date": TOMORROW,
            "government_only": True,
        },
    )
    text = r.content[0].text
    assert "Government (RTC) buses" in text and "HRTC" in text and "DTC" in text
    assert "boarding point" in text


@pytest.mark.parametrize(
    "args,needle",
    [
        ({"origin": "Delhi", "destination": "Manali", "travel_date": "2020-01-01"}, "past"),
        ({"origin": "Delhi", "destination": "Manali", "travel_date": "someday"}, "YYYY-MM-DD"),
        ({"origin": "Aurangabad", "destination": "Pune", "travel_date": TOMORROW}, "more than one place"),
        (
            {"origin": "Qwertyville", "destination": "Pune", "travel_date": TOMORROW},
            "isn't in the place list",
        ),
        ({"origin": "Delhi", "destination": "New Delhi", "travel_date": TOMORROW}, "both resolve"),
        (
            {"origin": "Delhi to Manali", "destination": "Manali", "travel_date": TOMORROW},
            "more than one place",
        ),
        ({"origin": "Jaunpur", "destination": "Pune", "travel_date": TOMORROW}, "Did you mean"),
    ],
)
async def test_plan_bus_trip_errors_are_tool_errors(args, needle):
    r = await call("plan_bus_trip", args)
    assert r.is_error and needle in r.content[0].text


async def test_invalid_enum_is_rejected():
    r = await call(
        "plan_bus_trip",
        {"origin": "Delhi", "destination": "Manali", "travel_date": TOMORROW, "bus_type": "luxury"},
    )
    assert r.is_error


async def test_unknown_place_is_queued_for_review(isolated_data_dir):
    await call("plan_bus_trip", {"origin": "Qwertyville", "destination": "Pune", "travel_date": TOMORROW})
    assert "qwertyville" in (isolated_data_dir / "pending_locations.json").read_text()


async def test_check_operator():
    r = await call(
        "check_operator",
        {
            "operator_name": "Laxmi Holidays Pvt Ltd",
            "rating": 4.8,
            "review_count": 626,
            "has_live_tracking": True,
        },
    )
    assert not r.is_error and "Strong trust signals" in r.content[0].text
    r = await call("check_operator", {"operator_name": "KSRTC Express Line"})
    assert "Be careful" in r.content[0].text
    r = await call("check_operator", {"operator_name": "X", "rating": 9})
    assert r.is_error


async def test_check_booking_safety():
    r = await call("check_booking_safety", {"content": "pay advance to raju@ybl on whatsapp"})
    text = r.content[0].text
    assert "HIGH RISK" in text and "1930" in text and "cybercrime.gov.in" in text


async def test_state_name_not_repeated_in_operator_label():
    r = await call(
        "plan_bus_trip",
        {"origin": "Bangalore", "destination": "Hyderabad", "travel_date": TOMORROW, "government_only": True},
    )
    text = r.content[0].text
    assert "KSRTC (Karnataka):" in text and "(Karnataka) (Karnataka)" not in text


# ------------------------------------------------------------ booking_steps
async def test_booking_steps_redbus_script_has_stops_and_start_link():
    res = await call(
        "booking_steps",
        {
            "origin": "Delhi",
            "destination": "Manali",
            "travel_date": TOMORROW,
            "operator": "ReoBus Pro",
            "departure": "18:30",
            "seats": ["3", "4"],
        },
    )
    assert not res.is_error
    text = res.content[0].text
    assert "ReoBus Pro" in text and "Delhi → Manali" in text
    assert "redbus.in/bus-tickets/" in text and "fromCityId=733" in text
    assert "planned: 3, 4" in text
    assert (
        text.count("\n") and sum(line.split(". ", 1)[-1].startswith("⏸") for line in text.splitlines()) == 5
    )
    assert "State of residence" in text and "WhatsApp" in text
    assert "Never:" in text and "OTP" in text
    assert "already left" not in text


async def test_booking_steps_schema_and_optional_seats():
    async with Client(mcp) as client:
        tool = {t.name: t for t in await client.list_tools()}["booking_steps"]
    schema = tool.input_schema
    assert set(schema["required"]) == {"origin", "destination", "travel_date", "operator", "departure"}
    assert schema["properties"]["site"]["enum"] == ["redBus", "MakeMyTrip", "other"]
    res = await call(
        "booking_steps",
        {
            "origin": "Delhi",
            "destination": "Manali",
            "travel_date": TOMORROW,
            "operator": "X Travels",
            "departure": "21:00",
        },
    )
    assert not res.is_error and "not chosen yet" in res.content[0].text
    assert "21:00" in res.content[0].text


async def test_booking_steps_makemytrip_labels_seats_by_position():
    res = await call(
        "booking_steps",
        {
            "origin": "Delhi",
            "destination": "Manali",
            "travel_date": TOMORROW,
            "operator": "X Travels",
            "departure": "20:00",
            "seats": ["L1"],
            "site": "MakeMyTrip",
        },
    )
    text = res.content[0].text
    assert not res.is_error and "makemytrip.com/bus/search/" in text and "U1" in text
    assert "⏸" in text


async def test_booking_steps_other_site_has_no_start_link():
    res = await call(
        "booking_steps",
        {
            "origin": "Delhi",
            "destination": "Manali",
            "travel_date": TOMORROW,
            "operator": "HRTC",
            "departure": "19:00",
            "site": "other",
        },
    )
    assert not res.is_error and "Start here" not in res.content[0].text


@pytest.mark.parametrize(
    "args, needle",
    [
        ({"travel_date": "2001-01-01"}, "past"),
        ({"departure": "25:99"}, ""),
        ({"origin": "Qwxzyville"}, "Origin"),
    ],
)
async def test_booking_steps_errors(args, needle):
    base = {
        "origin": "Delhi",
        "destination": "Manali",
        "travel_date": TOMORROW,
        "operator": "X",
        "departure": "20:00",
    }
    res = await call("booking_steps", {**base, **args})
    assert res.is_error and needle in res.content[0].text


async def test_booking_steps_warns_when_bus_already_left_today():
    from datetime import datetime

    from mcp_route_master.places.dates import IST

    now = datetime.now(IST)
    if now.hour == 0 and now.minute < 2:
        pytest.skip("too close to midnight")
    res = await call(
        "booking_steps",
        {
            "origin": "Delhi",
            "destination": "Manali",
            "travel_date": now.date().isoformat(),
            "operator": "X",
            "departure": "00:00",
        },
    )
    assert not res.is_error and "already left" in res.content[0].text
