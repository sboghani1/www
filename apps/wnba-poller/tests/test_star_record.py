from wnba_poller.star_record import (
    compute_star_record,
    format_star_record,
    leg_result,
    leg_stars,
)


def test_leg_stars_counts_the_run_and_accepts_unicode_or_asterisks():
    assert leg_stars("side (★★ small TEMPO +10) -- HIT") == 2
    assert leg_stars("total (★ small UNDER 184) -- MISS") == 1
    assert leg_stars("1H (★★★ moderate OVER) -- MISS") == 3
    assert leg_stars("total (** small) -- HIT") == 2
    assert leg_stars("total (⭐⭐ small) -- HIT") == 2
    assert leg_stars("side (small, no rating) -- HIT") is None


def test_leg_result_reads_the_verdict_marker_not_the_net_summary():
    # "net: TRIPLE HIT" must not flip a leg that MISSED.
    assert leg_result("side (★★ small SKY +8) -- MISS (valks by 20)") == "wrong"
    assert leg_result("side (★★ small TEMPO +10) -- HIT (covered)") == "right"
    assert leg_result("total (★ small UNDER) -- PUSH (landed on 170)") == "push"
    assert leg_result("first-half (★ small OVER) -- HIT. net: side hit") == "right"
    assert leg_result("side (small) with no marker") is None


_PATH210 = """# Notes For Model

model_lean: schema description with a starred example: side (★★ small X) -- HIT.

# Past Events

110fademercury
wrong
tags
model_lean: side (★★ small SPARKS -2) -- MISS (mercury won); full total (★ watch OVER) -- HIT (181); first-half total (★ small OVER) -- MISS (H1 91). net: mixed.

111fadewings
right
tags
model_lean: side (★★ small TEMPO +10) -- HIT (covered); full total (★ small UNDER) -- HIT (182); first-half total (★★ small UNDER) -- HIT (H1 82). net: TRIPLE HIT.

# Model Cache

# Upcoming Events

model_lean: pending lean, no result yet: side (★★★ strong FUTURE +3) still open.
"""


def test_compute_star_record_scopes_to_past_events_and_tallies_by_tier():
    record = compute_star_record(_PATH210)

    # 110 (side ** MISS, full * HIT, 1H * MISS) + 111 (side ** HIT, full * HIT,
    # 1H ** HIT). The Notes example and the Upcoming pending lean are excluded.
    assert record["rated_legs"] == 6
    assert record["tiers"][1] == {"right": 2, "wrong": 1, "push": 0}
    assert record["tiers"][2] == {"right": 2, "wrong": 1, "push": 0}
    assert record["tiers"][3] == {"right": 0, "wrong": 0, "push": 0}
    assert record["overall"] == {"right": 4, "wrong": 2, "push": 0}


def test_format_star_record_is_readable():
    text = format_star_record(compute_star_record(_PATH210))
    assert "6 rated legs" in text
    assert "**" in text
    assert "overall" in text
