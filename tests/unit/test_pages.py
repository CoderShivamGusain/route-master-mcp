"""Page parsing (browser/pages.py) and the read_page / page_action tools, on captures recorded live."""

import json

import pytest
from fastmcp import Client
from page_fixtures import (
    MMT_FORM,
    MMT_RESULTS,
    MMT_SEATS,
    REDBUS_FORM,
    REDBUS_FORM_SAVED_CONTACT,
    REDBUS_PAYMENT,
    REDBUS_POINTS,
    REDBUS_RESULTS,
    REDBUS_SEATS,
    TRAVEL,
)

from mcp_route_master.browser import pages as P
from mcp_route_master.server import mcp


def cap(fx):
    return P.load_capture(json.dumps(fx))


# ------------------------------------------------------------------ capture handling
def test_capture_accepts_double_encoded_json_and_rejects_junk():
    assert P.load_capture(json.dumps(json.dumps(REDBUS_PAYMENT))).url.endswith("/paymentDetails")
    with pytest.raises(P.CaptureError):
        P.load_capture("not json")
    with pytest.raises(P.CaptureError):
        P.load_capture(json.dumps({"text": "no url"}))
    with pytest.raises(P.CaptureError):
        P.load_capture("x" * (P.MAX_CAPTURE_CHARS + 1))


@pytest.mark.parametrize(
    "fx, site, kind",
    [
        (REDBUS_RESULTS, "redBus", "results"),
        (REDBUS_SEATS, "redBus", "seats"),
        (REDBUS_POINTS, "redBus", "points"),
        (REDBUS_FORM, "redBus", "passenger"),
        (REDBUS_PAYMENT, "redBus", "payment"),
        (MMT_RESULTS, "MakeMyTrip", "results"),
        (MMT_SEATS, "MakeMyTrip", "seats"),
        (MMT_FORM, "MakeMyTrip", "passenger"),
    ],
)
def test_site_and_page_kind(fx, site, kind):
    c = cap(fx)
    assert P.site_of(c.url) == site and P.page_kind(c, site) == kind


def test_lookalike_hosts_are_not_supported_sites():
    assert P.site_of("https://redbus.in.evil.com/x") is None
    assert P.site_of("https://myredbus.in/x") is None
    assert P.site_of("https://www.makemytrip.com.pay-now.in/") is None


def test_route_from_urls():
    assert P.route_of(cap(REDBUS_RESULTS), "redBus").origin == "Delhi"
    r = P.route_of(cap(MMT_FORM), "MakeMyTrip")  # date=...T18:30Z is 2 Nov in India
    assert (r.origin, r.destination, r.travel_date.isoformat()) == ("Dehradun", "Agra", TRAVEL.isoformat())
    assert P._clean_city("Prayagraj(Uttar Pradesh)") == "Prayagraj"
    assert P._clean_city("Allahabad (Prayagraj)") == "Prayagraj"


# ------------------------------------------------------------------ parsers
def test_redbus_cards_take_the_discounted_price_and_skip_ads():
    buses, groups, problems = P.parse_redbus_results(cap(REDBUS_RESULTS))
    by = {b.operator: b for b in buses}
    assert by["Bedi Travels"].fare == 638 and by["Bedi Travels"].departure == "22:15"
    assert by["Deltin Travels Pvt Ltd"].bus_type.startswith("Volvo")  # 'Ad' tag skipped
    assert by["Tiwari and Brothers"].seats_left == 24 and by["ReoBus Pro"].review_count == 658
    assert groups[0].name == "HRTC" and groups[0].count == 9 and not problems


def test_mmt_results_and_government_groups():
    buses, groups, _ = P.parse_mmt_results(cap(MMT_RESULTS))
    assert [b.operator for b in buses][:2] == [
        "RTH STAR BUS",
        "Shri Krishna Travels ( Jai Shree Ganesh Yatra Co.)",
    ]
    assert buses[0].departure == "21:45" and buses[0].arrival == "04:30" and buses[0].fare == 949
    assert buses[2].fare == 1300 and buses[1].has_live_tracking is True
    assert [(g.name, g.count) for g in groups] == [("UPSRTC", 2), ("UTC", 2)]


