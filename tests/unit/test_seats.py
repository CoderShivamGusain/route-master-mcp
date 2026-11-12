import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_route_master.booking.seats import (
    Seat,
    layout,
    normalise_kind,
    normalise_reserved,
    normalise_status,
    seat_sort_key,
    suggest,
)
from mcp_route_master.places.dates import today_ist
from mcp_route_master.server import mcp

# Real seat map read from redBus (ReoBus Pro, Delhi -> Manali 22:25, 2 Nov 2026).
FIXTURE = Path(__file__).parent.parent / "fixtures" / "redbus_reobus_seats.json"
REDBUS = json.loads(FIXTURE.read_text())["seats"]


def redbus_seats():
    return [
        Seat(
            s["number"],
            s["deck"],
            normalise_kind(s["seat_type"]),
            normalise_status(s["status"]),
            s["price"],
            normalise_reserved(s["reserved_for"]),
            s["x"],
            s["y"],
        )
        for s in REDBUS
    ]


def test_normalisers():
    assert normalise_status("Sold") == "sold" and normalise_status("booked") == "sold"
    assert normalise_status("available") == "available" and normalise_status(None) == "available"
    assert normalise_kind("Semi-Sleeper") == "semi_sleeper" and normalise_kind("sleeper berth") == "sleeper"
    assert normalise_reserved("berth reserved for female") == "female"
    assert normalise_reserved("reserved for male") == "male" and normalise_reserved("unreserved") is None
    assert sorted(["L10", "L2", "U1"], key=seat_sort_key) == ["L2", "L10", "U1"]


def test_layout_matches_the_real_bus():
    seats = redbus_seats()
    (deck,) = layout(seats)
    assert deck.positioned and deck.rows == 13 and deck.cols == 5
    by = {s.number: s for s in seats}
    assert by["1"].row == 0 and by["1"].window and by["4"].window  # front row, both sides
    assert by["2"].aisle and by["3"].aisle and not by["1"].aisle
    assert by["51"].row == 12 and by["51"].col == 2  # back-row middle seat
    assert deck.aisle_cols == {2}


def test_suggest_two_together_never_across_aisle_or_sold_or_reserved():
    seats = redbus_seats()
    decks = layout(seats)
    picks = suggest(seats, 2, ["window", "front", "together"], decks)
    nums = {p.number for p in picks}
    assert len(picks) == 2 and len({p.row for p in picks}) == 1
    cols = sorted(p.col for p in picks)
    assert cols[1] - cols[0] == 1 and 2 not in range(cols[0], cols[1] + 1)
    assert all(p.status == "available" and p.reserved_for is None for p in picks)
    assert nums in ({"3", "4"}, {"1", "2"})


def test_suggest_cheapest_prefers_back_row_fares():
    seats = redbus_seats()
    picks = suggest(seats, 1, ["cheapest"], layout(seats))
    assert picks[0].price == 547


def test_without_positions_still_lists_and_suggests():
    seats = [Seat("L1", status="available", price=900), Seat("L2", status="sold", price=900)]
    (deck,) = layout(seats)
    assert not deck.positioned
    assert suggest(seats, 1, [], [deck])[0].number == "L1"
    assert suggest(seats, 2, [], [deck]) is None


@pytest.mark.anyio
async def test_explain_seat_map_tool_draws_real_layout():
    async with Client(mcp) as client:
        r = await client.call_tool(
            "explain_seat_map",
            {
                "operator": "ReoBus Pro",
                "departure": "22:25",
                "site": "redBus",
                "seats": REDBUS,
                "seats_wanted": 2,
                "preferences": ["window", "front"],
            },
        )
    text = r.content[0].text
    assert "41 of 53 free · ₹547–₹697" in text
    assert "| 💺 **4** ₹697 | 💺 **3** ₹697 |   | 💺 **2** ₹697 | 💺 **1** ₹697 |" in text  # 2 + aisle + 2
    assert "| ~~16~~ sold | ~~15~~ sold |" in text
    assert "| 💺 **53** ₹547 | 💺 **52** ₹547 | 💺 **51** ₹547 | 💺 **50** ₹547 | 💺 **49** ₹547 |" in text
    assert "**Suggested (2):**" in text


