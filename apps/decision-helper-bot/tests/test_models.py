import pytest

from decision_helper.models import (
    closing_value,
    extraction_from_payload,
    grade_pick,
    implied_probability,
    review_from_payload,
    team_matches,
)
from helpers import extraction_payload, make_events


def test_grade_winner_spread_total_and_push() -> None:
    common = dict(away_team="A", home_team="H", away_score=20, home_score=24)
    assert grade_pick(pick_type="winner", selection="H", line=None, **common) == "win"
    assert grade_pick(pick_type="winner", selection="A", line=None, **common) == "loss"
    assert grade_pick(pick_type="spread", selection="A", line=4.0, **common) == "push"
    assert grade_pick(pick_type="spread", selection="A", line=4.5, **common) == "win"
    assert grade_pick(pick_type="spread", selection="H", line=-4.5, **common) == "loss"
    assert grade_pick(pick_type="total", selection="Over", line=43.5, **common) == "win"
    assert grade_pick(pick_type="total", selection="Under", line=43.5, **common) == "loss"
    assert grade_pick(pick_type="total", selection="Under", line=None, **common) == "ungradeable"


def test_closing_value_direction() -> None:
    # Took +3, closed +1.5: beat the close by 1.5 points.
    assert closing_value(
        pick_type="spread", selection="A", pick_line=3.0, closing_line=1.5,
        pick_price=None, closing_price=None,
    ) == 1.5
    assert closing_value(
        pick_type="total", selection="Over", pick_line=45.0, closing_line=47.0,
        pick_price=None, closing_price=None,
    ) == 2.0
    assert closing_value(
        pick_type="total", selection="Under", pick_line=45.0, closing_line=47.0,
        pick_price=None, closing_price=None,
    ) == -2.0
    value = closing_value(
        pick_type="winner", selection="A", pick_line=None, closing_line=None,
        pick_price=120, closing_price=-110,
    )
    assert value == pytest.approx(implied_probability(-110) - implied_probability(120))
    assert value > 0


def test_extraction_validates_and_normalizes() -> None:
    extraction = extraction_from_payload(extraction_payload(), make_events())
    assert len(extraction.picks) == 3
    assert extraction.picks[1].selection == "Under"
    assert (extraction.picks[0].my_line_low, extraction.picks[0].my_line_high) == (3.5, 4.5)
    assert (extraction.picks[1].my_line_low, extraction.picks[1].my_line_high) == (44, 44)
    assert extraction.picks[2].my_line_low is None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("event_id", "unknown", "not a candidate"),
        ("selection", "Miami Dolphins", "candidate team"),
        ("pick_type", "prop", "unsupported"),
        ("confidence_probability", 1.5, "0-1"),
    ],
)
def test_extraction_rejects_invented_data(field, value, message) -> None:
    payload = extraction_payload()
    payload["picks"][0][field] = value
    with pytest.raises(ValueError, match=message):
        extraction_from_payload(payload, make_events())


def test_winner_line_rejected() -> None:
    payload = extraction_payload()
    payload["picks"][2]["line"] = -3
    with pytest.raises(ValueError, match="null for winner"):
        extraction_from_payload(payload, make_events())


def test_review_validation() -> None:
    review = review_from_payload(
        {
            "process_score": 4,
            "luck_summary": "Lost on a late fumble",
            "what_worked": "Price aware",
            "what_to_improve": "Size down",
            "bias_tags": ["recency"],
        }
    )
    assert review.bias_tags == ("recency",)
    with pytest.raises(ValueError):
        review_from_payload({"process_score": 9})


def test_team_matching_handles_espn_variants() -> None:
    assert team_matches("LA Clippers", "Los Angeles Clippers")
    assert not team_matches("Los Angeles Rams", "Los Angeles Chargers")
