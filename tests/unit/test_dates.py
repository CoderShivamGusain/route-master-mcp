from datetime import date, timedelta

import pytest

from mcp_route_master.places.dates import (
    DateError,
    goibibo_date,
    human_date,
    mmt_date,
    parse_travel_date,
    redbus_date,
)

TODAY = date(2026, 10, 31)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-11-02", date(2026, 11, 2)),
        ("02-11-2026", date(2026, 11, 2)),
        ("02/11/2026", date(2026, 11, 2)),
        ("today", TODAY),
        ("Tomorrow", TODAY + timedelta(days=1)),
        (" day after  tomorrow ", TODAY + timedelta(days=2)),
    ],
)
def test_parse_valid(text, expected):
    assert parse_travel_date(text, today=TODAY) == expected


@pytest.mark.parametrize("text", ["", "next friday", "2026-13-01", "31/02/2026"])
def test_parse_invalid(text):
    with pytest.raises(DateError):
        parse_travel_date(text, today=TODAY)


def test_past_and_far_future_rejected():
    with pytest.raises(DateError, match="past"):
        parse_travel_date("2026-10-30", today=TODAY)
    with pytest.raises(DateError, match="days away"):
        parse_travel_date("2027-10-30", today=TODAY)


def test_provider_formats_are_locale_independent():
    d = date(2026, 11, 2)
    assert redbus_date(d) == "02-Nov-2026"
    assert mmt_date(d) == "02-11-2026"
    assert goibibo_date(d) == "20261102"
    assert human_date(d) == "Mon, 2 Nov 2026"
