"""Run the extension's page scripts (generated actions.js and forms.js) in headless Chromium.

Skipped when Node.js or its `playwright` package isn't installed.
"""

import json
import subprocess
from pathlib import Path

import pytest
from test_page_scripts import RUNNER, _node_playwright

EXT = Path(__file__).resolve().parents[2] / "route-master-extension" / "page"
BUNDLE = (
    (EXT / "actions.js").read_text(encoding="utf-8") + "\n" + (EXT / "forms.js").read_text(encoding="utf-8")
)
RB = "https://www.redbus.in/bus-tickets/a-to-b?step=SL"
FORM = """<div><label for="n">Name *</label><input id="n" placeholder="Enter your Name">
  <label for="a">Age *</label><input id="a" type="number" placeholder="Enter Age">
  <label for="e">Email ID</label><input id="e" placeholder="Email ID">
  <label for="c">Card number</label><input id="c" placeholder="Card number">
  <div role="radio">Male</div><div role="radio">Female</div><button>Continue booking</button></div>"""

MMT = "https://www.makemytrip.com/bus/search/Delhi/Agra/19-11-2026"
_CARD = (
    "<div><p>{op}</p><p>{t}</p><p>Policies</p><p>Photos</p><p>Amenities</p><p>Pickups &amp; Drops</p>"
    "<p>Reviews</p><p>6 Window Seats</p><p>Save ₹120</p><button>SELECT SEATS</button></div>"
)
MMT_LIST = (
    "<p>12 buses found</p>"
    + _CARD.format(op="FlixBus", t="00:5020 NOV")
    + _CARD.format(op="Zing", t="21:3019 NOV")
    + "<p>End of buses</p><footer>About us</footer>"
)
MMT_SEAT_MAP = MMT_LIST.replace(
    "<p>End of buses</p>",
    '<section><p>LOWER BERTH (2)</p><div><span data-testid="seat_L1" class="listingSprite"></span>'
    "<span>₹899</span></div><p>PICK UP POINT</p><p>Kashmere Gate</p></section><p>End of buses</p>",
)

pytestmark = pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright")


def _js(call: str) -> str:
    return f"(async () => {{ {BUNDLE}\n return await {call}; }})()"


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    cases = {
        "seats": {
            "page": "redbus_seats",
            "url": RB,
            "js": _js("__rm.select_seats({site: 'redBus', seats: ['3U']})"),
        },
        "open_bus": {
            "page": "results_rb",
            "url": RB.replace("?step=SL", ""),
            "js": _js("__rm.open_bus({site: 'redBus', operator: ' ReoBus Pro ', departure: '22:25'})"),
        },
        "capture": {"page": "results_rb", "url": RB.replace("?step=SL", ""), "js": _js("__rm.capture({})")},
        "points": {
            "page": "redbus_points",
            "url": RB,
            "js": _js("__rm.choose_points({site: 'redBus', boarding: '2', dropping: 'mudaila'})"),
        },
        "continue": {"page": "continue_rb", "url": RB, "js": _js("__rm.continue({site: 'redBus'})")},
        "fill": {
            "html": FORM,
            "url": RB,
            "js": _js(
                "Promise.resolve(__rmForms.fill([{field: 'Name', value: 'Ram Singh'}, {field: 'age', value: '31'}, "
                "{field: 'Card number', value: '4111'}, {field: 'Phone', value: '1'}]))"
                ".then(r => JSON.stringify({...r, values: [...document.querySelectorAll('input')].map(i => i.value)}))"
            ),
        },
        "choose": {"html": FORM, "url": RB, "js": _js("JSON.stringify(__rmForms.choose('Male'))")},
        "choose_second": {
            "html": FORM.replace("<button>", '<div role="radio">Male</div><button>'),
            "url": RB,
            "js": _js(
                "Promise.resolve(__rmForms.choose('Male', 2)).then(r => JSON.stringify({...r, "
                "at: [...document.querySelectorAll('[role=radio]')].findIndex(e => e.hasAttribute('data-rm-target'))}))"
            ),
        },
        "choose_missing_nth": {
            "html": FORM,
            "url": RB,
            "js": _js("JSON.stringify(__rmForms.choose('Male', 2))"),
        },
        "choose_banned": {
            "html": FORM,
            "url": RB,
            "js": _js("JSON.stringify(__rmForms.choose('Continue booking'))"),
        },
        "mmt_list": {"html": MMT_LIST, "url": MMT, "js": _js("__rm.capture({})")},
        "mmt_seat_map": {"html": MMT_SEAT_MAP, "url": MMT, "js": _js("__rm.capture({})")},
        "fill_on_payment": {
            "html": FORM,
            "url": "https://www.redbus.in/paymentDetails",
            "js": _js("JSON.stringify(__rmForms.fill([{field: 'Name', value: 'x'}]))"),
        },
    }
    path = tmp_path_factory.mktemp("ext") / "scripts.json"
    path.write_text(json.dumps(cases))
    proc = subprocess.run(["node", str(RUNNER), str(path)], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_generated_actions_behave_like_the_server_scripts(results):
    assert results["seats"]["result"]["selected_seats"] == ["3U"]
    assert results["open_bus"]["result"] == {"marked": True} and results["open_bus"]["marked"] == "ReoBus Pro"
    assert len(results["capture"]["result"]["cards"]) == 2
    assert results["points"]["result"]["radios_on"] == ["bp_1", "dp_0"]
    assert results["continue"]["result"]["marked"] is True


def test_fill_types_only_safe_fields(results):
    r = results["fill"]["result"]
    assert r["filled"] == ["Name", "age"] and r["refused"] == ["Card number"] and r["missing"] == ["Phone"]
    assert r["values"] == ["Ram Singh", "31", "", ""]
    assert "error" in results["fill_on_payment"]["result"]


def test_choose_marks_options_but_never_continue(results):
    assert results["choose"]["result"] == {"marked": True, "option": "Male"}
    assert "can't be chosen" in results["choose_banned"]["result"]["error"]
    assert results["choose_second"]["result"]["at"] == 2  # passenger 2's "Male" (third radio)
    assert "there's no number 2" in results["choose_missing_nth"]["result"]["error"]


def test_mmt_capture_keeps_buses_and_drops_link_rows(results):
    text = (
        json.loads(results["mmt_list"]["result"])["text"]
        if isinstance(results["mmt_list"]["result"], str)
        else results["mmt_list"]["result"]["text"]
    )
    assert text.startswith("12 buses found") and "FlixBus" in text and "00:5020 NOV" in text
    assert not any(w in text for w in ("Policies", "Photos", "Window Seats", "Save ₹", "About us"))


def test_mmt_capture_with_a_seat_map_open_keeps_only_that_bus(results):
    r = results["mmt_seat_map"]["result"]
    r = json.loads(r) if isinstance(r, str) else r
    assert "LOWER BERTH (2)" in r["text"] and "PICK UP POINT" in r["text"] and "Zing" not in r["text"]
    assert [s["t"] for s in r["mmt_seats"]] == ["seat_L1"]
