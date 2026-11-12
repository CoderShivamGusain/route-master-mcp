"""JavaScript the AI host runs in the user's own browser tab, supplied by the server.

Why this exists: the server never opens booking sites itself (no scraping, no hidden
browser). The AI host already has the user's browser open. Instead of the AI writing its
own page-reading code on every step, it runs these fixed scripts:

- CAPTURE_JS reads the current page into one small JSON object. The AI passes that
  object to the `read_page` tool, which turns it into buses, seats, stops or a form check.
- action_js() returns a script for one click-type action (select a seat, choose stops,
  decline paid extras, press continue). Every action script reports what the page shows
  afterwards, so the AI can check the result.

Safety rules baked into the scripts:
- Nothing is sent anywhere: the scripts only read or click inside the open tab and return
  their result to the AI.
- On payment pages the capture keeps only the visible summary text; it never reads input
  fields, so card numbers or UPI IDs typed there are never collected.
- Password fields are never read.
- Scripts never type personal details; the AI types those itself, after the user agrees.
"""

from __future__ import annotations

import json

# Read the current page. Returns (a promise of) a JSON string:
#   {v, url, title, text, cards, seats, mmt_seats, radios, inputs, selected, choices, active}
# - cards: redBus result cards, one line each ("20:00 | 08:00 | 12h | 36 Seats | ₹1,600 | ...").
# - seats: redBus seat buttons (their accessibility labels hold number, deck, type, price,
#   status) with on-page position [x, y].
# - mmt_seats: MakeMyTrip seat icons (data-testid, icon class, price text, position, DOM index).
# - radios / choices: radio buttons and MakeMyTrip's custom choice rows, with their text.
# - inputs: visible form fields. Never on payment pages; never password, card, CVV, UPI, OTP or
#   PIN fields on any page. Checkboxes carry the text next to them as their label.
CAPTURE_JS = r"""(async () => {
  // Fields that must never be read, judged by id, name, autocomplete, placeholder and label.
  const SECRET = /otp|one.?time|cvv|cvc|card|cc-|\bpin\b|upi|vpa|password|passcode|expiry|exp-/i;
  const about = e => [e.id, e.name, e.getAttribute("autocomplete"), e.placeholder, e.getAttribute("aria-label")]
    .join(" ");
  const pay = /pay|checkout/i.test(location.pathname) || /^payments?\./i.test(location.hostname) ||
    [...document.querySelectorAll("input")].some(e => /cc-|card|cvv|cvc|upi|vpa/i.test(about(e)));
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const cardSel = "li[class*=tupleWrapper]";
  if (!pay && document.querySelector(cardSel) && !/[?&]step=/.test(location.search)) {
    // redBus results load as you scroll: load more cards, then go back to the top.
    // (Only works while the browser pane is visible; hidden tabs don't load more.)
    for (let k = 0; k < 12; k++) {
      const all = document.querySelectorAll(cardSel);
      all[all.length - 1].scrollIntoView({ block: "end" }); await wait(600);
    }
    window.scrollTo(0, 0);
  }
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const pos = e => { const r = e.getBoundingClientRect();
    return [Math.round(r.x + scrollX), Math.round(r.y + scrollY)]; };
  const squash = s => (s || "").replace(/[ \t]+/g, " ").replace(/\n\s*\n+/g, "\n").trim();
  let text = squash(document.body.innerText);
  const cut = (from, to) => { const i = text.indexOf(from); if (i < 0) return;
    const j = to ? text.indexOf(to, i) : -1; text = text.slice(i, j > i ? j : undefined); };
  const mmt = /makemytrip/.test(location.host);
  // MakeMyTrip with a seat map open: keep just that bus's seat map and stops (the full list is long).
  let seatBox = mmt && !pay && document.querySelector('[data-testid^="seat_"]');
  while (seatBox && !/PICK ?UP POINT/i.test(seatBox.innerText || "")) seatBox = seatBox.parentElement;
  if (seatBox) text = squash(seatBox.innerText);
  else if (mmt && /buses found/.test(text)) {
    const at = Math.max(text.search(/\d+ buses found/), 0), end = text.indexOf("End of buses", at);
    text = text.slice(at, end > at ? end : undefined);
    // Drop each card's link row (Policies, Photos...) so every bus on the page fits.
    text = text.split("\n").filter(l => !/^(Policies|Photos|Amenities|Pickups & Drops|Reviews|\d+ Window Seats?|Save ₹[\d,]+)$/.test(l.trim())).join("\n");
  }
  const cards = [...document.querySelectorAll(cardSel)].map(e => squash(e.innerText).replace(/\n/g, " | "));
  if (cards.length) text = text.slice(0, 2500);  // redBus: the cards carry the buses; keep the header for groups
  const out = { v: 1, url: location.href, title: document.title,
    text: text.slice(0, pay ? 3000 : mmt ? 30000 : 14000), cards: cards.slice(0, 100) };
  if (pay) return JSON.stringify(out);
  out.seats = [...document.querySelectorAll('[aria-label^="Seat number"]')]
    .map(e => ({ l: e.getAttribute("aria-label").split(". Press")[0], p: pos(e) }));
  out.mmt_seats = [...document.querySelectorAll('[data-testid^="seat_"]')]
    .map((e, i) => ({ t: e.getAttribute("data-testid"),
      c: String(e.className).replace(/listingSprite|appendBottom4/g, "").trim(),
      price: squash(e.parentElement && e.parentElement.innerText).slice(0, 12), p: pos(e), i }));
  out.radios = [...document.querySelectorAll('input[type=radio]')].map(r => {
    let c = r; for (let k = 0; k < 5 && c && squash(c.innerText).length < 6; k++) c = c.parentElement;
    return { n: r.name, on: r.checked, t: squash(c && c.innerText).replace(/\n/g, " | ").slice(0, 200) }; });
  const near = e => { let c = e; for (let k = 0; k < 5 && c && squash(c.innerText).length < 6; k++) c = c.parentElement;
    return squash(c && c.innerText).replace(/\n/g, " ").slice(0, 60); };
  // A field's own label: its <label>, or the short text just before it (not the whole page).
  const ownLabel = e => [...(e.labels || [])].map(l => l.innerText)
    .concat(e.previousElementSibling ? [e.previousElementSibling.innerText] : []).join(" ").slice(0, 60);
  out.inputs = [...document.querySelectorAll("input,select,textarea")]
    .filter(e => vis(e) && !["radio", "hidden", "password", "submit", "button"].includes(e.type))
    .filter(e => !SECRET.test(about(e)) && !SECRET.test(ownLabel(e)))
    .map(e => ({ id: e.id || e.name, type: e.type,
      label: e.type === "checkbox" ? near(e) : (e.placeholder || e.getAttribute("aria-label") || ""),
      v: e.type === "checkbox" ? e.checked : String(e.value).slice(0, 80) }));
  out.selected = [...document.querySelectorAll("li.selected")]
    .map(l => squash(l.innerText).replace(/\n/g, " | ").slice(0, 200));
  out.choices = [...document.querySelectorAll(".bottomContainer > div")].map(d => {
    const inner = d.querySelector("span span");
    const bg = inner ? getComputedStyle(inner).backgroundColor : "";
    return { t: squash(d.innerText).slice(0, 120), on: !!bg && !/rgba\(0, 0, 0, 0\)|transparent/.test(bg) }; });
  out.active = [...document.querySelectorAll('[class*=ActiveTab],[aria-checked="true"],[aria-selected="true"]')]
    .map(e => squash(e.innerText || e.getAttribute("aria-label")).slice(0, 40)).filter(Boolean);
  return JSON.stringify(out);
})()"""


