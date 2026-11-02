"""Small text helpers shared by the place resolver and the trust modules.

* `normalize` is the one canonical form used for every name comparison, so
  place and operator matching agree on punctuation, case and accents.
* `load_data` reads the bundled JSON files (hubs, operators) once per process.
* `edit_distance` powers lookalike-domain detection in `trust/safety.py`.
* `GENERIC_WORDS` keeps shared vocabulary from causing false brand matches.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import cache
from importlib import resources
from typing import Any

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Words that say nothing about *which* operator or site something is: state
# names and transport vocabulary. Used to avoid false "copycat" matches.
GENERIC_WORDS = frozenset(
    {
        "state",
        "road",
        "transport",
        "corporation",
        "roadways",
        "bus",
        "buses",
        "ltd",
        "limited",
        "pvt",
        "private",
        "online",
        "ebooking",
        "booking",
        "express",
        "tourism",
        "development",
        "the",
        "and",
        "of",
        "travels",
        "services",
        "public",
        "rtc",
        "himachal",
        "pradesh",
        "punjab",
        "haryana",
        "rajasthan",
        "kerala",
        "karnataka",
        "tamil",
        "nadu",
        "uttar",
        "bengal",
        "west",
        "north",
        "south",
        "western",
        "delhi",
        "assam",
        "bihar",
        "odisha",
        "telangana",
        "andhra",
        "maharashtra",
        "gujarat",
        "uttarakhand",
        "jammu",
        "kashmir",
        "goa",
        "kalyana",
        "india",
    }
)


def normalize(text: str) -> str:
    """Lower-case, strip accents and punctuation, collapse whitespace.

    NFKD splits accented letters into base letter + combining mark, and the marks
    are then dropped. Only a-z and 0-9 survive, so non-Latin scripts normalise to
    an empty string.

    "ISBT Kashmiri-Gate, Delhi" -> "isbt kashmiri gate delhi"
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _NON_ALNUM.sub(" ", text.lower()).strip()


@cache
def load_data(filename: str) -> dict[str, Any]:
    """Load a bundled JSON data file (read-only package data).

    Cached: callers share one dict per file and must not modify it.
    """
    raw = resources.files("mcp_route_master").joinpath("data", filename).read_text("utf-8")
    return json.loads(raw)


def edit_distance(a: str, b: str, limit: int | None = None) -> int:
    """Optimal-string-alignment distance: insert, delete, substitute, swap neighbours.

    Swapping two letters ("rebdus" for "redbus") counts as one edit, which is how
    look-alike domains are usually made. Returns early (limit + 1) past `limit`.
    """
    if a == b:
        return 0
    if limit is not None and abs(len(a) - len(b)) > limit:
        return limit + 1
    # Classic dynamic programming kept to three rows: current, previous, and the one
    # before that (needed for the transposition case).
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if limit is not None and min(cur) > limit:
            return limit + 1  # early exit: the whole row is already past the limit
        prev2, prev = prev, cur
    return prev[-1]
