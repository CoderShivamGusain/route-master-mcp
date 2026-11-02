"""Resolve free-text place names to curated bus hubs (offline, no network).

Every tool that takes an origin or destination goes through `Resolver.resolve`.
A "hub" is a city or town from the bundled `hubs.json` (plus any local
`locations.json` overrides, see `common/store.py`), together with its aliases, bus
terminals, areas and the per-platform IDs that `places/links.py` needs to build dated
search links.

Order: exact name/alias/terminal/area -> noise-word stripping -> known place
inside a longer phrase -> fuzzy match. Ambiguous names (e.g. "Aurangabad")
are returned as ambiguous instead of guessed.

Design notes:
  * Silently picking the wrong city sends the traveller to the wrong search
    page, so every uncertain step (two hubs, a typo that could be another real
    city) returns "ambiguous" or "not_found" with suggestions rather than a guess.
  * Fuzzy matching is deliberately strict (high ratio, same first letter);
    see `_fuzzy`.
  * Overrides merge into the bundled data; a malformed overrides file is logged
    and ignored so a bad local edit can never break resolution.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp_route_master.common.store import load_overrides
from mcp_route_master.common.textutil import load_data, normalize

log = logging.getLogger(__name__)

MatchKind = Literal["name", "alias", "terminal", "area"]
# Words people add around a place name ("Pune bus stand", "from Delhi") that
# carry no location information; dropped before a second exact lookup.
_NOISE = {
    "bus",
    "buses",
    "stand",
    "station",
    "stop",
    "depot",
    "terminal",
    "terminus",
    "isbt",
    "city",
    "india",
    "from",
    "to",
    "the",
}
# Fuzzy-match thresholds (difflib SequenceMatcher ratio, 0..1):
_FUZZY_ACCEPT = 0.88  # auto-correct a typo only at or above this similarity
_SUGGEST_CUTOFF = 0.6  # below the accept bar but above this: offer as "did you mean"
_FUZZY_MIN_LEN = 4  # shorter queries are never auto-corrected (too many near neighbours)
_AMBIGUITY_MARGIN = 0.05  # hubs scoring within this of the best are treated as a tie


def _strip_noise(text: str) -> str:
    """Drop `_NOISE` words from already-normalised text."""
    return " ".join(t for t in text.split() if t not in _NOISE)


@dataclass
class Hub:
    """One curated place: a city or town with its bus terminals and booking-site IDs.

    `providers` maps a platform key ("redbus", "mmt", "goibibo") to that site's
    own identifier for the city. Only IDs checked on the live site belong here,
    because `places/links.py` builds URLs from them without further validation.
    """

    name: str
    state: str
    aliases: list[str] = field(default_factory=list)
    terminals: list[str] = field(default_factory=list)
    areas: list[str] = field(default_factory=list)
    providers: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Display name, with the state added unless the name already contains it."""
        return self.name if self.state.lower() in self.name.lower() else f"{self.name} ({self.state})"