def _js(value: object) -> str:
    """A Python value as a JavaScript literal (JSON is valid JS)."""
    return json.dumps(value, ensure_ascii=False)


_REPORT = r"""
  const sq = s => (s || "").replace(/\s+/g, " ").trim();
  const report = () => ({
    url: location.href,
    selected_seats: [...document.querySelectorAll('[aria-label^="Seat number"]')]
      .filter(e => /currently selected/.test(e.getAttribute("aria-label")))
      .map(e => e.getAttribute("aria-label").split(",")[0].replace("Seat number ", "")),
    mmt_selected: (document.body.innerText.match(/Selected Seats\n([^\n]+)\n([^\n]+)/) || []).slice(1),
    radios_on: [...document.querySelectorAll("input[type=radio]")].filter(r => r.checked).map(r => r.name),
    points: [...document.querySelectorAll("li.selected")].map(l => sq(l.innerText).slice(0, 80)),
  });
  const wait = ms => new Promise(r => setTimeout(r, ms));
"""


def select_seats_js(site: str, seats: list[str], mmt_indexes: list[int] | None = None) -> str:
    """Click the named seats. redBus finds each by its label; MakeMyTrip by the DOM index
    `read_page` gave for each position label (its map has no seat numbers)."""
    if site == "MakeMyTrip":
        return f"""(async () => {{{_REPORT}
  const want = {_js(mmt_indexes or [])};
  const all = [...document.querySelectorAll('[data-testid^="seat_"]')];
  for (const i of want) {{ if (!all[i]) return JSON.stringify({{error: "seat index " + i + " not on page"}});
    all[i].scrollIntoView({{block: "center"}}); all[i].click(); await wait(900); }}
  return JSON.stringify(report());
}})()"""
    return f"""(async () => {{{_REPORT}
  const want = {_js(seats)};
  for (const n of want) {{
    const el = document.querySelector('[aria-label^="Seat number ' + CSS.escape(n) + ',"]');
    if (!el) return JSON.stringify({{error: "seat " + n + " not on page"}});
    if (!/currently selected/.test(el.getAttribute("aria-label"))) {{
      el.scrollIntoView({{block: "center"}}); el.click(); await wait(900); }}
  }}
  return JSON.stringify(report());
}})()"""


