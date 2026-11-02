"""Classify bus-type labels from booking sites.

Key trap: "Semi-Sleeper" contains the word "sleeper" but is a reclining seat,
not a berth. Delhi -> Manali on 31 Oct 2026 listed 118 buses, many called
"Semi-Sleeper", and zero real sleepers.

Sites label buses with free text ("Volvo 9600 A/C Multi-Axle Semi Sleeper
(2+2)"), so `classify` pulls out AC, berth type, seat layout and chassis with
regexes. `matches` checks a label against the traveller's preference and is
three-valued: True / False when the label is clear, None when it doesn't say,
so callers (`booking/ranking.py`, `trust/operators.py`) can rank unknowns between matches
and mismatches instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Berth = Literal["sleeper", "semi_sleeper", "seater", "seater_sleeper", "unknown"]
BusPreference = Literal[
    "any", "ac", "non_ac", "sleeper", "ac_sleeper", "non_ac_sleeper", "semi_sleeper", "seater", "volvo"
]

# Checked before _AC, since "Non A/C" also contains "A/C". Matches "non ac", "non-a/c",
# "non a c", "non air conditioned".
_NON_AC = re.compile(r"\bnon[\s-]*a\s*/?\s*c\b|\bnon[\s-]*air[\s-]*condition")
_AC = re.compile(r"\ba\s*/\s*c\b|\bac\b|air[\s-]*condition")
_SEMI = re.compile(r"semi[\s-]*sleeper")
_SLEEPER = re.compile(r"\bsleeper\b|\bberth\b")  # applied only after "semi sleeper" is removed
_SEATER = re.compile(r"\bseater\b|\bpush[\s-]*back\b|\bseat\b")
# Seat/berth layout either side of the aisle, e.g. "2+2" or "2+1".
_LAYOUT = re.compile(r"\b([12])\s*\+\s*([12])\b")
_CHASSIS = {
    "volvo": re.compile(r"\bvolvo\b"),
    "scania": re.compile(r"\bscania\b"),
    "mercedes-benz": re.compile(r"\bmercedes\b"),
    "bharatbenz": re.compile(r"\bbharat\s*benz\b"),
    "ashok leyland": re.compile(r"\bashok\s*leyland\b"),
    "tata": re.compile(r"\btata\b"),
    "eicher": re.compile(r"\beicher\b"),
}
# Coach chassis sold in India essentially only as AC buses, so AC is assumed (and
# marked as inferred) when the label names one but doesn't mention AC.
_PREMIUM = {"volvo", "scania", "mercedes-benz"}


@dataclass(frozen=True)
class BusType:
    """What a site's bus-type label says. `ac` is None when the label doesn't say;
    `ac_inferred` is True when AC was assumed from a premium chassis."""

    label: str
    ac: bool | None
    ac_inferred: bool
    berth: Berth
    layout: str | None
    chassis: str | None
    multi_axle: bool

    def describe(self) -> str:
        """Plain-English summary for the user, e.g. 'AC, sleeper berths, Volvo multi-axle, 2+1'."""
        parts = []
        if self.ac is True:
            parts.append("AC" + (" (assumed from chassis)" if self.ac_inferred else ""))
        elif self.ac is False:
            parts.append("Non-AC")
        parts.append(
            {
                "sleeper": "sleeper berths",
                "semi_sleeper": "semi-sleeper (reclining seat, not a berth)",
                "seater": "seats",
                "seater_sleeper": "mix of seats and sleeper berths",
                "unknown": "seat type unclear",
            }[self.berth]
        )
        if self.chassis:
            parts.append(self.chassis.title() + (" multi-axle" if self.multi_axle else ""))
        if self.layout:
            parts.append(self.layout)
        return ", ".join(parts)


def classify(label: str) -> BusType:
    """Parse a free-text bus-type label into a `BusType`."""
    text = (label or "").lower()
    chassis = next((name for name, rx in _CHASSIS.items() if rx.search(text)), None)

    if _NON_AC.search(text):
        ac, inferred = False, False
    elif _AC.search(text):
        ac, inferred = True, False
    elif chassis in _PREMIUM:
        ac, inferred = True, True
    else:
        ac, inferred = None, False

    # Remove "semi sleeper" first so its "sleeper" isn't counted as a real berth.
    # A label with both (e.g. "Semi Sleeper / Sleeper") is a mixed bus.
    without_semi = _SEMI.sub(" ", text)
    has_semi = bool(_SEMI.search(text))
    has_sleeper = bool(_SLEEPER.search(without_semi))
    has_seater = bool(_SEATER.search(without_semi))
    if has_sleeper and (has_seater or has_semi):
        berth: Berth = "seater_sleeper"
    elif has_sleeper:
        berth = "sleeper"
    elif has_semi:
        berth = "semi_sleeper"
    elif has_seater:
        berth = "seater"
    else:
        berth = "unknown"

    layout_match = _LAYOUT.search(text)
    layout = f"{layout_match.group(1)}+{layout_match.group(2)}" if layout_match else None
    multi_axle = bool(re.search(r"multi[\s-]*axle", text))
    return BusType(label, ac, inferred, berth, layout, chassis, multi_axle)


def matches(bus: BusType, preference: BusPreference) -> bool | None:
    """True/False when the label clearly does/doesn't fit; None when unclear."""
    if preference == "any":
        return True
    wants_ac = preference in {"ac", "ac_sleeper"}
    wants_non_ac = preference in {"non_ac", "non_ac_sleeper"}
    wants_sleeper = preference in {"sleeper", "ac_sleeper", "non_ac_sleeper"}

    if (wants_ac or wants_non_ac) and bus.ac is None:
        return None
    if wants_ac and bus.ac is False or wants_non_ac and bus.ac is True:
        return False
    if wants_sleeper:
        # A mixed seater/sleeper bus counts: the traveller can still pick a berth on it.
        if bus.berth == "unknown":
            return None
        return bus.berth in {"sleeper", "seater_sleeper"}
    if preference == "semi_sleeper":
        return None if bus.berth == "unknown" else bus.berth == "semi_sleeper"
    if preference == "seater":
        # A semi-sleeper is a (reclining) seat, so it satisfies a seater request.
        return None if bus.berth == "unknown" else bus.berth in {"seater", "semi_sleeper"}
    if preference == "volvo":
        return bus.chassis == "volvo"
    return True
