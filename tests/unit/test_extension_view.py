"""The drawable view (MCP Apps) behind browser_open / browser_read / browser_action."""

import json
from pathlib import Path

import pytest
from page_fixtures import MMT_SEATS, REDBUS_FORM, REDBUS_RESULTS, REDBUS_SEATS

from mcp_route_master.extension.view import VIEW_MIME, VIEW_URI, page_view, view_html
from mcp_route_master.server import read_page


async def _view(fx, **read):
    text = await read_page(capture=json.dumps(fx), **read)
    return page_view(json.dumps(fx), text, read.get("seats_wanted"), read.get("preferences")), text


@pytest.mark.anyio
async def test_seat_view_has_every_seat_on_a_grid():
    view, text = await _view(REDBUS_SEATS, operator="ReoBus Pro", departure="22:25")
    assert view["kind"] == "seats" and view["title"] == "Seats: ReoBus Pro, 22:25 (redBus)"
    seats = {(d["deck"], s["n"]): s for d in view["decks"] for s in d["seats"]}
    assert (
        len(seats) == 12
        and seats[("lower", "4L")]["s"] == "available"
        and seats[("lower", "4L")]["p"] == 3999
    )
    assert seats[("upper", "3U")]["s"] == "selected" and seats[("upper", "14U")]["f"] == "F"
    assert [s["n"] for s in seats.values() if s["pick"]] == ["4L"]  # same suggestion as the text
    assert "**Suggested (1):** 4L" in text
    assert all(isinstance(s["r"], int) and isinstance(s["c"], int) for s in seats.values())


@pytest.mark.anyio
async def test_mmt_seat_view_says_labels_are_positions():
    view, _ = await _view(MMT_SEATS)
    assert view["kind"] == "seats" and view["positions"] is True
    assert {d["deck"] for d in view["decks"]} == {"lower", "upper"}


@pytest.mark.anyio
async def test_bus_view_keeps_every_row_of_the_table():
    view, text = await _view(REDBUS_RESULTS, bus_type="sleeper")
    rows = view["sections"][0]["rows"]
    assert view["kind"] == "buses" and len(rows) == text.count("\n| ") - 1  # minus the header row
    assert rows[0]["n"] == 1 and "→" in rows[0]["time"] and rows[0]["fare"].startswith("₹")
    assert any("HRTC" in n for n in view["sections"][0]["notes"])


@pytest.mark.anyio
async def test_no_view_for_forms_or_junk():
    text = await read_page(capture=json.dumps(REDBUS_FORM), requested_seats=["3U"])
    assert page_view(json.dumps(REDBUS_FORM), text) is None
    assert page_view("not json", "x") is None


def test_view_page_is_self_contained():
    html = view_html()
    assert VIEW_URI.startswith("ui://") and VIEW_MIME == "text/html;profile=mcp-app"
    assert "ui/notifications/tool-result" in html and "innerHTML" not in html
    assert "http://" not in html and "https://" not in html  # no outside scripts, fonts or images


FLIXBUS = (Path(__file__).resolve().parents[1] / "fixtures" / "mmt_flixbus_seats.json").read_text(
    encoding="utf-8"
)


@pytest.mark.anyio
async def test_mixed_deck_says_which_side_is_single_and_what_is_a_berth():
    """User, 12 Nov 2026: the map didn't show sleeper vs chair, or which side has 1 and which 2."""
    text = await read_page(capture=FLIXBUS, operator="FlixBus", departure="18:15")
    assert "**Lower deck** — left: single sleeper berths (window) · right: 2 seats side by side" in text
    assert (
        "**Upper deck** — left: single sleeper berths (window) · right: 2 sleeper berths side by side" in text
    )
    assert "| 🛏 **L1** ₹398 |   | ~~L2~~ sold | ~~L3~~ sold |" in text  # berth | aisle | 2 seats
    assert "| ⋮ |   | 💺 **L4** ♀ ₹232 | ~~L5~~ sold |" in text  # the berth is two seats long
    assert "🛏 sleeper berth" in text and "each free seat has its price" in text
    assert "L1 (sleeper berth, single, window, lower deck" in text
    view = page_view(FLIXBUS, text)
    lower = view["decks"][0]
    assert lower["plan"].startswith("left: single sleeper berths") and lower["aisle_after"] == 0
    seats = {s["n"]: s for s in lower["seats"]}
    assert seats["L1"]["type"] == "sleeper berth" and seats["L1"]["span"] == 2 and seats["L1"]["single"]
    assert seats["L7"]["type"] == "seat" and seats["L7"]["span"] == 1 and seats["L7"]["side"] == "right"
    # Row by row with the aisle marked, so an app drawing its own grid keeps the 1 + 2 split.
    assert lower["layout"] == "1 + 2"
    assert [c if isinstance(c, str) or c is None else c["n"] for c in lower["grid"][0]] == [
        "L1",
        "AISLE",
        "L2",
        "L3",
    ]
    assert lower["grid"][1][0] is None and lower["grid"][1][1] == "AISLE"


@pytest.mark.anyio
async def test_mmt_stops_come_with_the_first_seat_map():
    """Live ChatGPT test, 13 Nov 2026: the stops were only listed after a seat was selected, and the
    AI told the user they hadn't loaded."""
    text = await read_page(capture=FLIXBUS)  # no seat selected yet
    assert "**Board at:**" in text and "Transport Nagar" in text
    assert "**Get off at:**" in text and "ISBT Kashmiri Gate" in text
    assert "After the seat, choose a boarding and drop point." in text
