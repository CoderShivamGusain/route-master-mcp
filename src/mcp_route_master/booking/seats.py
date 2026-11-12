"""Seat maps the AI host reads from a booking site, drawn as a text diagram.

The server never fetches seat maps. A host with a browser (e.g. Claude) opens
"Select seats" on the official site, reads each seat (number, deck, type, price,
status, reserved-for, on-screen position) and passes them here. redBus exposes
all of this in each seat's accessibility label, e.g. "Seat number 15, lower deck,
seat type seater, berth reserved for male, price 697 rupees, availability sold".

Used by the explain_seat_map tool. Pipeline: normalise each seat's free-text
fields -> `layout` places seats in a per-deck grid -> `draw` renders it as text
-> `suggest` picks seats that fit the user's preferences.

Why clustering for the layout: the host reads on-screen x/y positions, which
are fractional, slightly uneven and in whatever units the page uses; there is
no row/column number. Sorting coordinates and grouping values closer than a
fraction of the typical seat pitch (`_clusters`) recovers rows and columns
without assuming a fixed bus layout (2+1 sleeper, 2+2 seater, mixed decks).
The aisle is then inferred from a column that most rows leave empty, or
failing that from a gap between columns clearly wider than the seat pitch.
Window/aisle flags and "seats together" depend on this grid, so without
positions seats are only listed in number order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

SeatKind = Literal["seater", "semi_sleeper", "sleeper", "unknown"]
SeatStatus = Literal["available", "sold", "selected"]
Preference = Literal[
    "window", "aisle", "front", "back", "together", "lower", "upper", "cheapest", "sleeper", "seater"
]

_SOLD_WORDS = {"sold", "booked", "unavailable", "blocked", "reserved", "occupied"}


@dataclass
class Seat:
    """One seat or berth as read from the page; the grid fields are filled by `layout`."""

    number: str
    deck: str = "lower"
    kind: SeatKind = "unknown"
    status: SeatStatus = "available"
    price: float | None = None
    reserved_for: Literal["female", "male"] | None = None
    x: float | None = None  # on-screen position, any unit; rotated in place by _orient()
    y: float | None = None
    # filled by layout()
    row: int | None = None
    col: int | None = None
    window: bool = False
    aisle: bool = False
    side: str | None = None  # "left" / "right" of the aisle, as on the site's map (front at the top)
    single: bool = False  # alone on its side of the aisle (a single berth or seat)


def normalise_status(text: str | None) -> SeatStatus:
    """Map a site's availability text to available/sold/selected (unknown text = available)."""
    t = (text or "").strip().lower()
    if t in {"selected", "chosen"}:
        return "selected"
    if t in _SOLD_WORDS or "sold" in t or "not available" in t:
        return "sold"
    return "available"


def normalise_kind(text: str | None) -> SeatKind:
    """Map a seat-type label to a SeatKind; "semi sleeper" is checked first (see trust/bus_types.py)."""
    t = (text or "").lower()
    if re.search(r"semi[\s_-]*sleeper", t):
        return "semi_sleeper"
    if "sleeper" in t or "berth" in t:
        return "sleeper"
    if "seat" in t:
        return "seater"
    return "unknown"


def normalise_reserved(text: str | None) -> Literal["female", "male"] | None:
    """Ladies/gents-only seat marker, if any."""
    t = (text or "").lower()
    # Female first, with substring matching: "female" contains "male", so the male check
    # below needs word boundaries.
    if any(w in t for w in ("female", "ladies", "lady", "women", "woman")):
        return "female"
    if re.search(r"\bmale\b|\bgents\b|\bmen\b", t):
        return "male"
    return None