def test_redbus_seat_reservation_only_counts_on_free_seats():
    seats, _ = P.parse_redbus_seats(cap(REDBUS_SEATS))
    by = {s.number: s for s in seats}
    assert by["1"].reserved_for == "male" and by["2"].reserved_for is None  # sold: just who booked it
    assert by["3U"].status == "selected" and by["14U"].reserved_for == "female"


def test_mmt_seats_trust_the_grey_icon_and_label_by_position():
    seats, problems, site_no = P.parse_mmt_seats(cap(MMT_SEATS))
    by = {s.number: s for s in seats}
    assert by["L4"].status == "sold"  # testid said 'available', icon is greyed
    assert by["U2"].status == "selected" and by["U2"].dom_index == 4
    assert by["U3"].reserved_for == "female" and site_no == "B" and not problems


def test_points_flag_highway_stops_and_selection():
    rb = P.parse_redbus_points(cap(REDBUS_POINTS))
    drop = [p for p in rb if p.kind == "dropping"][0]
    assert drop.risky and drop.day == "03 Nov"
    assert [p.selected for p in rb if p.kind == "boarding"] == [False, False, True]
    mall = [p for p in rb if "Mall" in p.place][0]
    assert mall.detail == "Dehradun-Haridwar road, IIP Mokhampur"  # repeated name trimmed
    mmt = P.parse_mmt_points(cap(MMT_SEATS))
    assert [p.time for p in mmt if p.selected] == ["20:50", "08:00"]


def test_forms():
    rb = P.parse_redbus_form(cap(REDBUS_FORM))
    assert rb.seats == ["3U"] and rb.passengers[0].gender == "male" and rb.state == "Delhi"
    assert rb.whatsapp is False and rb.add_ons == {"Trip Guarantee": False, "Travel Insurance": False}
    assert rb.amount == 3499 and rb.departure == "17:40" and rb.operator == "Tiwari and Brothers"
    saved = P.parse_redbus_form(cap(REDBUS_FORM_SAVED_CONTACT))
    assert saved.mobile == "9876543210" and saved.whatsapp is None  # collapsed text isn't trusted
    mmt = P.parse_mmt_form(cap(MMT_FORM))
    assert mmt.seats == ["B"] and mmt.passengers[0].gender == "male"
    assert mmt.add_ons == {"Free Cancellation": False, "Trip insurance": False} and mmt.amount == 1099


def test_payment():
    p = P.parse_payment(cap(REDBUS_PAYMENT))
    assert p.total == 3673.95 and p.timer == "07:48"


# ------------------------------------------------------------------ tools
async def read(fx, **kw):
    async with Client(mcp) as client:
        r = await client.call_tool("read_page", {"capture": json.dumps(fx), **kw}, raise_on_error=False)
        return r.is_error, r.content[0].text


@pytest.mark.anyio
async def test_read_page_results_ranks_and_notes_groups_and_partial_list():
    err, text = await read(REDBUS_RESULTS, bus_type="sleeper")
    assert not err and "HRTC (9 buses, from ₹895)" in text and "5 buses loaded so far of 104" in text
    assert "| Book |" not in text and "Book on [redBus]" in text


@pytest.mark.anyio
async def test_read_page_seats_and_points():
    err, text = await read(REDBUS_SEATS, operator="Tiwari and Brothers", departure="17:00")
    assert not err and "[3U OK]" in text and "## Seats: Tiwari and Brothers, 17:00 (redBus)" in text
    err, text = await read(MMT_SEATS)
    assert "labels are positions" in text and "U3=6" in text and "seat **B**" in text and "Board at" in text
    err, text = await read(REDBUS_POINTS)
    assert "3. 17:40 · Doiwala Bypass Dehradun ✓ selected ⚠" in text


@pytest.mark.anyio
async def test_read_page_form_summary_or_gaps():
    err, text = await read(REDBUS_FORM, requested_seats=["3U"])
    assert not err and "## Your booking: Tiwari and Brothers" in text and "reply **confirm**" in text.lower()
    assert "State: Delhi · WhatsApp updates: off" in text
    gappy = json.loads(json.dumps(REDBUS_FORM))
    gappy["inputs"] = [i for i in gappy["inputs"] if i["id"] not in ("0_4", "0_201")]
    err, text = await read(gappy)
    assert "Still needed:" in text and "name (seat 3U)" in text and "state of residence" in text
    assert "confirm" not in text.lower()
    err, text = await read(REDBUS_FORM, requested_seats=["4L"])
    assert "differ from the seats you asked for" in text and "reply **confirm**" not in text.lower()


