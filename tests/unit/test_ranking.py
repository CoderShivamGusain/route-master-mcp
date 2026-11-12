from datetime import date, datetime, timedelta

import pytest
from fastmcp import Client

from mcp_route_master.booking.ranking import Listing, TimeFormatError, parse_clock, rank
from mcp_route_master.places.dates import IST, today_ist
from mcp_route_master.places.links import build_links
from mcp_route_master.places.resolver import Resolver
from mcp_route_master.server import mcp

D = date(2026, 10, 31)
NOW = datetime(2026, 10, 31, 23, 15, tzinfo=IST)


@pytest.fixture(scope="module")
def links():
    r = Resolver(overrides={})
    return build_links(r, r.resolve("Delhi").hub, r.resolve("Manali").hub, D)


def bus(operator, departure, **kw):
    return Listing(operator=operator, departure=departure, **kw)


TONIGHT = [
    bus(
        "KSAPHA TRAVELS",
        "23:58",
        arrival="10:55",
        bus_type="Volvo 9600 A/C Semi Sleeper (2+2)",
        fare=499,
        rating=4.6,
        review_count=463,
        seats_left=26,
        has_live_tracking=True,
    ),
    bus(
        "zingbus plus",
        "23:30",
        arrival="11:00",
        bus_type="Volvo 9600 Multi Axle Semi-Sleeper (2+2)",
        fare=441,
        rating=4.7,
        review_count=137,
        seats_left=11,
    ),
    bus("Malwa Smart Bus", "23:30", bus_type="AC Seater (2+2)", fare=2999, rating=2.0, review_count=0),
    bus("Gone Travels", "22:00", fare=400, rating=4.5, review_count=500),
]


@pytest.mark.parametrize(
    "text,minutes",
    [
        ("23:58", 1438),
        ("9:05", 545),
        ("11:58 PM", 1438),
        ("12:10 am", 10),
        ("12:00 PM", 720),
        ("07.30", 450),
        ("00:50 AM", 50),
    ],
)
def test_parse_clock(text, minutes):
    assert parse_clock(text) == minutes


@pytest.mark.parametrize("text", ["25:00", "13:00 PM", "00:50 PM", "noon", ""])
def test_parse_clock_rejects(text):
    with pytest.raises(TimeFormatError):
        parse_clock(text)


def test_best_ranking_prefers_trusted_and_drops_departed(links):
    ranked, departed, problems = rank(TONIGHT, D, links, now=NOW)
    assert [r.listing.operator for r in ranked][0] == "KSAPHA TRAVELS"
    assert ranked[-1].listing.operator == "Malwa Smart Bus"
    assert [b.operator for b in departed] == ["Gone Travels"]
    assert not problems
    ks = ranked[0]
    assert ks.duration_min == 657 and "semi-sleeper (reclining seat)" in ks.notes


def test_tight_departure_is_flagged_and_sorted_after_catchable(links):
    ranked, _, _ = rank(TONIGHT, D, links, sort_by="earliest", now=NOW)
    zing = next(r for r in ranked if r.listing.operator == "zingbus plus")
    assert zing.tight and "leaves in 15 min" in zing.notes
    assert ranked[0].listing.operator == "KSAPHA TRAVELS"  # first bus you can realistically catch


def test_future_date_keeps_all_and_sorts_by_time(links):
    ranked, departed, _ = rank(TONIGHT, D + timedelta(days=1), links, sort_by="earliest", now=NOW)
    assert not departed and [r.listing.departure for r in ranked] == ["22:00", "23:30", "23:30", "23:58"]


def test_preference_mismatch_sorts_last(links):
    listings = [
        bus("A", "21:00", bus_type="A/C Sleeper (2+1)", rating=4.0, review_count=200),
        bus("B", "20:00", bus_type="Volvo A/C Semi Sleeper (2+2)", rating=4.9, review_count=900),
    ]
    ranked, _, _ = rank(listings, D + timedelta(days=1), links, preference="sleeper", now=NOW)
    assert ranked[0].listing.operator == "A"
    assert ranked[1].level == 1 and "semi-sleeper (reclining seat)" in ranked[1].notes  # close substitute


