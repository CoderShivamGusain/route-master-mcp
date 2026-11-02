import pytest

from mcp_route_master.trust.bus_types import classify, matches


@pytest.mark.parametrize(
    "label,ac,berth",
    [
        ("Volvo 9600 Multi Axle Semi-Sleeper (2+2)", True, "semi_sleeper"),
        ("Volvo A/C Semi Sleeper (2+2)", True, "semi_sleeper"),
        ("A/C Sleeper (2+1)", True, "sleeper"),
        ("Bharat Benz A/C Seater /Sleeper (2+1)", True, "seater_sleeper"),
        ("NON A/C Sleeper (2+1)", False, "sleeper"),
        ("Non-AC Seater (2+3)", False, "seater"),
        ("Bharat Benz AC Seater", True, "seater"),
        ("Scania Multi-Axle", True, "unknown"),
        ("Express", None, "unknown"),
    ],
)
def test_classify(label, ac, berth):
    bt = classify(label)
    assert (bt.ac, bt.berth) == (ac, berth)


def test_semi_sleeper_is_not_a_sleeper():
    assert matches(classify("Volvo 9600 Multi Axle Semi-Sleeper (2+2)"), "sleeper") is False
    assert matches(classify("Volvo 9600 Multi Axle Semi-Sleeper (2+2)"), "ac_sleeper") is False
    assert matches(classify("A/C Sleeper (2+1)"), "ac_sleeper") is True
    assert matches(classify("NON A/C Sleeper (2+1)"), "ac_sleeper") is False


def test_details():
    bt = classify("Volvo 9600 Multi Axle Semi-Sleeper (2+2)")
    assert bt.chassis == "volvo" and bt.multi_axle and bt.layout == "2+2" and bt.ac_inferred
    assert classify("Bharat Benz A/C Semi Sleeper (2+2)").chassis == "bharatbenz"
    assert matches(classify("Express"), "ac") is None
