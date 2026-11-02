"""Regression tests from the live Dehradun -> Agra MakeMyTrip run (2 Nov 2026)."""

from datetime import timedelta

import pytest
from fastmcp import Client

from mcp_route_master.booking.checkout import looks_like_sample_email, looks_like_sample_mobile
from mcp_route_master.booking.guide import build, inputs_for
from mcp_route_master.places.dates import today_ist
from mcp_route_master.server import mcp

pytestmark = pytest.mark.anyio
TOMORROW = (today_ist() + timedelta(days=1)).isoformat()


async def call(name, args):
    async with Client(mcp) as client:
        return await client.call_tool(name, args, raise_on_error=False)


# --- booking_steps: site-specific inputs and the MakeMyTrip flow
def test_makemytrip_inputs_skip_redbus_only_fields():
    mmt = " ".join(inputs_for("MakeMyTrip"))
    assert "State of residence" not in mmt and "WhatsApp" not in mmt
    assert "email" in mmt and "Free Cancellation" in mmt
    red = " ".join(inputs_for("redBus"))
    assert "State of residence" in red and "WhatsApp" in red


def test_makemytrip_steps_cover_what_the_live_run_needed():
    steps = build("MakeMyTrip", "Shri Krishna Travels", "20:30", ["B"])
    text = " ".join(s.text for s in steps)
    assert "Selected Seats" in text  # read the site's real seat number after clicking
    assert "data-testid" in text and "grey" in text  # testid can claim 'available' for sold berths
    assert "/bus/review" in text and "No I don't need it" in text
    assert "MakeMyTrip's terms" in text  # hand the last CONTINUE to the user
    assert "pickup time as departure" in text
    stops = [s for s in steps if s.stop]
    assert len(stops) == 5 and stops[-1].text.startswith("Click CONTINUE")


async def test_booking_steps_tool_uses_site_inputs():
    res = await call(
        "booking_steps",
        {
            "origin": "Delhi",
            "destination": "Manali",
            "travel_date": TOMORROW,
            "operator": "X",
            "departure": "20:00",
            "site": "MakeMyTrip",
        },
    )
    text = res.content[0].text
    assert not res.is_error and "State of residence" not in text and "MakeMyTrip requires" in text


# --- explain_seat_map: honest labels, berth/seat split, sleeper preference
MIXED = [
    {
        "number": "L1",
        "deck": "lower",
        "seat_type": "sleeper",
        "price": 1299,
        "status": "available",
        "x": 64,
        "y": 39,
    },
    {
        "number": "L2",
        "deck": "lower",
        "seat_type": "seater",
        "price": 799,
        "status": "available",
        "x": 152,
        "y": 39,
    },
    {
        "number": "L3",
        "deck": "lower",
        "seat_type": "seater",
        "price": 799,
        "status": "available",
        "x": 192,
        "y": 39,
    },
    {
        "number": "L4",
        "deck": "lower",
        "seat_type": "seater",
        "price": 799,
        "status": "sold",
        "x": 152,
        "y": 84,
    },
    {
        "number": "L5",
        "deck": "lower",
        "seat_type": "sleeper",
        "price": 1299,
        "status": "sold",
        "x": 64,
        "y": 138,
    },
]


async def test_seat_map_legend_is_honest_for_makemytrip():
    args = {"operator": "X", "departure": "20:30", "seats": MIXED}
    mmt = (await call("explain_seat_map", {**args, "site": "MakeMyTrip"})).content[0].text
    assert "labels are positions" in mmt
    red = (await call("explain_seat_map", {**args, "site": "redBus"})).content[0].text
    assert "labels are positions" not in red


async def test_seat_map_splits_berths_and_seats_and_prefers_a_berth():
    res = await call(
        "explain_seat_map",
        {"operator": "X", "departure": "20:30", "seats": MIXED, "preferences": ["sleeper"]},
    )
    text = res.content[0].text
    assert "1 sleeper berth(s) + 2 seat(s)" in text
    assert "**Suggested (1):** L1" in text


async def test_seat_map_without_mixed_kinds_has_no_split():
    seats = [
        {"number": "1", "seat_type": "seater", "status": "available"},
        {"number": "2", "seat_type": "seater", "status": "sold"},
    ]
    text = (
        (await call("explain_seat_map", {"operator": "X", "departure": "20:30", "seats": seats}))
        .content[0]
        .text
    )
    assert "berth(s)" not in text


# --- rank_buses: mixed seater/sleeper buses get a note
async def test_rank_buses_flags_mixed_seater_sleeper_for_sleeper_request():
    buses = [
        {
            "operator": "Shri Krishna Travels",
            "departure": "20:30",
            "bus_type": "VE A/C Seater / Sleeper (2+1)",
            "fare": 799,
            "platform": "MakeMyTrip",
        },
        {
            "operator": "Parashar Travels",
            "departure": "19:45",
            "bus_type": "Bharat Benz A/C Sleeper (2+1)",
            "fare": 899,
            "platform": "MakeMyTrip",
        },
    ]
    text = (
        (
            await call(
                "rank_buses",
                {
                    "origin": "Dehradun",
                    "destination": "Agra",
                    "travel_date": TOMORROW,
                    "bus_type": "sleeper",
                    "buses": buses,
                },
            )
        )
        .content[0]
        .text
    )
    rows = {line.split("|")[3]: line for line in text.splitlines() if line.startswith("| ") and "·" in line}
    mixed = next(v for k, v in rows.items() if "Shri Krishna" in k)
    plain = next(v for k, v in rows.items() if "Parashar" in k)
    assert "pick a berth" in mixed and "pick a berth" not in plain


