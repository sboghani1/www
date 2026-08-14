from wnba_poller.star_record import (
    build_star_grade,
    compute_star_record,
    format_star_record,
    format_stars_token,
    parse_star_legs,
    stars_from_summary,
    strength_to_stars,
    strip_stars_token,
)


def test_strength_maps_to_stars_for_fallback():
    assert strength_to_stars("strong") == 3
    assert strength_to_stars("moderate") == 2
    assert strength_to_stars("small") == 1
    assert strength_to_stars("watch") == 0
    assert strength_to_stars(None) == 0


def test_summary_token_round_trips_and_strips():
    token = format_stars_token({"side": 2, "total": 1, "fh_total": 3})
    assert token == "[stars: side=2, total=1, fh_total=3]"
    summary = f"Mystics +5.5, moderate. {token}"
    assert stars_from_summary(summary) == {"side": 2, "total": 1, "fh_total": 3}
    assert strip_stars_token(summary) == "Mystics +5.5, moderate."
    assert stars_from_summary("no token here") == {}


def test_build_star_grade_prefers_summary_stars_over_strength():
    # side is 2-star conviction despite "small" strength (the decoupled case).
    active = {
        "summary": "small fade of a firm line [stars: side=2, fh_total=3]",
        "full_game_side_strength": "small",
        "full_game_total_strength": "small",
        "first_half_total_strength": "small",
    }
    graded = {"side": {"result": "right"}, "total": {"result": "wrong"}}
    fh = {"side": None, "total": {"result": "wrong"}}

    grade = build_star_grade(active, graded, fh)

    # side=2 (from token), total=1 (strength fallback), fh_total=3 (token).
    assert grade == "stars: side=2:right, total=1:wrong, fh_total=3:wrong"


def test_build_star_grade_falls_back_to_strength_without_a_token():
    active = {
        "summary": "pre-star lean, no token",
        "full_game_side_strength": "moderate",
        "full_game_total_strength": "watch",
    }
    graded = {"side": {"result": "wrong"}, "total": {"result": "right"}}
    assert build_star_grade(active, graded, None) == (
        "stars: side=2:wrong, total=0:right"
    )


def test_parse_star_legs_reads_the_model_lean_fragment():
    body = " side (small X) -- HIT | stars: side=2:right, fh_total=3:wrong"
    assert parse_star_legs(body) == [(2, "right"), (3, "wrong")]


_PATH210 = """# Notes For Model

model_lean: schema note ... | stars: side=3:right (illustrative).

# Past Events

110
wrong
tags
model_lean: side (small SPARKS -2) -- MISS | stars: side=2:wrong, total=1:right, fh_total=1:wrong

111
right
tags
model_lean: side (small TEMPO +10) -- HIT | stars: side=2:right, total=1:right, fh_total=2:right

# Model Cache

# Upcoming Events

model_lean: pending | stars: side=3:right
"""


def test_compute_star_record_scopes_to_past_events_and_tallies_by_tier():
    record = compute_star_record(_PATH210)

    # 110: side2 wrong, total1 right, fh_total1 wrong. 111: side2 right,
    # total1 right, fh_total2 right. (Notes + Upcoming excluded.)
    assert record["tiers"][1] == {"right": 2, "wrong": 1, "push": 0}
    assert record["tiers"][2] == {"right": 2, "wrong": 1, "push": 0}
    assert record["tiers"][3] == {"right": 0, "wrong": 0, "push": 0}
    assert record["overall"] == {"right": 4, "wrong": 2, "push": 0}
    assert record["rated_legs"] == 6


def test_format_star_record_is_readable():
    text = format_star_record(compute_star_record(_PATH210))
    assert "rated legs" in text
    assert "overall" in text