def choose_points_js(site: str, boarding: str, dropping: str) -> str:
    """Pick a boarding and a dropping point. `boarding`/`dropping` are the numbers `read_page`
    showed (1-based) or a few words of the point's name. A name that matches more than one
    point, or a list that can't be told apart from the other one, is an error, not a guess."""
    return f"""(async () => {{{_REPORT}
  const site = {_js(site)};
  const textOf = x => {{ let c = x; for (let k = 0; k < 5 && sq(c.innerText).length < 6; k++) c = c.parentElement;
    return sq(c.innerText).toLowerCase(); }};
  const choose = (items, key, label) => {{
    if (/^\\d+$/.test(key)) return items[Number(key) - 1] || label + " point " + key + " doesn't exist";
    const hits = items.filter(x => textOf(x).includes(key.toLowerCase()));
    if (hits.length === 1) return hits[0];
    return hits.length ? label + " point '" + key + "' matches " + hits.length + " points; use its number"
      : label + " point '" + key + "' not found";
  }};
  const lists = () => {{
    if (site === "redBus") return {{
      bp: [...document.querySelectorAll('input[type=radio][name^="bp_"]')],
      dp: [...document.querySelectorAll('input[type=radio][name^="dp_"]')] }};
    const heads = [...document.querySelectorAll("p,span,div,h2,h3,h4")];
    const pickHead = heads.find(n => /^PICK ?UP POINT$/i.test(sq(n.textContent)));
    const dropHead = heads.find(n => /^DROP POINT$/i.test(sq(n.textContent)));
    // A heading's list: the smallest box around it that has items and doesn't hold the other heading.
    const own = (h, other) => {{ let box = h;
      while (box && !(box.querySelector("li") && !(other && box.contains(other)))) box = box.parentElement;
      return box ? [...box.querySelectorAll("li")] : []; }};
    return {{ bp: pickHead ? own(pickHead, dropHead) : [], dp: dropHead ? own(dropHead, pickHead) : [] }};
  }};
  const L = lists();
  if (!L.bp.length || !L.dp.length || L.bp.some(x => L.dp.includes(x)))
    return JSON.stringify({{error: "couldn't tell the boarding and dropping lists apart; pick them by hand"}});
  for (const [kind, key, label] of [["bp", {_js(boarding)}, "boarding"], ["dp", {_js(dropping)}, "dropping"]]) {{
    const el = choose(L[kind], key, label);
    if (typeof el === "string") return JSON.stringify(Object.assign(report(), {{error: el}}));
    (el.closest("label") || el).click(); await wait(1000);
  }}
  return JSON.stringify(report());
}})()"""


