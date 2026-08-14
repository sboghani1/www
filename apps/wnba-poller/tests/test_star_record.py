from wnba_poller.star_record import (
    build_star_grade,
    compute_star_record,
    format_star_record,
    parse_star_legs,
    strength_to_stars,
)


def test_strength_maps_to_stars_deterministically():
    assert strength_to_stars("strong") == 3
    assert strength_to_stars("moderate") == 2
    assert strength_to_stars("small") == 1
    assert strength_to_stars("watch") == 0
    assert strength_to_stars("pass") == 0
    assert strength_to_stars("") == 0
    assert strength_to_stars(None) == 0


def test_build_star_grade_reads_strength_and_result_per_leg():
    active = {
        "full_game_side_strength": "moderate",
        "full_game_total_strength": "watch",
        "first_half_total_strength": "small",
    }
    graded = {
        "side": {"result": "wrong"},
        "total": {"result": "right"},
    }
    fh = {"side": None, "total": {"result": "wrong"}}

    grade = build_star_grade(active, graded, fh)

    assert grade == "stars: side=2:wrong, total=0:right, fh_total=1:wrong"


def test_build_star_grade_empty_when_nothing_graded():
    assert build_star_grade({}, {"side": None, "total": None}, None) == ""


def test_parse_star_legs_reads_the_fragment():
    body = " side (small X) -- HIT. net: ok | stars: side=1:right, fh_total=1:wrong"
    assert parse_star_legs(body) == [(1, "right"), (1, "wrong")]
    assert parse_star_legs("no fragment here -- HIT") == []


_PATH210 = """# Notes For Model

model_lean: schema note with an example: ... | stars: side=3:right (illustrative).

# Past Events

110fademercury
wrong
tags
model_lean: side (small SPARKS -2) -- MISS; full total (watch OVER) -- HIT. net: x | stars: side=1:wrong, total=0:right, fh_total=1:wrong

111fadewings
right
tags
model_lean: side (moderate TEMPO +10) -- HIT. net: y | stars: side=2:right, total=1:right, fh_total=2:right

# Model Cache

# Upcoming Events

model_lean: pending, unresolved | stars: side=3:right
"""


def test_compute_star_record_scopes_to_past_events_and_tallies_by_tier():
    record = compute_star_record(_PATH210)

    # Only the two Past Events entries count (Notes example + Upcoming pending
    # are excluded). 110: side1 wrong, total0 (untallied), fh_total1 wrong.
    # 111: side2 right, total1 right, fh_total2 right.
    assert record["tiers"][1] == {"right": 1, "wrong": 2, "push": 0}
    assert record["tiers"][2] == {"right": 2, "wrong": 0, "push": 0}
    assert record["tiers"][3] == {"right": 0, "wrong": 0, "push": 0}
    assert record["overall"] == {"right": 3, "wrong": 2, "push": 0}
    assert record["rated_legs"] == 5  # the total=0 leg is unrated, not tallied


def test_format_star_record_is_readable():
    text = format_star_record(compute_star_record(_PATH210))
    assert "rated legs" in text
    assert "overall" in text
