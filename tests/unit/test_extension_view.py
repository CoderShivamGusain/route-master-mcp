"""The drawable view (MCP Apps) behind browser_open / browser_read / browser_action."""

import json

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
