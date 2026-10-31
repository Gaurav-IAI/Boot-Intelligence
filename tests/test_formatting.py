"""Indian digit grouping for every number a reader sees."""
import pytest

from app.formatting import indian, pct


@pytest.mark.parametrize("value, text", [
    (0, "0"), (999, "999"), (1000, "1,000"), (99999, "99,999"), (100000, "1,00,000"),
    (164986, "1,64,986"), (12543, "12,543"), (10000000, "1,00,00,000"), (-30052, "-30,052"), (None, "—"),
])
def test_indian_grouping(value, text):
    assert indian(value) == text


def test_pct():
    assert pct(61.43) == "61.4%" and pct(61.43, 2) == "61.43%" and pct(None) == "—"


def test_party_identity():
    from app.web.parties import NEUTRAL, party_color, party_name, same_party
    assert party_color("bjp") == party_color("BJP") != NEUTRAL and party_color("XYZ") == NEUTRAL
    assert party_name("INC") == "Indian National Congress" and party_name("XYZ") == "XYZ"
    # TRS renamed itself BRS (the ECI 2023 report prints BHRS): keeping a seat is a hold
    assert same_party("TRS", "BHRS") and same_party("BRS", "TRS") and not same_party("TRS", "INC")
