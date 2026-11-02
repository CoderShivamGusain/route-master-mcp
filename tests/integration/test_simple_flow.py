"""Simple confirm (no typed code), payment-link handover and short outputs (Prayagraj test, 2 Nov 2026)."""

import re
from datetime import timedelta

import pytest
from fastmcp import Client

from mcp_route_master.booking.guide import build, inputs_for
from mcp_route_master.places.dates import today_ist
from mcp_route_master.server import mcp

pytestmark = pytest.mark.anyio
TOMORROW = (today_ist() + timedelta(days=1)).isoformat()


async def call(name, args):
    async with Client(mcp) as client:
        return (await client.call_tool(name, args, raise_on_error=False)).content[0].text


SUMMARY = {
    "page_url": "https://www.redbus.in/bus-tickets/dehradun-to-allahabad?fromCityId=777&step=CI",
    "operator": "Tiwari and Brothers",
    "origin": "Dehradun",
    "destination": "Prayagraj",
    "travel_date": TOMORROW,
    "departure": "17:40",
    "seats": ["11U"],
    "requested_seats": ["11U"],
    "seat_prices": [3499],
    "passengers": [{"name": "Ram Singh", "age": 31, "gender": "male", "seat": "11U"}],
    "contact_mobile": "9837012345",
    "contact_email": "ram.singh@gmail.com",
    "boarding_point": "Doiwala Bypass (17:40)",
    "dropping_point": "Andawa Tiraha",
    "total_fare": 3499,
}


async def test_summary_asks_for_plain_confirm_without_a_code():
    text = await call("booking_summary", SUMMARY)
    assert "reply **confirm**" in text.lower()
    assert not re.search(r"\b[0-9A-F]{3}-[0-9A-F]{3}\b", text)
    assert "may add tax" in text and len(text.splitlines()) <= 12


async def test_summary_with_problems_has_no_confirm_prompt():
    text = await call("booking_summary", {**SUMMARY, "page_url": "https://redbus-pay.in/x"})
    assert "Fix before paying" in text and "reply **confirm**" not in text.lower()


@pytest.mark.parametrize("site", ["redBus", "MakeMyTrip", "other"])
def test_every_flow_ends_with_a_payment_link_and_no_code(site):
    text = " ".join(s.text for s in build(site, "X", "17:00", ["1"]))
    assert "<code>" not in text and "'confirm'" in text
    assert "Pay here" in text


def test_redbus_steps_cover_prayagraj_findings():
    text = " ".join(s.text for s in build("redBus", "Tiwari and Brothers", "17:00", ["11U"]))
    assert "tap the bus card" in text and "Fill passenger details" in text
    assert "pre-fill" in text and "/paymentDetails" in text and "time left" in text
    joined = " ".join(inputs_for("redBus"))
    assert "confirm it" in joined and "on by default" not in joined


async def test_plan_dehradun_prayagraj_has_dated_redbus_link_and_is_short():
    text = await call(
        "plan_bus_trip",
        {
            "origin": "Dehradun",
            "destination": "Prayagraj",
            "travel_date": TOMORROW,
            "bus_type": "sleeper",
            "departure_window": "night",
        },
    )
    assert "redbus.in/bus-tickets/dehradun-to-allahabad?fromCityId=777" in text and "toCityId=84832" in text
    assert "See the buses for this date" in text and len(text.splitlines()) <= 14


async def test_rank_single_bus_is_short_and_says_bus():
    text = await call(
        "rank_buses",
        {
            "origin": "Dehradun",
            "destination": "Prayagraj",
            "travel_date": TOMORROW,
            "bus_type": "sleeper",
            "buses": [
                {
                    "operator": "Tiwari and Brothers",
                    "departure": "17:00",
                    "arrival": "08:30",
                    "bus_type": "A/C Seater/Sleeper (2+1)",
                    "fare": 2199,
                    "rating": 3.8,
                    "review_count": 26,
                    "platform": "redBus",
                }
            ],
        },
    )
    assert "· 1 bus (" in text and "1 buses" not in text
    assert "Book on [redBus]" in text and "| Book |" not in text  # one link, not a column
    assert "berths cost more than this fare" in text


async def test_booking_steps_is_marked_for_the_assistant():
    text = await call(
        "booking_steps",
        {
            "origin": "Dehradun",
            "destination": "Prayagraj",
            "travel_date": TOMORROW,
            "operator": "Tiwari and Brothers",
            "departure": "17:00",
        },
    )
    assert "For the assistant" in text and "fromCityId=777" in text


async def test_warnings_soften_the_confirm_prompt_and_addons_none_is_shown():
    text = await call("booking_summary", {**SUMMARY, "contact_mobile": "9876543210"})
    assert "If the points above are fine, reply **confirm**" in text and "**Add-ons:** none" in text


async def test_unverified_page_says_leave():
    text = await call("booking_summary", {**SUMMARY, "page_url": "https://redbus-pay.in/x"})
    assert "Leave this page" in text and "reply **confirm**" not in text.lower()


def test_steps_require_a_clean_summary_and_a_fresh_confirm():
    for site in ("redBus", "MakeMyTrip", "other"):
        text = " ".join(s.text for s in build(site, "X", "17:00", ["1"]))
        assert "no problems" in text and "new 'confirm'" in text
        assert "fare plus" in text  # handover flags anything added on the payment page


async def test_plan_route_pages_are_not_called_dated():
    text = await call("plan_bus_trip", {"origin": "Delhi", "destination": "Manali", "travel_date": TOMORROW})
    dated = text.split("Also (choose the date there)")[0]
    assert "AbhiBus" not in dated and "AbhiBus" in text and "OTP" in text


async def test_rank_shows_book_column_when_bus_seen_on_other_site():
    text = await call(
        "rank_buses",
        {
            "origin": "Delhi",
            "destination": "Manali",
            "travel_date": TOMORROW,
            "buses": [
                {"operator": "A Travels", "departure": "20:00", "platform": "redBus"},
                {"operator": "Flix", "departure": "21:00", "platform": "FlixBus"},
            ],
        },
    )
    assert "| Book |" in text


async def test_sold_out_map_asks_no_question():
    seats = [{"number": "1", "status": "sold"}, {"number": "2", "status": "sold"}]
    text = await call("explain_seat_map", {"operator": "X", "departure": "20:00", "seats": seats})
    assert "sold out" in text and "Which seat" not in text