def test_link_follows_platform_seen(links):
    listings = [
        bus("A", "21:00", platform="redBus"),
        bus("B", "21:00", platform="MakeMyTrip"),
        bus("C", "21:00", platform="Cleartrip"),
        bus("D", "21:00"),
    ]
    ranked, _, _ = rank(listings, D + timedelta(days=1), links, now=NOW)
    by = {r.listing.operator: r.link.platform for r in ranked}
    assert by == {"A": "redBus", "B": "MakeMyTrip", "C": "redBus", "D": "redBus"}  # no dated Cleartrip link


def test_bad_rows_are_reported_not_fatal(links):
    ranked, _, problems = rank(
        [bus("X", "99:99"), bus("Y", "21:00", rating=9)] + TONIGHT[:1], D, links, now=NOW
    )
    assert len(ranked) == 1 and len(problems) == 2


TOMORROW = (today_ist() + timedelta(days=1)).isoformat()


@pytest.mark.anyio
async def test_rank_buses_tool():
    payload = [
        {
            "operator": "KSAPHA TRAVELS",
            "departure": "23:58",
            "arrival": "10:55",
            "bus_type": "Volvo 9600 A/C Semi Sleeper (2+2)",
            "fare": 499,
            "rating": 4.6,
            "review_count": 463,
            "seats_left": 26,
            "has_live_tracking": True,
            "platform": "MakeMyTrip",
        },
        {
            "operator": "Malwa | Smart",
            "departure": "11:30 PM",
            "fare": 2999,
            "rating": 2.0,
            "review_count": 0,
        },
    ]
    async with Client(mcp) as client:
        r = await client.call_tool(
            "rank_buses",
            {
                "origin": "Delhi",
                "destination": "Manali",
                "travel_date": TOMORROW,
                "buses": payload,
                "bus_type": "sleeper",
            },
        )
    text = r.content[0].text
    assert "**Top pick:** KSAPHA TRAVELS, 23:58" in text
    assert "None of these is clearly sleeper" in text
    assert "https://www.makemytrip.com/bus/search/Delhi/Manali/" in text
    assert "Malwa / Smart" in text  # pipe can't break the table
    assert "find KSAPHA TRAVELS 23:58" in text  # mixed sites: per-row links


@pytest.mark.anyio
async def test_rank_buses_says_when_nothing_leaves_near_requested_time():
    payload = [
        {
            "operator": "City Land Travels",
            "departure": "21:30",
            "bus_type": "Volvo A/C Semi Sleeper (2+2)",
            "rating": 4.1,
            "review_count": 233,
        }
    ]
    async with Client(mcp) as client:
        r = await client.call_tool(
            "rank_buses",
            {
                "origin": "Dehradun",
                "destination": "Manali",
                "travel_date": TOMORROW,
                "buses": payload,
                "around_time": "9:00 AM",
            },
        )
        bad = await client.call_tool(
            "rank_buses",
            {
                "origin": "Dehradun",
                "destination": "Manali",
                "travel_date": TOMORROW,
                "buses": payload,
                "around_time": "morning",
            },
            raise_on_error=False,
        )
    assert "No bus leaves near 09:00; the closest is City Land Travels at 21:30" in r.content[0].text
    assert bad.is_error


@pytest.mark.anyio
async def test_rank_buses_rejects_empty_and_garbage():
    async with Client(mcp) as client:
        empty = await client.call_tool(
            "rank_buses",
            {"origin": "Delhi", "destination": "Manali", "travel_date": TOMORROW, "buses": []},
            raise_on_error=False,
        )
        junk = await client.call_tool(
            "rank_buses",
            {
                "origin": "Delhi",
                "destination": "Manali",
                "travel_date": TOMORROW,
                "buses": [{"operator": "X", "departure": "late"}],
            },
            raise_on_error=False,
        )
    assert empty.is_error and junk.is_error and "couldn't be read" not in empty.content[0].text


