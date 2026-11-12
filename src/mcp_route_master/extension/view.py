"""What the chat app draws for a browser step (MCP Apps): a real seat map, or the bus table.

The browser_* tools return their usual text (every AI reads it; Gemini only reads that) and, for
seat and results pages, the same facts as `structuredContent`. Apps that support MCP Apps
(ChatGPT, Claude) render it with the `ui://` page in `view.html`: decks drawn to scale, sold and
ladies' seats marked, suggested seats highlighted, and a click on a free seat asks for it in chat.
"""

from __future__ import annotations

import re
from importlib.resources import files
from typing import Any

from mcp_route_master.booking.seats import (
    DeckLayout,
    Seat,
    deck_plan,
    layout,
    mixed,
    normalise_kind,
    normalise_reserved,
    normalise_status,
    sides,
    suggest,
)
from mcp_route_master.browser.pages import (
    CaptureError,
    load_capture,
    page_kind,
    parse_mmt_seats,
    parse_redbus_seats,
    site_of,
)

# A new URI whenever the page changes in a way old copies can't handle (hosts cache by URI).
VIEW_URI = "ui://route-master/view-v1.html"
VIEW_MIME = "text/html;profile=mcp-app"


def view_html() -> str:
    return files("mcp_route_master.extension").joinpath("view.html").read_text(encoding="utf-8")


def page_view(
    capture: str, text: str, seats_wanted: int | None = None, preferences: list[str] | None = None
) -> dict[str, Any] | None:
    """The structured view of this step, or None when there's nothing to draw."""
    try:
        c = load_capture(capture)
    except CaptureError:
        return None
    site = site_of(c.url)
    if site is None:
        return None
    kind = page_kind(c, site)
    if kind == "seats" or (kind == "results" and (c.seats or c.mmt_seats)):
        return _seats_view(c, site, text, seats_wanted, preferences)
    if kind == "results":
        return _buses_view(text)
    return None


def _seats_view(c, site: str, text: str, seats_wanted, preferences) -> dict[str, Any] | None:
    read = parse_redbus_seats(c)[0] if site == "redBus" else parse_mmt_seats(c)[0]
    if not read:
        return None
    seats = [
        Seat(
            number=s.number.strip(),
            deck=(s.deck or "lower").strip().lower() or "lower",
            kind=normalise_kind(s.seat_type),
            status=normalise_status(s.status),
            price=s.price,
            reserved_for=normalise_reserved(s.reserved_for),
            x=s.x,
            y=s.y,
        )
        for s in read
    ]
    decks = layout(seats, "top")
    prefs = list(preferences or [])
    wanted = seats_wanted or 1
    if wanted > 1 and "together" not in prefs:
        prefs.append("together")
    picks = {(p.deck, p.number) for p in (suggest(seats, wanted, prefs, decks) or [])}  # type: ignore[arg-type]
    lines = text.splitlines()
    at = next((i for i, ln in enumerate(lines) if ln.startswith("## Seats")), None)
    return {
        "kind": "seats",
        "title": lines[at][3:].strip() if at is not None else "Seats",
        "summary": lines[at + 1].strip() if at is not None and at + 1 < len(lines) else "",
        "site": site,
        "positions": site == "MakeMyTrip",  # labels are positions; the site numbers seats once picked
        "decks": [_deck_view(d, picks) for d in decks],
    }


_TYPE = {"sleeper": "sleeper berth", "seater": "seat", "semi_sleeper": "semi-sleeper seat"}


def _deck_view(d: DeckLayout, picks: set) -> dict[str, Any]:
    taken = {(s.row, s.col) for s in d.seats}
    groups = sides(d)
    return {
        "deck": d.deck,
        # e.g. "left: single sleeper berths (window) · right: 2 seats side by side"
        "plan": deck_plan(d),
        "rows": d.rows if d.positioned else None,
        "cols": d.cols if d.positioned else None,
        # The aisle is right of this column; None when it's already an empty column of the grid.
        "aisle_after": groups[0][-1] if len(groups) == 2 and not any(a >= 0 for a in d.aisle_cols) else None,
        "mixed": mixed(d),
        "seats": [
            {
                "n": s.number,
                "type": _TYPE.get(s.kind, "seat"),
                "r": s.row,
                "c": s.col,
                # A berth is as long as two seats: it takes the row below too when that's empty.
                "span": 2
                if s.kind == "sleeper"
                and s.row is not None
                and s.row + 1 < d.rows
                and (s.row + 1, s.col) not in taken
                else 1,
                "side": s.side,
                "single": s.single,
                "s": s.status,
                "p": s.price,
                "k": s.kind,
                "f": {"female": "F", "male": "M"}.get(s.reserved_for or "", ""),
                "w": s.window,
                "pick": (s.deck, s.number) in picks,
            }
            for s in d.seats
        ],
    }


_ROW = re.compile(r"^\| (\d+) \| (.+?) \| (.+?) \| (.+?) \| (.+?) \| (.*?) \|(?: \[([^\]]+)\]\()?")


def _buses_view(text: str) -> dict[str, Any] | None:
    """The ranked table(s) from the results text (today's, plus tomorrow's when shown)."""
    sections: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append({"title": line[3:].strip(), "notes": [], "rows": []})
        elif not sections:
            continue
        elif m := _ROW.match(line):
            n, time, bus, fare, trust, note, site = m.groups()
            sections[-1]["rows"].append(
                {
                    "n": int(n),
                    "time": time,
                    "bus": bus,
                    "fare": fare,
                    "trust": trust,
                    "note": "" if note == "-" else note,
                    "site": site,  # set when buses from several sites are ranked together
                }
            )
        elif line.startswith("_") and line.endswith("_") and not line.startswith("_Trust"):
            sections[-1]["notes"].append(line.strip("_"))
    sections = [s for s in sections if s["rows"]]
    return {"kind": "buses", "sections": sections} if sections else None