@pytest.mark.anyio
async def test_explain_seat_map_accepts_nulls_and_two_decks():
    seats = [
        {
            "number": "LB1",
            "deck": "lower",
            "seat_type": "sleeper",
            "status": "available",
            "price": 1200,
            "reserved_for": None,
            "x": 0,
            "y": 0,
        },
        {
            "number": "LB2",
            "deck": "lower",
            "seat_type": "sleeper",
            "status": "sold",
            "price": None,
            "reserved_for": None,
            "x": 50,
            "y": 0,
        },
        {
            "number": "UB1",
            "deck": "upper",
            "seat_type": "sleeper",
            "status": None,
            "price": 1100,
            "reserved_for": "ladies",
            "x": None,
            "y": None,
        },
    ]
    async with Client(mcp) as client:
        r = await client.call_tool(
            "explain_seat_map",
            {
                "operator": "X",
                "departure": "21:00",
                "seats": seats,
                "seats_wanted": None,
                "preferences": None,
                "site": None,
            },
        )
    text = r.content[0].text
    assert "**Lower deck**" in text and "**Upper deck**" in text and "🛏 **UB1** ♀ ₹1100" in text


@pytest.mark.anyio
async def test_explain_seat_map_rejects_duplicate_seats():
    async with Client(mcp) as client:
        r = await client.call_tool(
            "explain_seat_map",
            {"operator": "X", "departure": "21:00", "seats": [{"number": "1"}, {"number": "1"}]},
            raise_on_error=False,
        )
    assert r.is_error


TOMORROW = (today_ist() + timedelta(days=1)).isoformat()
GOOD = {
    "page_url": "https://www.redbus.in/bus-tickets/delhi-to-manali?step=PI",
    "operator": "ReoBus Pro",
    "origin": "Delhi",
    "destination": "Manali",
    "travel_date": TOMORROW,
    "departure": "22:25",
    "seats": ["3", "4"],
    "requested_seats": ["3", "4"],
    "passengers": [
        {"name": "Shivam Gusain", "age": 28, "gender": "male", "seat": "3"},
        {"name": "Priya Sharma", "age": 27, "gender": "F", "seat": "4"},
    ],
    "contact_mobile": "+91 98765 43210",
    "contact_email": "traveller@example.com",
    "boarding_point": "ISBT Kashmiri Gate",
    "dropping_point": "Mall Road",
    "total_fare": 1394,
    "seat_prices": [697, 697],
}


async def summary(**changes):
    args = {**GOOD, **changes}
    async with Client(mcp) as client:
        return (await client.call_tool("booking_summary", args, raise_on_error=False)).content[0].text


@pytest.mark.anyio
async def test_booking_summary_good_booking_gets_code():
    text = await summary()
    assert "**Seat 3:** Shivam Gusain, 28, male" in text and "**Seat 4:** Priya Sharma, 27, female" in text
    assert "Ticket to:** 9876543210" in text and "₹1394" in text
    assert "reply **confirm**" in text.lower() and "Fix before paying" not in text
    assert "redbus.in (redBus)" in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "changes,needle",
    [
        ({"page_url": "https://redbus-pay.in/checkout"}, "not a verified official booking site"),
        ({"page_url": "http://www.redbus.in/checkout"}, "not a verified official booking site"),
        (
            {
                "seats": ["3", "5"],
                "passengers": [{**GOOD["passengers"][0]}, {**GOOD["passengers"][1], "seat": "5"}],
            },
            "differ from the seats you asked for",
        ),
        ({"passengers": GOOD["passengers"][:1]}, "2 seat(s) but 1 passenger(s)"),
        (
            {"passengers": [{**GOOD["passengers"][0], "name": "123"}, GOOD["passengers"][1]]},
            "name looks wrong",
        ),
        (
            {"passengers": [{**GOOD["passengers"][0], "gender": "xyz"}, GOOD["passengers"][1]]},
            "not recognised",
        ),
        ({"passengers": [GOOD["passengers"][0], {**GOOD["passengers"][1], "seat": "3"}]}, "same seat"),
        ({"contact_mobile": "12345"}, "10-digit Indian number"),
        ({"contact_email": "not-an-email"}, "Email address looks wrong"),
        ({"seat_reserved_for": ["female", ""]}, "seat 3 is reserved for female"),
    ],
)
async def test_booking_summary_blocks_problems(changes, needle):
    text = await summary(**changes)
    assert (
        needle in text
        and ("Fix these on the site first" in text or "Leave this page" in text)
        and "reply **confirm**" not in text.lower()
    )