def test_unknown_type_does_not_beat_described_alternatives(links):
    listings = [
        bus("Good", "23:58", bus_type="Volvo A/C Semi Sleeper (2+2)", rating=4.6, review_count=463),
        bus("Unknown", "23:30", rating=2.0, review_count=0),
    ]
    ranked, _, _ = rank(listings, D + timedelta(days=1), links, preference="sleeper", now=NOW)
    assert ranked[0].listing.operator == "Good"
    listings.append(bus("Real sleeper", "22:00", bus_type="A/C Sleeper (2+1)", rating=4.0, review_count=150))
    ranked, _, _ = rank(listings, D + timedelta(days=1), links, preference="sleeper", now=NOW)
    assert [r.listing.operator for r in ranked] == ["Real sleeper", "Good", "Unknown"]


def test_bus_leaving_in_minutes_counts_as_gone(links):
    now = datetime(2026, 10, 31, 23, 29, tzinfo=IST)
    listings = [
        bus("Now", "23:30", rating=4.7, review_count=500),
        bus("Soon", "23:45", rating=4.0, review_count=200),
        bus("Later", "23:58", rating=4.6, review_count=463),
    ]
    ranked, departed, _ = rank(listings, D, links, sort_by="earliest", now=now)
    assert [b.operator for b in departed] == ["Now"]
    assert [r.listing.operator for r in ranked] == ["Soon", "Later"]
    assert all(r.tight for r in ranked)


DEHRADUN_MANALI = [
    bus(
        "City Land Travels",
        "21:30",
        arrival="07:20",
        bus_type="Volvo A/C Semi Sleeper (2+2)",
        fare=971,
        rating=4.1,
        review_count=233,
        seats_left=29,
        has_live_tracking=True,
    ),
    bus(
        "HRTC",
        "15:00",
        arrival="07:00",
        bus_type="Ordinary 3+2 Non AC Seater",
        fare=925,
        rating=4.1,
        seats_left=36,
    ),
    bus(
        "HRTC",
        "20:00",
        arrival="10:00",
        bus_type="Himsuta AC Seater Volvo/Scania 2+2",
        fare=1852,
        rating=4.1,
        review_count=20,
        seats_left=34,
    ),
]


def test_semi_sleeper_beats_seaters_when_a_sleeper_was_asked(links):
    """Live test, Dehradun -> Manali: seaters (one a 16-hour 3+2 ordinary) were ranked above semi-sleepers."""
    ranked, _, _ = rank(DEHRADUN_MANALI, D + timedelta(days=2), links, preference="sleeper", now=NOW)
    assert ranked[0].listing.operator == "City Land Travels"
    assert ranked[0].level == 1 and all(r.level == 3 for r in ranked[1:])
    assert "not sleeper" in ranked[-1].notes


def test_ac_sleeper_request_does_not_accept_non_ac_semi_sleeper(links):
    listings = [
        bus("A", "21:00", bus_type="Non AC Semi Sleeper (2+2)"),
        bus("B", "21:00", bus_type="A/C Semi Sleeper (2+2)"),
    ]
    ranked, _, _ = rank(listings, D + timedelta(days=1), links, preference="ac_sleeper", now=NOW)
    assert [(r.listing.operator, r.level) for r in ranked] == [("B", 1), ("A", 3)]


def test_around_time_prefers_nearby_departures(links):
    listings = [
        bus("Night", "20:00", rating=4.6, review_count=500),
        bus("Morning", "09:30", rating=4.4, review_count=400),
    ]
    ranked, _, _ = rank(listings, D + timedelta(days=1), links, now=NOW, around_time="09:00")
    assert ranked[0].listing.operator == "Morning"
    assert not any("your time" in n for n in ranked[1].notes)  # far-off buses aren't nagged per row


def test_far_target_time_does_not_reorder_by_clock(links):
    """Live test: for '9 am' with only night buses, a weak 23:00 bus beat a better 21:30 one."""
    listings = [
        bus("Weak", "23:00", bus_type="Volvo A/C Semi Sleeper (2+2)", fare=709, rating=4.1, review_count=0),
        bus(
            "Better",
            "21:30",
            bus_type="Volvo A/C Semi Sleeper (2+2)",
            fare=971,
            rating=4.1,
            review_count=233,
            has_live_tracking=True,
        ),
    ]
    ranked, _, _ = rank(
        listings, D + timedelta(days=2), links, preference="sleeper", now=NOW, around_time="09:00"
    )
    assert ranked[0].listing.operator == "Better"