@pytest.mark.anyio
async def test_read_page_payment_handover_flags_extras():
    err, text = await read(REDBUS_PAYMENT, approved_fare=3499)
    assert "**Total: ₹3,673.95** · seat held about 7 min" in text and "Pay here (redbus.in)" in text
    assert "⚠" not in text
    err, text = await read(REDBUS_PAYMENT, approved_fare=3000)
    assert "more than the confirmed fare" in text


@pytest.mark.anyio
async def test_read_page_rejects_other_sites_and_bad_json():
    err, text = await read({"v": 1, "url": "https://redbus-pay.in/checkout", "text": "Pay ₹100"})
    assert err and "redBus and MakeMyTrip pages only" in text
    async with Client(mcp) as client:
        r = await client.call_tool("read_page", {"capture": "{oops"}, raise_on_error=False)
    assert r.is_error and "valid JSON" in r.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args, needle",
    [
        ({"action": "capture"}, "read_page"),
        (
            {"action": "open_bus", "site": "redBus", "operator": "ReoBus Pro", "departure": "22:25"},
            "rm-target",
        ),
        ({"action": "select_seats", "site": "redBus", "seats": ["3U"]}, "Seat number "),
        ({"action": "select_seats", "site": "MakeMyTrip", "seat_indexes": [4]}, "data-testid"),
        ({"action": "choose_points", "site": "redBus", "boarding": "3", "dropping": "1"}, "bp"),
        ({"action": "decline_addons", "site": "redBus"}, "said no"),
        ({"action": "continue", "site": "MakeMyTrip"}, "rm-target"),
    ],
)
async def test_page_action_scripts(args, needle):
    async with Client(mcp) as client:
        r = await client.call_tool("page_action", args, raise_on_error=False)
    assert not r.is_error and "```js" in r.content[0].text and needle in r.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args",
    [
        {"action": "open_bus"},
        {"action": "open_bus", "site": "redBus"},
        {"action": "select_seats", "site": "MakeMyTrip", "seats": ["B"]},
        {"action": "choose_points", "site": "redBus", "boarding": "1"},
    ],
)
async def test_page_action_needs_its_inputs(args):
    async with Client(mcp) as client:
        r = await client.call_tool("page_action", args, raise_on_error=False)
    assert r.is_error


def test_seat_label_with_quote_is_escaped_in_script():
    from mcp_route_master.browser.scripts import select_seats_js

    js = select_seats_js("redBus", ['3U"); alert(1); ("'])
    assert 'const want = ["3U\\"); alert(1); (\\""];' in js  # stays inside a JSON string literal
    assert "CSS.escape(n)" in js


def test_redbus_sold_seat_label_variant_seen_live():
    c = P.load_capture(
        json.dumps(
            {
                "url": "https://www.redbus.in/bus-tickets/delhi-to-manali?step=SL",
                "seats": [
                    {
                        "l": "Seat number 1, lower deck, seat type seater, berth reserved for male, price 1197 rupees,"
                        " availability sold. Seat is not available for selection.",
                        "p": [381, 187],
                    }
                ],
            }
        )
    )
    seats, problems = P.parse_redbus_seats(c)
    assert seats[0].status == "sold" and seats[0].price == 1197 and not problems


# ------------------------------------------------------------------ review findings (v0.5.0)
def _form(fx, **changes):
    data = json.loads(json.dumps(fx))
    data.update(changes)
    return data


@pytest.mark.anyio
async def test_unreadable_seats_or_missing_passengers_never_reach_confirm():
    no_seats = _form(REDBUS_FORM, text=REDBUS_FORM["text"].replace("Passenger 1\nSeat 3U, Upper Deck\n", ""))
    err, text = await read(no_seats)
    assert "seat numbers (not readable" in text and "confirm" not in text.lower()
    two = _form(
        REDBUS_FORM,
        text=REDBUS_FORM["text"].replace(
            "Passenger 1\nSeat 3U, Upper Deck\n",
            "Passenger 1\nSeat 3U, Upper Deck\nPassenger 2\nSeat 4L, Lower Deck\n",
        ),
    )
    err, text = await read(two)
    assert "a name box for every seat" in text or "name (seat 4L)" in text
    assert "confirm" not in text.lower()