# --- booking_summary: sample-looking contact details warn, never block
@pytest.mark.parametrize(
    "mobile, sample",
    [
        ("9876543210", True),
        ("6789012345", True),
        ("9999999999", True),
        ("9837012345", False),
        ("7017234589", False),
    ],
)
def test_sample_mobile(mobile, sample):
    assert looks_like_sample_mobile(mobile) is sample


@pytest.mark.parametrize(
    "email, sample", [("abc@gmail.com", True), ("ram@example.com", True), ("ram.singh@gmail.com", False)]
)
def test_sample_email(email, sample):
    assert looks_like_sample_email(email) is sample


async def test_booking_summary_warns_on_sample_contacts_and_notes_makemytrip_handover():
    res = await call(
        "booking_summary",
        {
            "page_url": "https://www.makemytrip.com/bus/review?from=Dehradun&to=Agra&selectedSeats=B",
            "operator": "Shri Krishna Travels",
            "origin": "Dehradun",
            "destination": "Agra",
            "travel_date": TOMORROW,
            "departure": "20:50",
            "seats": ["B"],
            "requested_seats": ["B"],
            "seat_prices": [1099],
            "seat_reserved_for": ["male"],
            "passengers": [{"name": "Ram Singh", "age": 31, "gender": "male", "seat": "B"}],
            "contact_mobile": "9876543210",
            "contact_email": "abc@gmail.com",
            "boarding_point": "Mall of Dehradun",
            "total_fare": 1099,
        },
    )
    text = res.content[0].text
    assert not res.is_error
    assert "looks like a sample number" in text and "looks like a sample address" in text
    assert "reply **confirm**" in text.lower()  # warnings don't block
    assert "MakeMyTrip's terms" in text


# --- review follow-ups
PAIRS = [  # 2+1 bus: single-side berths, paired seats
    {"number": "L1", "seat_type": "sleeper", "price": 1299, "status": "available", "x": 60, "y": 40},
    {"number": "L2", "seat_type": "seater", "price": 799, "status": "available", "x": 150, "y": 40},
    {"number": "L3", "seat_type": "seater", "price": 799, "status": "available", "x": 190, "y": 40},
    {"number": "L4", "seat_type": "sleeper", "price": 1299, "status": "available", "x": 60, "y": 140},
    {"number": "L5", "seat_type": "seater", "price": 799, "status": "available", "x": 150, "y": 140},
    {"number": "L6", "seat_type": "seater", "price": 799, "status": "available", "x": 190, "y": 140},
]


async def test_sleeper_preference_beats_together_for_two_people():
    args = {"operator": "X", "departure": "20:30", "seats": PAIRS, "front": "top"}
    text = (
        (await call("explain_seat_map", {**args, "seats_wanted": 2, "preferences": ["sleeper"]}))
        .content[0]
        .text
    )
    line = next(x for x in text.splitlines() if x.startswith("**Suggested"))
    assert "L1" in line and "L4" in line and "includes seats" not in text
    text = (
        (await call("explain_seat_map", {**args, "seats_wanted": 3, "preferences": ["sleeper"]}))
        .content[0]
        .text
    )
    assert "includes seats" in text
    text = (
        (await call("explain_seat_map", {**args, "seats_wanted": 2, "preferences": ["seater"]}))
        .content[0]
        .text
    )
    line = next(x for x in text.splitlines() if x.startswith("**Suggested"))
    assert "L1" not in line and "L4" not in line


@pytest.mark.parametrize(
    "extra, positional",
    [
        ({"site": "MakeMyTrip.com"}, True),
        ({"site": "MMT"}, True),
        ({"site": "other", "labels_are_positions": True}, True),
        ({"site": "redBus"}, False),
        ({}, False),
    ],
)
async def test_legend_label_claim(extra, positional):
    text = (
        (await call("explain_seat_map", {"operator": "X", "departure": "20:30", "seats": PAIRS, **extra}))
        .content[0]
        .text
    )
    assert ("labels are positions" in text) is positional


async def test_untyped_seats_are_not_counted_as_seats():
    seats = [
        {"number": "1", "seat_type": "sleeper", "status": "available"},
        {"number": "2", "status": "available"},
        {"number": "3", "status": "available"},
    ]
    text = (
        (await call("explain_seat_map", {"operator": "X", "departure": "20:30", "seats": seats}))
        .content[0]
        .text
    )
    assert "berth(s)" not in text


def test_real_mail_domain_not_flagged():
    assert looks_like_sample_email("ram.singh@email.com") is False


async def test_handover_note_only_on_makemytrip():
    base = {
        "operator": "X",
        "origin": "Delhi",
        "destination": "Manali",
        "travel_date": TOMORROW,
        "departure": "20:00",
        "seats": ["5"],
        "passengers": [{"name": "Ram Singh", "age": 31, "gender": "male", "seat": "5"}],
        "contact_mobile": "9837012345",
    }
    red = (
        (await call("booking_summary", {**base, "page_url": "https://www.redbus.in/bus-tickets/x"}))
        .content[0]
        .text
    )
    mmt = (
        (await call("booking_summary", {**base, "page_url": "https://www.makemytrip.com/bus/review?x=1"}))
        .content[0]
        .text
    )
    assert "MakeMyTrip's terms" in mmt and "the site's terms" in red