DECLINE_ADDONS_JS = (
    r"""(async () => {"""
    + _REPORT
    + r"""
  const done = [];
  // "Don't add ..." (redBus), "No,I don't need it" (MakeMyTrip: Free Cancellation, TripAssured).
  const NO = /^(Don.?t add |No,?\s*I\s*(don.?t|do not)\s+(need|want)|No,?\s*I.?ll (take the )?risk)/i;
  const press = el => {
    const r = el.getBoundingClientRect();
    const at = { bubbles: true, cancelable: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"])
      el.dispatchEvent(new (type.startsWith("pointer") ? PointerEvent : MouseEvent)(type, at));
    el.click();
  };
  const labels = [...document.querySelectorAll("*")].filter(n => n.children.length === 0 && NO.test(sq(n.textContent)));
  for (const el of labels) {
    // Press only inside this option: a radio found further up could be the "Add ..." one.
    const box = el.closest(".bottomContainer > div");
    let r = null;
    if (box) r = box.querySelector("input[type=radio]");
    else {
      let c = el; for (let k = 0; k < 4 && c && !c.querySelector("input[type=radio]"); k++) c = c.parentElement;
      const radios = c ? c.querySelectorAll("input[type=radio]") : [];
      r = radios.length === 1 ? radios[0] : null; // one radio: this option's own
    }
    press(r || box || el.parentElement || el); done.push(sq(el.textContent)); await wait(600);
  }
  for (const cb of document.querySelectorAll(".travelInsuranceWrapper input[type=checkbox]")) {
    if (cb.checked) { cb.click(); done.push("unticked insurance"); await wait(600); } }
  return JSON.stringify(Object.assign(report(), {declined: done}));
})()"""
)

# Some clicks only work as real clicks (redBus bus cards, MakeMyTrip's last CONTINUE): the
# site ignores clicks made by a script. For those, the script only *marks* the element with
# the accessible name "rm-target"; the AI then finds "rm-target" with its page-search tool and
# clicks it with a real click.
_MARK = r"""
  const mark = el => {
    document.querySelectorAll("[data-rm-target]").forEach(e => {
      e.removeAttribute("data-rm-target");
      const old = e.getAttribute("data-rm-old");
      if (old === null) e.removeAttribute("aria-label"); else e.setAttribute("aria-label", old);
      if (e.hasAttribute("data-rm-role")) e.removeAttribute("role");
      e.removeAttribute("data-rm-old"); e.removeAttribute("data-rm-role"); });
    if (el.hasAttribute("aria-label")) el.setAttribute("data-rm-old", el.getAttribute("aria-label"));
    el.setAttribute("data-rm-target", "1"); el.setAttribute("aria-label", "rm-target");
    if (!el.getAttribute("role")) { el.setAttribute("role", "button"); el.setAttribute("data-rm-role", "1"); }
    el.scrollIntoView({ block: "center" });
  };
"""


def _hhmm(departure: str) -> str:
    """'12:50 AM', '00:50 AM', '0:50' -> '00:50' (the cards show 24-hour times); unreadable text unchanged."""
    from mcp_route_master.booking.ranking import TimeFormatError, fmt_clock, parse_clock

    try:
        return fmt_clock(parse_clock(departure.strip()))
    except TimeFormatError:
        return departure.strip()