@pytest.mark.anyio
async def test_unreadable_trip_header_is_a_gap_not_a_placeholder():
    err, text = await read(_form(REDBUS_FORM, text=REDBUS_FORM["text"].replace("02 Nov · 17:40\n", "")))
    assert "the bus and departure time" in text and "00:00" not in text and "confirm" not in text.lower()


def test_age_with_units_and_gender_per_traveller():
    assert P._age("31 yrs") == "31" and P._age("abc") == "" and P._age(None) == ""
    two = _form(MMT_FORM, text=MMT_FORM["text"].replace("Seat No: B", "Seat No: B, C"), active=["Male"])
    f = P.parse_mmt_form(cap(two))
    assert [p.gender for p in f.passengers] == [None, None]  # one tab chosen for two travellers: don't guess
    f = P.parse_mmt_form(cap(_form(two, active=["Male", "Female"])))
    assert [p.gender for p in f.passengers] == ["male", "female"]


@pytest.mark.anyio
async def test_mmt_men_only_seat_rule_reaches_the_check():
    f = P.parse_mmt_form(cap(MMT_FORM))
    assert f.reserved == {"B": "male"}
    err, text = await read(_form(MMT_FORM, active=["Female"]), requested_seats=["B"])
    assert "reserved for male" in text and "confirm" not in text.lower()


def test_mmt_insurance_found_by_label_not_position():
    f = P.parse_mmt_form(cap(MMT_FORM))
    assert f.add_ons["Trip insurance"] is False
    gst_only = _form(MMT_FORM, inputs=[i for i in MMT_FORM["inputs"] if "Secure" not in i["label"]])
    assert "Trip insurance" not in P.parse_mmt_form(cap(gst_only)).add_ons


def test_mmt_seats_without_positions_and_missing_deck_counts():
    no_pos = _form(
        MMT_SEATS, mmt_seats=[{k: v for k, v in s.items() if k != "p"} for s in MMT_SEATS["mmt_seats"]]
    )
    seats, _, _ = P.parse_mmt_seats(cap(no_pos))
    assert all(s.x is None and s.y is None for s in seats)
    upper_only = _form(MMT_SEATS, text=MMT_SEATS["text"].replace("LOWER BERTH (4)\n", ""))
    seats, problems, _ = P.parse_mmt_seats(cap(upper_only))
    assert sum(s.deck == "upper" for s in seats) == 4 and not problems
    neither = _form(MMT_SEATS, text=MMT_SEATS["text"].replace("LOWER BERTH (4)\nUPPER BERTH (4)\n", ""))
    _, problems, _ = P.parse_mmt_seats(cap(neither))
    assert problems and "deck" in problems[0]


def test_payment_timer_must_be_a_timer_and_pages_by_host():
    fake = P.load_capture(
        json.dumps(
            {"url": "https://www.redbus.in/paymentDetails", "text": "Total Amount ₹3,673.95\nDep 21:30"}
        )
    )
    assert P.parse_payment(fake).timer is None
    word = P.load_capture(
        json.dumps(
            {"url": "https://www.redbus.in/paymentDetails", "text": "Pay ₹100\nSession expires in 06:59"}
        )
    )
    assert P.parse_payment(word).timer == "06:59"
    host = P.load_capture(
        json.dumps({"url": "https://payments.makemytrip.com/ui/checkout", "text": "Pay ₹1"})
    )
    assert P.page_kind(host, "MakeMyTrip") == "payment"


def test_mmt_discounted_fare_is_the_last_price_before_rating():
    text = MMT_RESULTS["text"].replace(
        "21:452 NOV\n06hrs 45mins\n04:303 NOV\n949\n4.7",
        "21:452 NOV\n06hrs 45mins\n04:303 NOV\n1,200\n949\n4.7",
    )
    buses, _, _ = P.parse_mmt_results(cap(_form(MMT_RESULTS, text=text)))
    assert buses[0].fare == 949


@pytest.mark.anyio
async def test_insecure_page_is_refused():
    err, text = await read(_form(REDBUS_PAYMENT, url="http://www.redbus.in/paymentDetails"))
    assert err and "https" in text