@pytest.mark.anyio
async def test_booking_summary_warns_about_add_ons_and_missing_boarding():
    text = await summary(total_fare=1999, boarding_point=None)
    assert "is more than the seats" in text and "Boarding point not shown" in text
    assert "reply **confirm**" in text.lower()  # warnings don't block


@pytest.mark.anyio
async def test_booking_summary_past_date_is_error():
    async with Client(mcp) as client:
        r = await client.call_tool(
            "booking_summary", {**GOOD, "travel_date": "2020-01-01"}, raise_on_error=False
        )
    assert r.is_error


# ---- regressions from code review ---------------------------------------------------------
def test_fractional_and_jittered_positions_keep_the_grid():
    import random

    rnd = random.Random(7)
    seats = redbus_seats()
    for s in seats:
        s.x += rnd.uniform(-1.5, 1.5)
        s.y += rnd.uniform(-1.5, 1.5) + 0.37
    (deck,) = layout(seats)
    assert (deck.rows, deck.cols) == (13, 5)


def test_three_column_bus_finds_aisle_and_never_pairs_across_it():
    # 2+1 sleeper: two berths on the left, aisle, one on the right
    seats = []
    for r in range(6):
        for c, x in enumerate((0, 40, 120)):
            seats.append(Seat(f"L{r * 3 + c + 1}", status="available", price=1000, x=x, y=r * 100))
    (deck,) = layout(seats)
    by = {s.number: s for s in seats}
    assert by["L2"].aisle and by["L3"].aisle and not by["L1"].aisle
    pair = suggest(seats, 2, ["together"], [deck])
    cols = sorted(p.col for p in pair)
    assert cols == [0, 1]  # the two seats on the same side, not across the aisle


def test_mixed_seat_names_without_positions_do_not_crash():
    seats = [Seat("1"), Seat("L1"), Seat("10"), Seat("2")]
    (deck,) = layout(seats)
    assert [s.number for s in deck.seats] == ["1", "2", "10", "L1"]


def test_sideways_map_is_rotated():
    seats = redbus_seats()
    for s in seats:
        s.x, s.y = s.y, s.x  # front of the bus drawn on the left
    (deck,) = layout(seats)
    assert (deck.rows, deck.cols) == (13, 5)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name",
    ["O'Brien", "Anne-Marie", "S. Kumar", "D’Souza", "रवि कुमार", "அருண் குமார்", "Shivam Gusain"],
)
async def test_valid_names_pass(name):
    text = await summary(passengers=[{**GOOD["passengers"][0], "name": name}, GOOD["passengers"][1]])
    assert "name looks wrong" not in text


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["ab---- ''", "Ravi'", "123", "A", "R2D2", "Ravi  Kumar"])
async def test_invalid_names_block(name):
    text = await summary(passengers=[{**GOOD["passengers"][0], "name": name}, GOOD["passengers"][1]])
    assert "name looks wrong" in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    ["https://www.redbus.in/checkout?mobile=9876543210&helpline=1", "https://www.makemytrip.com/bus/review"],
)
async def test_real_checkout_query_strings_do_not_block(url):
    assert "not a verified official" not in await summary(page_url=url)


