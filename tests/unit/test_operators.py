import pytest

from mcp_route_master.trust.operators import assess, bayesian_rating, match_government


@pytest.mark.parametrize(
    "name,short",
    [
        ("HRTC", "HRTC"),
        ("HRTC Buses", "HRTC"),
        ("Himachal Road Transport Corporation", "HRTC"),
        ("Himachal Pradesh Tourism Development Corporation (HPTDC) - 49531", "HPTDC"),
        ("UPSRTC", "UPSRTC"),
        ("TSRTC", "TGSRTC"),
    ],
)
def test_real_government_operators(name, short):
    gov, lookalike = match_government(name)
    assert [o.short for o in gov] == [short] and not lookalike


def test_ksrtc_alone_is_both_states():
    gov, _ = match_government("KSRTC")
    assert {o.state for o in gov} == {"Karnataka", "Kerala"}


@pytest.mark.parametrize(
    "name", ["KSRTC Express Line", "Shivneri Travels", "HRTC Volvo Tours", "UPSRTC Janrath Tours"]
)
def test_copycat_names_are_flagged(name):
    gov, lookalike = match_government(name)
    assert not gov and lookalike


@pytest.mark.parametrize(
    "name", ["Laxmi Holidays Pvt Ltd", "Himachal Travels", "Punjab Travels", "zingbus plus"]
)
def test_ordinary_private_names_are_not_flagged(name):
    assert match_government(name) == ([], [])


def test_bayesian_rating_discounts_few_reviews():
    assert bayesian_rating(5.0, 3) < bayesian_rating(4.3, 600)
    assert bayesian_rating(4.8, 10_000) == pytest.approx(4.8, abs=0.02)


def test_well_reviewed_private_operator_scores_high():
    a = assess(
        "Laxmi Holidays Pvt Ltd",
        rating=4.8,
        review_count=626,
        has_live_tracking=True,
        boarding_point="ISBT Kashmiri Gate",
    )
    assert a.level == "HIGH" and a.score >= 75 and not a.warnings


def test_new_five_star_listing_is_not_trusted():
    a = assess("Shiny New Travels", rating=5.0, review_count=3)
    assert a.level != "HIGH"
    assert any("Only 3 reviews" in w for w in a.warnings)


def test_highway_boarding_and_semi_sleeper_warnings():
    a = assess(
        "X Travels",
        rating=4.5,
        review_count=500,
        boarding_point="Near toll plaza, NH-44 bypass",
        bus_type="Volvo Semi-Sleeper (2+2)",
    )
    assert any("highway" in w for w in a.warnings)
    assert any("semi-sleeper" in w for w in a.warnings)


def test_name_only_private_operator_needs_more_info():
    a = assess("Laxmi Holidays Pvt Ltd")
    assert a.level == "NOT_ENOUGH_INFO" and a.score is None


def test_copycat_with_no_data_is_caution():
    assert assess("KSRTC Express Line").level == "CAUTION"


def test_invalid_rating():
    with pytest.raises(ValueError):
        assess("X", rating=7)


def test_government_name_alone_is_capped_below_high():
    a = assess("HRTC")
    assert a.level == "MODERATE"
    assert assess("HRTC", rating=4.4, review_count=45000).level == "HIGH"