@dataclass
class Resolution:
    """Outcome of resolving one place name.

    status "ok" sets `hub` (and `place` when a terminal/area was named);
    "ambiguous" lists `candidates`; "not_found" may carry `suggestions`.
    """

    query: str
    status: Literal["ok", "ambiguous", "not_found"]
    hub: Hub | None = None
    kind: MatchKind | None = None
    place: str | None = None  # terminal/area the user named, if any
    fuzzy: bool = False  # typo-corrected
    assumed: bool = False  # picked from a longer phrase ("Indore MP" -> Indore)
    reason: str = ""  # "multiple" when one field names two different places
    candidates: list[Hub] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Resolver:
    """In-memory index of all hubs, built once from bundled data plus local overrides.

    Pass `data` / `overrides` explicitly in tests; by default they are loaded
    from `hubs.json` and the user's `locations.json`.
    """

    def __init__(self, data: dict[str, Any] | None = None, overrides: dict[str, Any] | None = None):
        base = data if data is not None else load_data("hubs.json")
        extra = overrides if overrides is not None else load_overrides()
        self.hubs: list[Hub] = []
        self.routes: dict[str, dict[str, Any]] = {}
        self._merge(base)
        if extra:
            try:
                self._merge(extra)
            except (TypeError, KeyError, ValueError) as exc:
                log.warning("Ignoring malformed location overrides: %s", exc)
        # normalised text -> every (hub, how it matched, specific terminal/area) it can mean.
        # One key can map to several hubs, which is how ambiguity is detected.
        self._index: dict[str, list[tuple[Hub, MatchKind, str | None]]] = {}
        for hub in self.hubs:
            self._add(hub.name, hub, "name", None)
            for alias in hub.aliases:
                self._add(alias, hub, "alias", None)
            for terminal in hub.terminals:
                self._add(terminal, hub, "terminal", terminal)
                self._add(_strip_noise(normalize(terminal)), hub, "terminal", terminal)  # "Swargate"
            for area in hub.areas:
                self._add(area, hub, "area", area)

    # ------------------------------------------------------------------ build
    def _merge(self, data: dict[str, Any]) -> None:
        """Add hubs and route IDs from `data`; a hub with a known name is extended, not duplicated."""
        by_name = {h.name.lower(): h for h in self.hubs}
        for raw in data.get("hubs", []):
            name = str(raw["name"]).strip()
            existing = by_name.get(name.lower())
            if existing is None:
                hub = Hub(name=name, state=str(raw.get("state", "")).strip())
                self.hubs.append(hub)
                by_name[name.lower()] = hub
                existing = hub
            elif raw.get("state"):
                existing.state = str(raw["state"]).strip()
            for key in ("aliases", "terminals", "areas"):
                values = getattr(existing, key)
                values.extend(str(v) for v in raw.get(key, []) if str(v) not in values)
            existing.providers.update(raw.get("providers", {}))
        for provider, table in data.get("routes", {}).items():
            self.routes.setdefault(provider, {}).update(table)

    def _add(self, text: str, hub: Hub, kind: MatchKind, place: str | None) -> None:
        key = normalize(text)
        if not key:
            return
        entries = self._index.setdefault(key, [])
        if not any(h is hub for h, _, _ in entries):
            entries.append((hub, kind, place))

    # ---------------------------------------------------------------- resolve
    def resolve(self, query: str) -> Resolution:
        """Resolve free text to a hub, trying the stages in the order given in the module docstring.

        A state name in the query ("Aurangabad Bihar") is used last to settle an
        ambiguous match.
        """
        q = normalize(query or "")
        if not q:
            return Resolution(query=query, status="not_found")

        result = self._exact(query, q)
        if result is None:
            stripped = _strip_noise(q)
            if stripped and stripped != q:
                result = self._exact(query, stripped)
        if result is None:
            result = self._contained(query, q)
        if result is None:
            result = self._fuzzy(query, q)
        if result.status == "ambiguous":
            result = self._disambiguate_by_state(result, q)
        return result

    def _exact(self, query: str, key: str) -> Resolution | None:
        entries = self._index.get(key)
        if not entries:
            return None
        return self._from_entries(query, entries, fuzzy=False)

    def _contained(self, query: str, q: str) -> Resolution | None:
        """Known places mentioned as whole words, e.g. 'majnu ka tilla, delhi' or 'indore mp'."""
        # Space-padding turns substring search into whole-word search ("goalpara" must not hit "goa").
        padded = f" {q} "
        hits = [k for k in self._index if len(k) >= 3 and f" {k} " in padded]
        # "kashmiri gate" inside "isbt kashmiri gate" is the same mention, keep the longer one.
        hits = [k for k in hits if not any(k != other and f" {k} " in f" {other} " for other in hits)]
        if not hits:
            return None
        entries = [e for k in hits for e in self._index[k]]
        hubs = list({id(h): h for h, _, _ in entries}.values())
        if len(hubs) > 1:
            # Each hit is unique to one hub, so the phrase names several places rather than
            # one place that several hubs share.
            if all(len(self._index[k]) == 1 for k in hits):
                # Two different places in one field, e.g. "Delhi to Manali".
                return Resolution(query=query, status="ambiguous", candidates=hubs, reason="multiple")
            return self._from_entries(query, entries, fuzzy=False)
        res = self._from_entries(query, entries, fuzzy=False)
        # A city name found inside extra words is a guess worth telling the user about;
        # a named terminal or area is specific enough not to be.
        res.assumed = res.kind in ("name", "alias")
        return res

    def _fuzzy(self, query: str, q: str) -> Resolution:
        """Accept a typo only when it is very close and starts with the same letter.

        'Manli' -> Manali is fine; 'Raipur' -> Jaipur or 'Kannur' -> Kanpur are different
        real cities, so those come back as suggestions instead of a silent swap.
        """
        keys = list(self._index)
        scored = sorted(((difflib.SequenceMatcher(None, q, k).ratio(), k) for k in keys), reverse=True)
        if len(q) >= _FUZZY_MIN_LEN:
            # Best-scoring key per hub (scored is sorted, so the first one seen wins).
            best_by_hub: dict[int, tuple[float, str]] = {}
            for score, key in scored:
                if score < _FUZZY_ACCEPT:
                    break
                if key[0] != q[0]:
                    continue
                for hub, _, _ in self._index[key]:
                    best_by_hub.setdefault(id(hub), (score, key))
            if best_by_hub:
                ranked = sorted(best_by_hub.values(), reverse=True)
                top_score = ranked[0][0]
                close = [k for sc, k in ranked if top_score - sc < _AMBIGUITY_MARGIN]
                entries = [e for k in dict.fromkeys(close) for e in self._index[k]]
                return self._from_entries(query, entries, fuzzy=True)
        suggestions = [self._index[k][0][0].label for sc, k in scored[:8] if sc >= _SUGGEST_CUTOFF]
        return Resolution(query=query, status="not_found", suggestions=list(dict.fromkeys(suggestions))[:5])

    @staticmethod
    def _from_entries(
        query: str, entries: list[tuple[Hub, MatchKind, str | None]], fuzzy: bool
    ) -> Resolution:
        """Return "ok" when every entry points to the same hub, otherwise "ambiguous"."""
        hubs = list({id(h): h for h, _, _ in entries}.values())
        if len(hubs) > 1:
            return Resolution(query=query, status="ambiguous", candidates=hubs, fuzzy=fuzzy)
        # Prefer the specific terminal/area the user named over the bare city name.
        hub, kind, place = next((e for e in entries if e[2]), entries[0])
        return Resolution(query=query, status="ok", hub=hub, kind=kind, place=place, fuzzy=fuzzy)

    def _disambiguate_by_state(self, res: Resolution, q: str) -> Resolution:
        """Pick the one candidate whose state is named in the query, if exactly one is."""
        if res.reason == "multiple":
            return res
        padded = f" {q} "
        matching = [h for h in res.candidates if f" {normalize(h.state)} " in padded]
        if len(matching) == 1:
            return Resolution(query=res.query, status="ok", hub=matching[0], kind="name", fuzzy=res.fuzzy)
        return res

    # ----------------------------------------------------------------- lookup
    def route_id(self, provider: str, origin: Hub, destination: Hub) -> Any:
        """Verified per-route ID for platforms that key pages by route (AbhiBus), or None."""
        return self.routes.get(provider, {}).get(f"{origin.name}|{destination.name}")

    def is_known_terminal(self, text: str, hub: Hub | None = None) -> bool:
        """True when the boarding point text names a curated terminal."""
        q = f" {normalize(text)} "
        hubs = [hub] if hub else self.hubs
        return any(f" {normalize(t)} " in q for h in hubs for t in h.terminals)