@pytest.mark.anyio
@pytest.mark.parametrize("url", ["https://www.redbus.in:8443/checkout", "https://redbus.in@evil.com/pay"])
async def test_odd_ports_and_userinfo_block(url):
    assert "not a verified official" in await summary(page_url=url)


@pytest.mark.anyio
async def test_seat_case_and_blank_requests_do_not_false_block():
    text = await summary(
        seats=["l3", "4"],
        requested_seats=["L3", "4", ""],
        passengers=[{**GOOD["passengers"][0], "seat": "L3"}, GOOD["passengers"][1]],
    )
    assert "differ from" not in text and "aren't among" not in text


@pytest.mark.anyio
@pytest.mark.parametrize("gender", ["Mr.", "Mrs.", "Ms.", "MALE"])
async def test_titles_with_dots_are_genders(gender):
    text = await summary(passengers=[{**GOOD["passengers"][0], "gender": gender}, GOOD["passengers"][1]])
    assert "not recognised" not in text


@pytest.mark.anyio
@pytest.mark.parametrize("mobile", ["९८७६५४३२१०", "98765 4321", "+91 5876543210"])
async def test_bad_mobiles_block(mobile):
    assert "10-digit Indian number" in await summary(contact_mobile=mobile)


@pytest.mark.anyio
@pytest.mark.parametrize("mobile", ["+91 98765 43210", "+91-9876543210", "09876543210", "(98765) 43210"])
async def test_good_mobiles_pass(mobile):
    assert "10-digit Indian number" not in await summary(contact_mobile=mobile)


@pytest.mark.anyio
async def test_list_length_mismatch_is_an_error():
    async with Client(mcp) as client:
        r = await client.call_tool(
            "booking_summary", {**GOOD, "seat_reserved_for": ["female"]}, raise_on_error=False
        )
    assert r.is_error and "one entry per seat" in r.content[0].text


def test_bus_that_left_today_is_blocked():
    from datetime import datetime

    from mcp_route_master.booking.checkout import BookingDraft, Passenger, check_draft
    from mcp_route_master.places.dates import IST

    now = datetime(2026, 11, 1, 23, 0, tzinfo=IST)
    draft = BookingDraft(
        "https://www.redbus.in/x",
        "Op",
        "Delhi",
        "Manali",
        now.date(),
        "22:25",
        ["3"],
        [Passenger("Shivam Gusain", 28, "male", "3")],
        "9876543210",
    )
    assert any("left at 22:25" in p for p in check_draft(draft, now).problems)


@pytest.mark.anyio
async def test_blank_seat_number_message():
    async with Client(mcp) as client:
        r = await client.call_tool(
            "explain_seat_map",
            {"operator": "X", "departure": "21:00", "seats": [{"number": " "}, {"number": "2"}]},
            raise_on_error=False,
        )
    assert r.is_error and "has no number" in r.content[0].text


@pytest.mark.anyio
async def test_published_schemas_are_plain():
    import json as _json

    async with Client(mcp) as client:
        for tool in await client.list_tools():
            text = _json.dumps(tool.input_schema)
            assert "anyOf" not in text and "$ref" not in text and '"default": null' not in text, tool.name


@pytest.mark.anyio
async def test_add_ons_seen_on_redbus():
    # Real redBus checkout: 2 seats x Rs 697 = Rs 1394; Free Cancellation Rs 70 per passenger.
    chosen = await summary(total_fare=1534, add_ons=["Free Cancellation ₹140"])
    assert "**Add-ons:** Free Cancellation ₹140" in chosen and "is more than the seats" not in chosen
    sneaky = await summary(total_fare=1470)  # trip guarantee 46 + insurance 30 the user never chose
    assert "is more than the seats" in sneaky and "**Add-ons:** none" in sneaky
