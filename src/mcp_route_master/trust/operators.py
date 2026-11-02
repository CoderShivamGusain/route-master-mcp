"""Operator trust signals.

The score is a summary of *signals the user can see on the booking site*, not
a guarantee. We never say "zero fraud risk": even government buses can be
resold by fake sites, which is what `trust/safety.py` checks.

Used by the check_operator tool directly and by `booking/ranking.py` for every listing.
`assess` starts from a neutral 50 and adds or subtracts points for:
  * a government (state RTC) name match, from `operators.json`; a name that only
    *resembles* an RTC is a warning, since private operators copy RTC names;
  * the star rating, weighted by review count (see `bayesian_rating`);
  * live GPS tracking;
  * the boarding point (a recognised bus station vs a highway/bypass pickup).
The result is clamped to 0-100 and mapped to HIGH (>= 75), MODERATE (>= 55)
or CAUTION. With neither a government match nor a rating the level is
NOT_ENOUGH_INFO: the other signals alone say too little about who runs the bus.

Why a Bayesian rating: a new listing with "5.0 from 3 reviews" would otherwise
outrank an operator with "4.3 from 2,000 reviews". The adjusted rating pulls
ratings with few reviews towards a cautious prior (`PRIOR_RATING`), and only
lets the listing's own rating dominate once it has well over
`REVIEW_THRESHOLD` reviews.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from typing import Literal

from mcp_route_master.common.textutil import GENERIC_WORDS, load_data, normalize
from mcp_route_master.trust.bus_types import BusType, classify

Level = Literal["HIGH", "MODERATE", "CAUTION", "NOT_ENOUGH_INFO"]

REVIEW_THRESHOLD = 100  # m in the Bayesian average; also the "too few reviews" warning line
PRIOR_RATING = 3.8  # C: what we assume before seeing enough reviews
# Words inside RTC names that private operators also use legitimately ("Swift Travels").
_COMMON_NAME_WORDS = {"swift", "kadamba"}
_STRIP_TOKENS = {"buses", "bus", "ltd", "limited", "pvt", "private"}  # dropped before name matching
# Boarding-point words that suggest a roadside pickup rather than a bus station. Such stops
# are harder to find at night and are where "boarding point changed" scams happen.
_HIGHWAY = re.compile(
    r"\b(bypass|by pass|toll|highway|nh\s*-?\s*\d+|flyover|petrol pump|fuel station|dhaba|"
    r"expressway|service road|outskirts?)\b"
)
_STATION = re.compile(r"\b(isbt|bus stand|bus station|bus terminus|bus terminal|bus depot|depot)\b")


@dataclass(frozen=True)
class GovOperator:
    """A state transport undertaking from `operators.json`, with its verified booking domains."""

    short: str
    name: str
    state: str
    domains: tuple[str, ...]


@dataclass
class Assessment:
    """Result of `assess`. `score` is None when the level is NOT_ENOUGH_INFO or a bare
    lookalike CAUTION; `adjusted_rating` is the Bayesian rating when a rating was given."""

    operator: str
    level: Level
    score: int | None
    government: list[GovOperator] = field(default_factory=list)
    lookalike_of: list[GovOperator] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bus_type: BusType | None = None
    adjusted_rating: float | None = None


@cache
def _operators() -> tuple[GovOperator, ...]:
    """All government operators, loaded once."""
    data = load_data("operators.json")
    return tuple(
        GovOperator(o["short"], o["name"], o["state"], tuple(o.get("domains", [])))
        for o in data["government_operators"]
    )


@cache
def _name_index() -> tuple[dict[str, list[GovOperator]], dict[str, list[GovOperator]]]:
    """(exact normalised name -> operators, distinctive brand token -> operators)."""
    raw = load_data("operators.json")["government_operators"]
    ops = _operators()
    exact: dict[str, list[GovOperator]] = {}
    brands: dict[str, list[GovOperator]] = {}
    for spec, op in zip(raw, ops, strict=True):
        for name in [*spec["names"], spec["name"], spec["short"]]:
            key = _clean(name)
            exact.setdefault(key, [])
            if op not in exact[key]:
                exact[key].append(op)
            # Distinctive tokens ("hrtc", "msrtc") let a partial name be flagged as a
            # lookalike; generic words ("state", "transport", state names) would match everyone.
            for token in key.split():
                if len(token) >= 3 and token not in GENERIC_WORDS and token not in _COMMON_NAME_WORDS:
                    brands.setdefault(token, [])
                    if op not in brands[token]:
                        brands[token].append(op)
    # "KSRTC" alone is used by both Karnataka and Kerala.
    exact.setdefault("ksrtc", [o for o in ops if o.short.startswith("KSRTC")])
    brands.setdefault("ksrtc", [o for o in ops if o.short.startswith("KSRTC")])
    return exact, brands


def _clean(name: str) -> str:
    """Normalise an operator name for matching: drop company suffixes and bare numbers."""
    tokens = [t for t in normalize(name).split() if t not in _STRIP_TOKENS and not t.isdigit()]
    return " ".join(tokens)


def match_government(operator_name: str) -> tuple[list[GovOperator], list[GovOperator]]:
    """Return (exact government matches, government operators the name only resembles)."""
    exact, brands = _name_index()
    key = _clean(operator_name)
    if key in exact:
        return exact[key], []
    resembles: list[GovOperator] = []
    for token in key.split():
        for op in brands.get(token, []):
            if op not in resembles:
                resembles.append(op)
    # A full multi-word RTC name inside a longer name ("... Kerala State Road Transport ... Travels").
    # Short or single-word names are skipped; the token check above already covers them.
    for full, ops in exact.items():
        if len(full) > 6 and " " in full and f" {full} " in f" {key} ":
            resembles.extend(o for o in ops if o not in resembles)
    return [], resembles


def bayesian_rating(rating: float, reviews: int) -> float:
    """Weighted average of the listing's rating and `PRIOR_RATING`: (v*R + m*C) / (v + m).

    With v = 0 reviews the result is the prior; at v = m it is halfway; with many
    reviews it approaches the listing's own rating.
    """
    v, m = max(reviews, 0), REVIEW_THRESHOLD
    return (v / (v + m)) * rating + (m / (v + m)) * PRIOR_RATING


def assess(
    operator_name: str,
    rating: float | None = None,
    review_count: int | None = None,
    has_live_tracking: bool | None = None,
    boarding_point: str | None = None,
    bus_type: str | None = None,
    is_known_terminal: Callable[[str], bool] | None = None,
) -> Assessment:
    """Score the signals the user can see on a listing.

    `is_known_terminal` (usually `Resolver.is_known_terminal`) lets a curated terminal
    name count as a bus station even without words like "bus stand". Raises
    ValueError for a rating outside 0-5.
    """
    gov, lookalike = match_government(operator_name)
    result = Assessment(
        operator=operator_name.strip(),
        level="NOT_ENOUGH_INFO",
        score=None,
        government=gov,
        lookalike_of=lookalike,
    )
    score = 50

    if gov:
        score += 30
        names = " / ".join(o.short for o in gov)
        result.positives.append(
            f"Name matches government operator {names}. Confirm the site marks it as a "
            "government/RTC bus, and book only on an official site."
        )
    elif lookalike:
        score -= 15
        names = ", ".join(o.short for o in lookalike)
        result.warnings.append(
            f"Name looks like government operator {names} but isn't an exact match. Private "
            "operators sometimes copy RTC names; check for the site's RTC/government label."
        )

    if rating is not None:
        if not 0 <= rating <= 5:
            raise ValueError("rating must be between 0 and 5")
        reviews = review_count or 0
        adjusted = bayesian_rating(rating, reviews)
        result.adjusted_rating = round(adjusted, 2)
        # 3.5 is neutral; each 0.1 star above or below moves the score by 2, capped at +/-25
        # (reached at 2.25 and 4.75).
        score += round(max(-25.0, min(25.0, (adjusted - 3.5) * 20)))
        if reviews < REVIEW_THRESHOLD:
            result.warnings.append(
                f"Only {reviews} reviews: a {rating:.1f} rating isn't reliable yet "
                f"(counts as {adjusted:.1f}). New listings can hide a bad history."
            )
            if reviews < 20:
                score -= 10
        elif adjusted >= 4.2:
            result.positives.append(f"Rated {rating:.1f} from {reviews} reviews.")
        if rating < 3.5:
            result.warnings.append(f"Low rating ({rating:.1f}/5).")
    elif review_count is not None:
        result.warnings.append("Give the star rating too, so reviews can be weighed.")

    if has_live_tracking is not None:
        if has_live_tracking:
            score += 8
            result.positives.append("Live GPS tracking offered.")
        else:
            score -= 5
            result.warnings.append("No live tracking listed.")

    if boarding_point:
        text = normalize(boarding_point)
        if _HIGHWAY.search(text):
            score -= 10
            result.warnings.append(
                f"Boarding at '{boarding_point}' looks like a highway/bypass stop. Confirm the "
                "exact spot with the operator through the booking site, and be wary of last-minute "
                "changes sent by SMS or WhatsApp."
            )
        elif (is_known_terminal and is_known_terminal(boarding_point)) or _STATION.search(text):
            score += 5
            result.positives.append(f"Boards at a recognised bus station ({boarding_point}).")

    if bus_type:
        result.bus_type = classify(bus_type)
        if result.bus_type.berth == "semi_sleeper":
            result.warnings.append("This is a semi-sleeper: a reclining seat, not a sleeper berth.")

    # A score needs either a government match or a rating; tracking or a boarding
    # point alone says too little about who runs the bus.
    if not gov and rating is None:
        result.level = "CAUTION" if lookalike else "NOT_ENOUGH_INFO"
        return result

    if gov and rating is None and has_live_tracking is None and not boarding_point:
        # A name alone is easy to type into a fake listing, so it can't earn HIGH by itself.
        score = min(score, 70)
    result.score = max(0, min(100, score))
    result.level = "HIGH" if result.score >= 75 else "MODERATE" if result.score >= 55 else "CAUTION"
    return result