def seat_sort_key(number: str) -> tuple:
    """Natural order: 'L2' < 'L10', '2' < '10'."""
    # Split into digit and non-digit runs; numbers compare numerically, text case-insensitively.
    return tuple(
        (0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in re.findall(r"\d+|\D+", number)
    )


def _clusters(values: list[float]) -> list[float]:
    """Group nearly equal coordinates (same row/column) and return the group centres.

    Positions read from a page are fractional and slightly uneven, so the tolerance comes from
    the seat pitch (the typical distance between rows/columns), not from the smallest gap.
    """
    vals = sorted(set(values))
    if not vals:
        return []
    gaps = [b - a for a, b in zip(vals, vals[1:], strict=False)]
    if not gaps:
        return vals
    # Ignore tiny gaps (jitter within one row) when estimating the pitch: keep gaps at least a
    # quarter of the largest, then take their median. Values within 40% of a pitch are merged.
    significant = [g for g in gaps if g >= 0.25 * max(gaps)]
    pitch = sorted(significant)[len(significant) // 2]
    tol = max(0.4 * pitch, 1e-9)
    groups: list[list[float]] = [[vals[0]]]
    for v in vals[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _nearest(centres: list[float], v: float) -> int:
    """Index of the cluster centre closest to v."""
    return min(range(len(centres)), key=lambda i: abs(centres[i] - v))


@dataclass
class DeckLayout:
    """Grid for one deck.

    `aisle_cols` holds either a column index c >= 0 (an empty aisle column) or a
    negative value -(i + 1) meaning "the aisle runs between columns i and i + 1".
    `positioned` is False when seats had no x/y and are only in number order.
    """

    deck: str
    seats: list[Seat]
    rows: int = 0
    cols: int = 0
    aisle_cols: set[int] = field(default_factory=set)
    positioned: bool = False


# Which screen edge the driver's end of the bus is drawn at.
Front = Literal["auto", "top", "bottom", "left", "right"]


def _orient(seats: list[Seat], front: Front) -> None:
    """Rotate positions so the front of the bus is at the top (y grows towards the back)."""
    pos = [s for s in seats if s.x is not None and s.y is not None]
    if not pos:
        return
    if front == "auto":
        n_x, n_y = len(_clusters([s.x for s in pos])), len(_clusters([s.y for s in pos]))
        front = "left" if n_x > n_y * 1.5 else "top"  # a bus is longer than it is wide
    for s in pos:
        x, y = s.x, s.y
        if front == "bottom":
            s.x, s.y = -x, -y
        elif front == "left":
            s.x, s.y = -y, x
        elif front == "right":
            s.x, s.y = y, -x


def layout(seats: list[Seat], front: Front = "auto") -> list[DeckLayout]:
    """Place seats in a grid per deck using their on-screen positions (when given).

    Mutates the seats (rotated x/y, row, col, window, aisle). The lower deck comes first.
    """
    decks: dict[str, list[Seat]] = {}
    for s in seats:
        decks.setdefault(s.deck, []).append(s)
    out = []
    for deck in sorted(decks, key=lambda d: (d != "lower", d)):
        ds = decks[deck]
        _orient(ds, front)
        dl = DeckLayout(deck, ds)
        if all(s.x is not None and s.y is not None for s in ds) and len(ds) > 1:
            ys, xs = _clusters([s.y for s in ds]), _clusters([s.x for s in ds])
            for s in ds:
                s.row, s.col = _nearest(ys, s.y), _nearest(xs, s.x)
            dl.rows, dl.cols, dl.positioned = len(ys), len(xs), True
            # A column used by only a few rows is the aisle (e.g. the back-row middle seat).
            # Edge columns are windows, never the aisle.
            used = {c: len({s.row for s in ds if s.col == c}) for c in range(dl.cols)}
            dl.aisle_cols = {c for c, n in used.items() if n < 0.5 * dl.rows and 0 < c < dl.cols - 1}
            if not dl.aisle_cols and dl.cols >= 3:
                gaps = [xs[i + 1] - xs[i] for i in range(dl.cols - 1)]
                typical = min(gaps)  # seat pitch; the aisle is the gap clearly wider than it
                big = [i for i, g in enumerate(gaps) if g > 1.4 * typical]
                dl.aisle_cols = {-(i + 1) for i in big}  # aisle sits *between* col i and i+1
            groups = sides(dl)
            for s in ds:
                s.window = s.col in (0, dl.cols - 1)
                if len(groups) == 2:
                    g = next((i for i, cols in enumerate(groups) if s.col in cols), None)
                    if g is not None:
                        s.side, s.single = ("left", "right")[g], len(groups[g]) == 1
                # Next to an aisle column, or on either side of an aisle gap.
                s.aisle = any(
                    (a >= 0 and abs(s.col - a) == 1) or (a < 0 and s.col in (-a - 1, -a))
                    for a in dl.aisle_cols
                )
        else:
            ds.sort(key=lambda s: seat_sort_key(s.number))
        out.append(dl)
    return out


def sides(dl: DeckLayout) -> list[list[int]]:
    """Columns on each side of the aisle, left to right, e.g. [[0], [2, 3]] for a 1+2 layout."""
    if not dl.positioned:
        return []
    empty = {c for c in dl.aisle_cols if c >= 0}
    after = {-a - 1 for a in dl.aisle_cols if a < 0}
    groups: list[list[int]] = []
    cur: list[int] = []
    for c in range(dl.cols):
        if c in empty:
            if cur:
                groups.append(cur)
            cur = []
            continue
        cur.append(c)
        if c in after:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


_KIND_WORD = {"sleeper": "sleeper berth", "seater": "seat", "semi_sleeper": "semi-sleeper seat"}


def mixed(dl: DeckLayout) -> bool:
    """Both berths and seats on this deck (then each cell says which it is)."""
    kinds = {s.kind for s in dl.seats} - {"unknown"}
    return "sleeper" in kinds and len(kinds) > 1


def deck_plan(dl: DeckLayout) -> str:
    """'left: single sleeper berths (window) · right: 2 seats side by side', as on the site's map."""
    groups = sides(dl)
    if len(groups) != 2:
        kinds = {s.kind for s in dl.seats} - {"unknown"}
        return " and ".join(sorted(_KIND_WORD[k] + "s" for k in kinds)) if kinds else ""
    parts = []
    for name, cols in zip(("left", "right"), groups, strict=True):
        kinds = sorted({s.kind for s in dl.seats if s.col in cols} - {"unknown"})
        what = " and ".join(_KIND_WORD[k] + "s" for k in kinds) or "seats"
        n = len(cols)
        if n == 1:
            across = f"single {what} (window)"
        elif len(kinds) == 1:
            across = f"{n} {what} side by side"
        else:
            across = f"{n} across ({what})"
        parts.append(f"{name}: {across}")
    return " · ".join(parts)


def _cell(s: Seat | None, width: int, kinds: bool = False) -> str:
    """'[12F ₹850]' for a free ladies seat, '[12 --]' sold, '[12 OK]' selected; blank if no seat.
    With `kinds` (decks with berths and seats), berths get 'B' and seats 'S': '[L1 B ₹398]'."""
    if s is None:
        return " " * width
    flag = {"female": "F", "male": "M"}.get(s.reserved_for or "", "")
    if kinds and s.kind != "unknown":
        flag += " B" if s.kind == "sleeper" else " S"
    if s.status == "sold":
        text = f"{s.number}{flag} --"
    elif s.status == "selected":
        text = f"{s.number}{flag} OK"
    else:
        text = f"{s.number}{flag} ₹{s.price:g}" if s.price is not None else f"{s.number}{flag}"
    return f"[{text}]".ljust(width)


def draw(dl: DeckLayout) -> str:
    """Text diagram of one deck, front at the top, aisle marked with '|'."""
    if not dl.positioned:
        cells = [_cell(s, 0).strip() for s in dl.seats]
        lines = [" ".join(cells[i : i + 6]) for i in range(0, len(cells), 6)]
        return "\n".join(["(seat positions not given; listed in number order)", *lines])
    kinds = mixed(dl)
    width = max(len(_cell(s, 0, kinds)) for s in dl.seats) + 1
    grid: dict[tuple[int, int], Seat] = {(s.row, s.col): s for s in dl.seats}
    lines = ["FRONT (driver)"]
    gap_after = {-a - 1 for a in dl.aisle_cols if a < 0}
    for r in range(dl.rows):
        parts = []
        for c in range(dl.cols):
            seat = grid.get((r, c))
            if c in dl.aisle_cols and seat is None:
                parts.append("|".center(width))
            else:
                parts.append(_cell(seat, width, kinds))
            if c in gap_after:
                parts.append(" | ")
        lines.append("".join(parts).rstrip())
    lines.append("BACK")
    return "\n".join(lines)


def _tcell(s: Seat | None, above: Seat | None) -> str:
    """One table cell: '🛏 L1 ₹398', '💺 L4 ♀ ₹232', '~~L2~~ sold', '✅ L3'; '⋮' under a long berth."""
    if s is None:
        return "⋮" if above is not None and above.kind == "sleeper" else " "
    icon = {"sleeper": "🛏", "seater": "💺", "semi_sleeper": "💺"}.get(s.kind, "")
    flag = {"female": " ♀", "male": " ♂"}.get(s.reserved_for or "", "")
    if s.status == "sold":
        return f"~~{s.number}~~ sold"
    if s.status == "selected":
        return f"✅ {s.number}{flag}"
    price = f" ₹{s.price:g}" if s.price is not None else ""
    return f"{icon} **{s.number}**{flag}{price}".strip()


def table(dl: DeckLayout) -> str:
    """Markdown table of one deck (front at the top), one column per seat column plus the aisle.
    Chat apps show tables as they are, so the layout and every price survive."""
    if not dl.positioned:
        return " · ".join(_tcell(s, None) for s in dl.seats)
    grid: dict[tuple[int, int], Seat] = {(s.row, s.col): s for s in dl.seats}
    empty = {a for a in dl.aisle_cols if a >= 0}
    after = {-a - 1 for a in dl.aisle_cols if a < 0}
    cols: list[int | None] = []  # None = an aisle column added between two seat columns
    for c in range(dl.cols):
        cols.append(c)
        if c in after:
            cols.append(None)

    def head(c: int | None) -> str:
        if c is None or c in empty:
            return "aisle"
        here = [s for s in dl.seats if s.col == c]
        if any(s.single for s in here):
            return "single (window)"
        return "window" if c in (0, dl.cols - 1) else "aisle side"

    lines = ["| " + " | ".join(head(c) for c in cols) + " |", "|" + "---|" * len(cols)]
    for r in range(dl.rows):
        cells = []
        for c in cols:
            if c is None or (c in empty and (r, c) not in grid):
                cells.append(" ")
            else:
                cells.append(_tcell(grid.get((r, c)), grid.get((r - 1, c))))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


TABLE_KEY = (
    "🛏 sleeper berth (lie down) · 💺 seat · ♀/♂ women/men only · ✅ selected · ~~sold~~ · ⋮ same berth"
)


def suggest(
    seats: list[Seat], wanted: int, prefs: list[Preference], decks: list[DeckLayout]
) -> list[Seat] | None:
    """Pick `wanted` available, unreserved seats that best fit the preferences.

    Ladies/gents-only seats are left out, since the passengers' genders aren't known
    here. Returns None when not enough seats are free. For several seats, the best
    contiguous group in one row (not split by the aisle) wins; otherwise the
    individually best seats are returned.
    """
    free = [s for s in seats if s.status == "available" and s.reserved_for is None]
    if len(free) < wanted or wanted < 1:
        return None
    # A bed (or a seat) is what was asked for: keep to that kind whenever enough of it is free,
    # so "together" can't trade berths for seats.
    for pref, kinds in (("sleeper", {"sleeper"}), ("seater", {"seater", "semi_sleeper"})):
        same = [s for s in free if s.kind in kinds]
        if pref in prefs and len(same) >= wanted:
            free = same
    rows_by_deck = {d.deck: max(d.rows, 1) for d in decks}
    prices = [s.price for s in free if s.price is not None]
    lo, hi = (min(prices), max(prices)) if prices else (0, 0)

    # Preference points: position preferences are worth 2, deck 3, seat kind 5, so a bed
    # outweighs a window, and the deck outweighs front/back.
    def score(s: Seat) -> float:
        p = 0.0
        rows = rows_by_deck.get(s.deck, 1)
        pos = (s.row / max(rows - 1, 1)) if s.row is not None else 0.5  # 0 = front
        if "window" in prefs and s.window:
            p += 2
        if "aisle" in prefs and s.aisle:
            p += 2
        if "front" in prefs:
            p += 2 * (1 - pos)
        if "back" in prefs:
            p += 2 * pos
        if "lower" in prefs and s.deck == "lower":
            p += 3
        if "upper" in prefs and s.deck == "upper":
            p += 3
        if "sleeper" in prefs and s.kind == "sleeper":
            p += 5  # a bed matters more than position on mixed seater/sleeper buses
        if "seater" in prefs and s.kind in ("seater", "semi_sleeper"):
            p += 5
        if "cheapest" in prefs and s.price is not None and hi > lo:
            p += 2 * (hi - s.price) / (hi - lo)
        if s.row is not None and rows > 3 and pos > 0.9 and "back" not in prefs:
            p -= 0.5  # last row: more bumps, often no recline
        return p

    if wanted == 1:
        return [max(free, key=score)]
    # Prefer seats next to each other: same deck and row, neighbouring columns, no aisle between.
    if all(s.row is not None for s in free):
        best: tuple[float, list[Seat]] | None = None
        by_row: dict[tuple[str, int], list[Seat]] = {}
        for s in free:
            by_row.setdefault((s.deck, s.row), []).append(s)
        aisles = {d.deck: d.aisle_cols for d in decks}
        for (deck, _), row_seats in by_row.items():
            row_seats.sort(key=lambda s: s.col)
            for i in range(len(row_seats) - wanted + 1):
                group = row_seats[i : i + wanted]
                cols = [s.col for s in group]
                contiguous = all(b - a == 1 for a, b in zip(cols, cols[1:], strict=False))
                crosses = any(
                    (a >= 0 and min(cols) < a < max(cols)) or (a < 0 and min(cols) <= -a - 1 < max(cols))
                    for a in aisles.get(deck, set())
                )
                if contiguous and not crosses:
                    total = sum(score(s) for s in group)
                    if best is None or total > best[0]:
                        best = (total, group)
        if best:
            return best[1]
    return sorted(free, key=score, reverse=True)[:wanted]


def describe(s: Seat) -> str:
    """'15 (window, row 3, ₹697)'; the deck is named for berths and upper-deck seats."""
    bits = [_KIND_WORD[s.kind]] if s.kind in _KIND_WORD else []
    if s.single:
        bits.append("single, window")
    else:
        if s.window:
            bits.append("window")
        if s.aisle:
            bits.append("aisle")
    if s.deck != "lower" or s.kind == "sleeper":  # (a berth's deck matters: upper means climbing)
        bits.append(f"{s.deck} deck")
    if s.row is not None:
        bits.append(f"row {s.row + 1}")
    price = f", ₹{s.price:g}" if s.price is not None else ""
    return f"{s.number} ({', '.join(bits) or 'seat'}{price})"