def open_bus_js(site: str, operator: str, departure: str, arrival: str | None = None) -> str:
    """Mark the bus card (redBus) or its SELECT SEATS button (MakeMyTrip) for a real click.

    A card matches only if its operator text equals `operator` (not just contains it) and its
    *first* time is `departure` (so an 08:00 arrival doesn't match an 08:00 departure). When one
    operator runs two buses at that time (live: Laxmi Holidays 21:30, a Volvo arriving 02:00 and a
    Bharat Benz arriving 03:25), `arrival` (the card's second time) picks one. Zero or several
    matches is an error that lists the candidates: the AI must not open the wrong bus."""
    return f"""(() => {{{_REPORT}{_MARK}
  const op = {_js(operator.strip().lower())}, dep = {_js(_hhmm(departure))}, arr = {_js(_hhmm(arrival or ""))};
  const hhmm = t => {{ const m = /^(\\d{{1,2}}):(\\d{{2}})$/.exec(t || ""); return m ? m[1].padStart(2, "0") + ":" + m[2] : null; }};
  const want = hhmm(dep);
  const leaves = el => [...el.querySelectorAll("*")].filter(n => n.children.length === 0).map(n => [n, sq(n.textContent)]);
  const firstTime = el => {{ const m = /(\\d{{1,2}}:\\d{{2}})/.exec(sq(el.innerText)); return m ? hhmm(m[1]) : null; }};
  const opLeaf = el => (leaves(el).find(([, t]) => t.toLowerCase() === op) || [])[0];
  let cards;
  if ({_js(site)} === "redBus") {{
    cards = [...document.querySelectorAll("li[class*=tupleWrapper]")];
  }} else {{
    // One card per SELECT SEATS button: its largest ancestor that holds no other such button.
    const btns = [...document.querySelectorAll("*")].filter(n => n.children.length === 0 && /^select seats$/i.test(sq(n.textContent)));
    cards = btns.map(b => {{ let c = b;
      while (c.parentElement && [...btns].filter(x => c.parentElement.contains(x)).length === 1) c = c.parentElement;
      c.__rmButton = b; return c; }});
  }}
  const times = el => [...sq(el.innerText).matchAll(/(\\d{{1,2}}:\\d{{2}})/g)].map(m => hhmm(m[1]));
  let hits = cards.filter(c => opLeaf(c) && firstTime(c) === want);
  if (hits.length > 1 && hhmm(arr)) {{
    const same = hits.filter(c => times(c)[1] === hhmm(arr));
    if (same.length) hits = same;
  }}
  if (hits.length !== 1) {{
    if (hits.length) {{
      const kind = c => (leaves(c).map(([, x]) => x).find(x => x.length < 60 && /seater|sleeper|volvo|a.c|\\bac\\b|electric|semi/i.test(x)) || "");
      const list = hits.map(c => "arrives " + (times(c)[1] || "?") + (kind(c) ? ", " + kind(c) : "")).join("; ");
      return JSON.stringify({{error: hits.length + " buses of '" + op + "' leave at " + want + " (" + list
        + "). Call again with arrival=HH:MM of the one the user chose"}});
    }}
    // Never guess (a wrong guess opens the wrong bus): name the buses at that time, so the AI can
    // call again with the exact operator, e.g. "RSRTC - 211911" when it asked for "RSRTC AC Sleeper".
    const nameOf = c => {{
      const t = leaves(c).map(([, x]) => x).filter(Boolean);
      if ({_js(site)} === "redBus") {{
        const i = t.indexOf("Onwards");
        return i >= 0 ? t.slice(i + 1).find(x => x !== "Ad") : null;
      }}
      return t[0];
    }};
    const same = cards.filter(c => firstTime(c) === want).map(nameOf).filter(Boolean).slice(0, 6);
    return JSON.stringify({{error: same.length
      ? "no bus named '" + op + "' at " + want + "; at " + want + " this page has: " + same.map(n => "'" + n + "'").join(", ")
        + ". Call again with the exact name of the one the user chose"
      : "bus not found on this page; use the operator and time exactly as in the bus table"}});
  }}
  mark({_js(site)} === "redBus" ? opLeaf(hits[0]) : hits[0].__rmButton);
  return JSON.stringify({{marked: true}});
}})()"""


# Button texts per step, checked live (redBus: desktop and mobile layout; MakeMyTrip).
_CONTINUE = {
    "redBus": ["Select boarding & dropping points", "Fill passenger details", "Continue booking"],
    "MakeMyTrip": ["Continue"],
}


def continue_js(site: str) -> str:
    """Mark the site's 'next step' button for a real click.

    Only an element whose own text is exactly one of the known button texts counts, never one
    that also mentions Google, login, sign-in, OTP or guest. If several different buttons match,
    it's an error rather than a guess."""
    names = _js([n.lower() for n in _CONTINUE.get(site, ["Continue", "Proceed"])])
    return f"""(() => {{{_REPORT}{_MARK}
  const names = {names};
  const banned = /google|facebook|apple|log ?in|sign ?(in|up)|otp|guest|whatsapp/i;
  const seen = new Set();
  for (const n of document.querySelectorAll("button, [role=button], .paymentBtn, a, span, div, p")) {{
    if (!(n.offsetParent || n.getClientRects().length)) continue;
    const own = sq([...n.childNodes].filter(c => c.nodeType === 3).map(c => c.textContent).join(" ")) ||
      (n.children.length === 0 ? sq(n.textContent) : "");
    if (!names.includes(own.toLowerCase())) continue;
    const target = n.closest("button, [role=button], .paymentBtn, a") || n;
    if (banned.test(sq(target.innerText)) || banned.test(target.getAttribute("aria-label") || "")) continue;
    seen.add(target);
  }}
  const found = [...seen].filter(t => ![...seen].some(o => o !== t && o.contains(t)));
  if (found.length !== 1)
    return JSON.stringify({{error: found.length ? found.length + " possible buttons; press it by hand" : "no continue button found on this page"}});
  mark(found[0]);
  return JSON.stringify({{marked: true, button: sq(found[0].innerText).slice(0, 40)}});
}})()"""
