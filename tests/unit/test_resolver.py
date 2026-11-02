import pytest

from mcp_route_master.places.resolver import Resolver


@pytest.fixture(scope="module")
def resolver():
    return Resolver(overrides={})


@pytest.mark.parametrize(
    "query,city",
    [
        ("Delhi", "Delhi"),
        ("new delhi", "Delhi"),
        ("Bangalore", "Bengaluru"),
        ("BENGALURU", "Bengaluru"),
        ("Gurgaon", "Gurugram"),
        ("Banaras", "Varanasi"),
        ("Bombay", "Mumbai"),
        ("Calcutta", "Kolkata"),
        ("Trivandrum", "Thiruvananthapuram"),
        ("Pondy", "Puducherry"),
        ("Delhi bus stand", "Delhi"),
        ("Manali, HP", "Manali"),
        ("Majnu ka Tilla, Delhi", "Delhi"),
        ("Majestic", "Bengaluru"),
        ("Aurangabad, Bihar", "Aurangabad (Bihar)"),
        ("Aurangabad Maharashtra", "Chhatrapati Sambhajinagar"),
        ("Bangalore Majestic", "Bengaluru"),
        ("Indore MP", "Indore"),
        ("Pune station", "Pune"),
        ("Swargate", "Pune"),
        ("Anand Vihar", "Delhi"),
        ("Navi Mumbai", "Navi Mumbai"),
        ("Vashi", "Navi Mumbai"),
        ("Ooty", "Ooty"),
        ("Calicut", "Kozhikode"),
        ("Hampi", "Hosapete"),
        ("Coorg", "Madikeri"),
    ],
)
def test_resolves_aliases_and_places(resolver, query, city):
    res = resolver.resolve(query)
    assert res.ok, (query, res)
    assert res.hub.name == city


def test_terminal_keeps_boarding_point(resolver):
    res = resolver.resolve("Kashmiri Gate")
    assert res.ok and res.hub.name == "Delhi"
    assert res.kind == "terminal" and "Kashmiri Gate" in res.place


@pytest.mark.parametrize(
    "typo,city", [("Manli", "Manali"), ("Hydrabad", "Hyderabad"), ("Chandigrah", "Chandigarh")]
)
def test_typos_resolve_fuzzily(resolver, typo, city):
    res = resolver.resolve(typo)
    assert res.ok and res.hub.name == city and res.fuzzy


def test_ambiguous_name_is_not_guessed(resolver):
    res = resolver.resolve("Aurangabad")
    assert res.status == "ambiguous"
    assert {h.state for h in res.candidates} == {"Maharashtra", "Bihar"}


@pytest.mark.parametrize("query", ["", "zzzzqqq", "hotel near tea stall"])
def test_unknown_places_are_not_found(resolver, query):
    assert resolver.resolve(query).status == "not_found"


def test_overrides_add_new_place_and_ids():
    resolver = Resolver(
        overrides={
            "hubs": [
                {"name": "Kasol", "state": "Himachal Pradesh", "aliases": ["kasol village"]},
                {"name": "Manali", "areas": ["Solang"]},
            ],
            "routes": {"abhibus": {"Delhi|Kasol": 999}},
        }
    )
    assert resolver.resolve("kasol village").hub.name == "Kasol"
    solang = resolver.resolve("Solang")
    assert solang.hub.name == "Manali" and solang.kind == "area"
    delhi, kasol = resolver.resolve("Delhi").hub, resolver.resolve("Kasol").hub
    assert resolver.route_id("abhibus", delhi, kasol) == 999
    # existing data is kept when merging
    assert resolver.resolve("Old Manali").hub.name == "Manali"


def test_malformed_overrides_are_ignored():
    resolver = Resolver(overrides={"hubs": [{"no_name": True}]})
    assert resolver.resolve("Delhi").ok


def test_known_terminal(resolver):
    assert resolver.is_known_terminal("ISBT Kashmiri Gate, Gate No. 4")
    assert not resolver.is_known_terminal("Near Toll Plaza")


def test_bundled_data_is_consistent(resolver):
    names = [h.name for h in resolver.hubs]
    assert len(names) == len(set(names))
    assert all(h.state for h in resolver.hubs)
    for hub in resolver.hubs:
        rb = hub.providers.get("redbus")
        if rb:
            assert isinstance(rb["id"], int) and rb["slug"]


@pytest.mark.parametrize("query", ["Jaunpur", "Rajpura", "Main bus stand"])
def test_different_real_cities_are_not_swapped(resolver, query):
    res = resolver.resolve(query)
    assert not res.ok, (query, res.hub)


def test_close_but_different_city_is_offered_as_suggestion(resolver):
    res = resolver.resolve("Jaunpur")
    assert res.status == "not_found" and any("Jaipur" in s or "Kanpur" in s for s in res.suggestions)


@pytest.mark.parametrize("query", ["Delhi to Manali", "Delhi Manali"])
def test_two_places_in_one_field_are_rejected(resolver, query):
    res = resolver.resolve(query)
    assert res.status == "ambiguous" and res.reason == "multiple"


def test_place_picked_from_phrase_is_marked_assumed(resolver):
    assert resolver.resolve("Indore MP").assumed
    assert not resolver.resolve("Indore").assumed


@pytest.mark.parametrize(
    "query,city",
    [
        ("Raipur", "Raipur"),
        ("Kannur", "Kannur"),
        ("Gulbarga", "Kalaburagi"),
        ("Rajahmundry", "Rajamahendravaram"),
        ("Howrah", "Kolkata"),
        ("Bilaspur, Himachal Pradesh", "Bilaspur (Himachal Pradesh)"),
        ("Shillong", "Shillong"),
        ("Kasol", "Kasol"),
    ],
)
def test_added_cities(resolver, query, city):
    res = resolver.resolve(query)
    assert res.ok and res.hub.name == city


@pytest.mark.parametrize("query", ["Bilaspur", "Hamirpur"])
def test_duplicate_city_names_ask_which(resolver, query):
    assert resolver.resolve(query).status == "ambiguous"


def test_every_hub_and_alias_resolves_to_itself(resolver):
    """Guards the data file: no hub name or alias is swallowed by another hub."""
    for hub in resolver.hubs:
        res = resolver.resolve(hub.name)
        assert res.ok and res.hub is hub, hub.name
        for alias in hub.aliases:
            res = resolver.resolve(alias)
            assert hub in ([res.hub] if res.ok else res.candidates), (hub.name, alias)
