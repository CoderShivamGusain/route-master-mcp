"""Run the browser scripts (browser/scripts.py) on mock pages in headless Chromium.

Skipped when Node.js or its `playwright` package isn't installed (e.g. on a plain dev machine).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mcp_route_master.browser import scripts as ps

RUNNER = Path(__file__).parent / "js" / "run_page_scripts.js"
RB = "https://www.redbus.in/bus-tickets/a-to-b?step=SL"


def _node_playwright() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    probe = subprocess.run([node, "-e", "require('playwright')"], capture_output=True, timeout=30)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright")


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    cases = {
        "seats": {"page": "redbus_seats", "url": RB, "js": ps.select_seats_js("redBus", ["3U"])},
        "seat_missing": {"page": "redbus_seats", "url": RB, "js": ps.select_seats_js("redBus", ["99"])},
        "points_rb": {
            "page": "redbus_points",
            "url": RB,
            "js": ps.choose_points_js("redBus", "2", "mudaila"),
        },
        "points_mmt": {
            "page": "mmt_points",
            "url": "https://www.makemytrip.com/bus/search/A/B/02-11-2026",
            "js": ps.choose_points_js("MakeMyTrip", "mall of dehradun", "1"),
        },
        "addons": {"page": "addons", "url": RB, "js": ps.DECLINE_ADDONS_JS},
        "continue": {"page": "continue_rb", "url": RB, "js": ps.continue_js("redBus")},
        "open_bus": {
            "page": "results_rb",
            "url": RB.replace("?step=SL", ""),
            "js": ps.open_bus_js("redBus", "ReoBus Pro", "22:25"),
        },
        "capture": {"page": "results_rb", "url": RB.replace("?step=SL", ""), "js": ps.CAPTURE_JS},
        "capture_pay": {
            "page": "continue_rb",
            "url": "https://www.redbus.in/paymentDetails",
            "js": ps.CAPTURE_JS,
        },
    }
    cases.update(_review_cases())
    path = tmp_path_factory.mktemp("js") / "scripts.json"
    path.write_text(json.dumps(cases))
    run = subprocess.run(["node", str(RUNNER), str(path)], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_select_seat_by_label(results):
    r = results["seats"]["result"]
    assert r["selected_seats"] == ["3U"]
    assert results["seat_missing"]["result"]["error"] == "seat 99 not on page"


def test_choose_points_by_number_or_name(results):
    assert sorted(results["points_rb"]["result"]["radios_on"]) == ["bp_1", "dp_0"]
    mmt = results["points_mmt"]["result"]
    assert "error" not in mmt and len(mmt["points"]) == 2 and "mall of dehradun" in mmt["points"][0]


def test_decline_addons_and_untick_insurance(results):
    r = results["addons"]["result"]
    assert set(r["radios_on"]) == {"tg", "ins"} and "unticked insurance" in r["declined"]
    assert len(r["declined"]) == 3


def test_continue_and_open_bus_only_mark_for_a_real_click(results):
    assert results["continue"]["result"] == {"marked": True, "button": "Continue booking"}
    assert results["continue"]["marked"] == "Continue booking"
    assert results["open_bus"]["result"] == {"marked": True} and results["open_bus"]["marked"] == "ReoBus Pro"


def test_capture_reads_cards_and_skips_inputs_on_payment(results):
    cap = results["capture"]["result"]
    assert len(cap["cards"]) == 2 and cap["cards"][1].startswith("22:25 | 09:55")
    pay = results["capture_pay"]["result"]
    assert "inputs" not in pay and "seats" not in pay and pay["url"].endswith("/paymentDetails")


# ------------------------------------------------------------------ review findings (v0.5.0)
RB_CARD = '<li class="tupleWrapper_x" data-name="{n}"><div>{dep}</div><div>{arr}</div><div>₹900</div><div>Onwards</div><div>{op}</div></li>'
MMT_CARD = '<div class="card" data-name="{n}"><p>{op}</p><p>{dep}2 NOV</p><p>04:00 3 NOV</p><button>SELECT SEATS</button></div>'


def _review_cases():
    rb_results = "https://www.redbus.in/bus-tickets/a-to-b"
    rb = (
        "<ul>"
        + RB_CARD.format(n="arrival", dep="20:00", arr="08:00", op="Zingbus")
        + RB_CARD.format(n="plus", dep="18:00", arr="06:00", op="Zingbus Plus")
        + RB_CARD.format(n="right", dep="08:00", arr="20:00", op="Zingbus")
        + "</ul>"
    )
    mmt = (
        "<section>"
        + MMT_CARD.format(n="alpha", op="Alpha Travels", dep="19:00")
        + MMT_CARD.format(n="beta", op="Beta Travels", dep="21:45")
        + "</section>"
    )
    twins = (
        "<ul>"
        + RB_CARD.format(n="a", dep="21:00", arr="09:00", op="Laxmi")
        + RB_CARD.format(n="b", dep="21:00", arr="09:30", op="Laxmi")
        + "</ul>"
    )
    google = (
        '<div><button>Continue with Google</button><span class="paymentBtn"><span>Continue</span></span>'
        "</div>"
    )
    two_continue = "<div><button>Continue booking</button><button>Continue booking</button></div>"
    one_list = (
        "<div><p>PICK UP POINT</p><p>DROP POINT</p><ul><li onclick=\"this.classList.add('selected')\">"
        "20:30 A</li><li>21:00 B</li></ul></div>"
    )
    dup_names = (
        '<label><input type="radio" name="bp_0"><span>20:45 Isbt Jogiwala</span></label>'
        '<label><input type="radio" name="bp_1"><span>20:50 Isbt mall gate</span></label>'
        '<label><input type="radio" name="dp_0"><span>08:00 Agra</span></label>'
    )
    card_page = '<input id="name" value="Ram Singh"><input autocomplete="cc-number" value="4111111111111111">'
    otp_upi = (
        '<input id="name" value="Ram Singh"><input name="otp" value="123456">'
        '<div><span>UPI ID</span><input id="v1" value="ram@upi"></div>'
    )
    return {
        "open_rb_exact": {"html": rb, "url": rb_results, "js": ps.open_bus_js("redBus", "Zingbus", "08:00")},
        "open_rb_short_time": {
            "html": rb,
            "url": rb_results,
            "js": ps.open_bus_js("redBus", "Zingbus", "8:00"),
        },
        "open_mmt_right_card": {
            "html": mmt,
            "url": "https://www.makemytrip.com/bus/search/A/B/02-11-2026",
            "js": ps.open_bus_js("MakeMyTrip", "Beta Travels", "21:45"),
        },
        "open_twins": {"html": twins, "url": rb_results, "js": ps.open_bus_js("redBus", "Laxmi", "21:00")},
        "continue_not_google": {
            "html": google,
            "url": "https://www.makemytrip.com/bus/review",
            "js": ps.continue_js("MakeMyTrip"),
        },
        "continue_ambiguous": {"html": two_continue, "url": RB, "js": ps.continue_js("redBus")},
        "points_one_list": {
            "html": one_list,
            "url": "https://www.makemytrip.com/bus/search/A/B/02-11-2026",
            "js": ps.choose_points_js("MakeMyTrip", "1", "1"),
        },
        "points_dup_name": {"html": dup_names, "url": RB, "js": ps.choose_points_js("redBus", "isbt", "1")},
        "capture_card_page": {
            "html": card_page,
            "url": RB.replace("step=SL", "step=CI"),
            "js": ps.CAPTURE_JS,
        },
        "capture_otp_upi": {"html": otp_upi, "url": RB.replace("step=SL", "step=CI"), "js": ps.CAPTURE_JS},
    }


def test_open_bus_matches_exact_operator_and_departure(results):
    assert results["open_rb_exact"]["card"] == "right"  # not the 08:00 arrival, not "Zingbus Plus"
    assert results["open_rb_short_time"]["card"] == "right"  # 8:00 == 08:00, but not 18:00
    assert results["open_mmt_right_card"]["card"] == "beta"
    assert (
        "2 buses match" in results["open_twins"]["result"]["error"]
        and results["open_twins"]["marked"] is None
    )


def test_continue_never_picks_login_and_refuses_to_guess(results):
    r = results["continue_not_google"]
    assert r["result"]["marked"] is True and r["marked"] == "Continue"
    assert "2 possible buttons" in results["continue_ambiguous"]["result"]["error"]


def test_choose_points_refuses_shared_list_and_ambiguous_name(results):
    assert "apart" in results["points_one_list"]["result"]["error"]
    assert "matches 2 points" in results["points_dup_name"]["result"]["error"]


def test_capture_never_reads_otp_card_or_upi_fields(results):
    assert "inputs" not in results["capture_card_page"]["result"]  # a card field makes it a payment page
    assert [i["id"] for i in results["capture_otp_upi"]["result"]["inputs"]] == ["name"]