def test_near_target_time_gets_a_note(links):
    ranked, _, _ = rank(
        [bus("A", "11:00", rating=4.5, review_count=300)],
        D + timedelta(days=1),
        links,
        now=NOW,
        around_time="09:00",
    )
    assert "2h after your time" in ranked[0].notes


@pytest.mark.anyio
async def test_rank_buses_uses_verified_per_bus_links_only():
    payload = [
        {
            "operator": "FlixBus",
            "departure": "21:00",
            "rating": 4.6,
            "review_count": 300,
            "booking_url": "https://www.flixbus.in/search?rideId=abc123",
        },
        {
            "operator": "Fake Travels",
            "departure": "21:10",
            "rating": 4.6,
            "review_count": 300,
            "booking_url": "https://flixbus-booking.in/ride/1",
        },
        {
            "operator": "Disguised",
            "departure": "21:20",
            "rating": 4.6,
            "review_count": 300,
            "booking_url": "https://redbus.in@evil.com/seat",
        },
        {
            "operator": "Plain http",
            "departure": "21:30",
            "rating": 4.6,
            "review_count": 300,
            "booking_url": "http://www.redbus.in/x",
        },
    ]
    async with Client(mcp) as client:
        r = await client.call_tool(
            "rank_buses",
            {"origin": "Delhi", "destination": "Manali", "travel_date": TOMORROW, "buses": payload},
        )
    text = r.content[0].text
    assert "[This bus](https://www.flixbus.in/search?rideId=abc123)" in text
    for bad in ("flixbus-booking.in", "evil.com", "http://www.redbus.in/x"):
        assert bad not in text
    assert text.count("wasn't an official site") == 3


@pytest.mark.anyio
async def test_rank_buses_accepts_explicit_nulls():
    """Models often send null for unknown fields; nested models must accept it."""
    payload = [
        {
            "operator": "X Travels",
            "departure": "21:00",
            "arrival": None,
            "bus_type": None,
            "fare": None,
            "rating": 4.5,
            "review_count": 300,
            "seats_left": None,
            "has_live_tracking": None,
            "boarding_point": None,
            "platform": None,
            "booking_url": None,
        }
    ]
    async with Client(mcp) as client:
        r = await client.call_tool(
            "rank_buses",
            {
                "origin": "Delhi",
                "destination": "Manali",
                "travel_date": TOMORROW,
                "buses": payload,
                "around_time": None,
            },
            raise_on_error=False,
        )
    assert not r.is_error


def test_around_time_lists_buses_near_it_first(links):
    """Live ChatGPT test, 12 Nov 2026 ("around 9 am"): better-rated evening buses filled the top 10
    and pushed out the morning ones."""
    listings = [bus(f"Night {h}", f"{h}:00", rating=4.9, review_count=900) for h in range(17, 24)]
    listings += [
        bus("Early", "06:00", rating=3.0, review_count=10),
        bus("Morning", "09:30", rating=3.2, review_count=20),
        bus("Noon", "12:00", rating=2.9, review_count=5),
    ]
    ranked, _, _ = rank(listings, D + timedelta(days=1), links, now=NOW, around_time="09:00")
    assert [r.listing.operator for r in ranked[:3]] == ["Morning", "Early", "Noon"]
    assert all(r.near for r in ranked[:3]) and not any(r.near for r in ranked[3:])
    ranked, _, _ = rank(listings, D + timedelta(days=1), links, now=NOW)
    assert ranked[0].listing.operator.startswith("Night") and ranked[0].near is None


def test_near_time_never_lifts_a_clear_mismatch_above_the_asked_type(links):
    listings = [
        bus("Seater", "09:15", bus_type="Bharat Benz Non A/C Seater (3+2)", rating=4.8, review_count=900),
        bus("Sleeper near", "11:00", bus_type="A/C Sleeper (2+1)", rating=3.5, review_count=50),
        bus("Sleeper far", "21:00", bus_type="A/C Sleeper (2+1)", rating=4.8, review_count=900),
    ]
    ranked, _, _ = rank(
        listings, D + timedelta(days=1), links, preference="sleeper", now=NOW, around_time="09:00"
    )
    assert [r.listing.operator for r in ranked] == ["Sleeper near", "Sleeper far", "Seater"]
