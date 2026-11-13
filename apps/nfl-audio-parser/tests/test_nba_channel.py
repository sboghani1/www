from __future__ import annotations

from datetime import UTC, date, datetime

from nfl_audio_parser.nba_channel import (
    CHANNELS,
    build_payload,
    candidate_schedule,
    classify,
    parse_json_object,
    quote_evidence,
    title_date,
    validate_payload,
)

CHANNEL = CHANNELS["docs-sports"]


def entry(title: str, availability: str | None = None) -> dict:
    return {"id": "abc123", "title": title, "availability": availability}


def test_title_date_reads_numeric_and_spelled_dates() -> None:
    assert title_date("Monday NBA Picks - 11/9/26 l Craig Trapp", 2026) == date(2026, 11, 9)
    assert title_date(
        "Wednesday NBA Picks Pacers vs. Pelicans, Indian Cowboy Picks, October 21th, 2026",
        2026,
    ) == date(2026, 10, 21)
    assert title_date("Sunday NBA Picks, Indian Cowboy Picks, November 8th", 2026) == date(2026, 11, 8)
    assert title_date("NBA Picks today", 2026) is None


def test_classify_keeps_public_nba_shows_naming_the_pickers() -> None:
    both = classify(
        CHANNEL,
        entry("Monday NBA Picks - 11/9/26 l Craig Trapp & Indian Cowboy NBA Picks"),
        2026,
    )
    assert both is not None
    assert both.pickers == ("Craig Trapp", "Indian Cowboy")
    assert both.program_name == "Doc's Sports Craig Trapp & Indian Cowboy NBA Picks"
    assert both.title_date == date(2026, 11, 9)
    assert len(both.reference_hash) == 64
    cowboy = classify(
        CHANNEL,
        entry("Sunday NBA Picks Pelicans vs. Clippers, Indian Cowboy Picks Predictions, November 8th"),
        2026,
    )
    assert cowboy is not None
    assert cowboy.program_name == "Doc's Sports Indian Cowboy NBA Picks"


def test_classify_skips_members_only_other_sports_and_preseason() -> None:
    skipped = [
        entry("Indian Cowboy Youtube Premium Monday NBA Picks, November 9th"),
        entry("Monday NBA Picks - 11/9/26 l Craig Trapp", availability="subscriber_only"),
        entry("Atlanta @ Golden State - WNBA Finals Picks - 10/31/26 l Craig Trapp & Indian Cowboy"),
        entry("Friday NBA Preseason Picks - 10/16/26 l Craig Trapp"),
        entry("Monday NCAA Basketball Picks - 11/9/26 l Craig Trapp & Indian Cowboy"),
        entry("Grizzlies vs Raptors Prediction | NBA Picks Monday 11/9/26 | Nick Menken"),
    ]
    assert all(classify(CHANNEL, item, 2026) is None for item in skipped)


def game(event_id: str, commence: str, away: str, home: str) -> dict:
    return {
        "event_id": event_id,
        "sport": "nba",
        "commence_time_utc": commence,
        "away_team": away,
        "home_team": home,
        "status": "scheduled",
    }


def test_candidate_schedule_drops_games_that_started_before_publishing() -> None:
    candidate = classify(CHANNEL, entry("Monday NBA Picks - 11/9/26 l Craig Trapp"), 2026)
    slates = {
        date(2026, 11, 9): [game("1", "2026-11-10T00:30:00Z", "Memphis Grizzlies", "Toronto Raptors")],
        date(2026, 11, 8): [game("0", "2026-11-08T20:00:00Z", "A", "B")],
        date(2026, 11, 10): [],
    }
    published = datetime(2026, 11, 9, 6, 25, tzinfo=UTC)
    schedule = candidate_schedule(candidate, published, lambda day: slates.get(day, []))
    assert [row["event_id"] for row in schedule] == ["1"]


def transcript() -> dict:
    words = [
        {"text": word, "source_start_seconds": 10.0 + index, "source_end_seconds": 10.5 + index}
        for index, word in enumerate("I like Memphis plus three and a half here.".split())
    ]
    return {
        "source_start_seconds": 0.0,
        "source_end_seconds": 120.0,
        "segment_sha256": "b" * 64,
        "utterances": [
            {
                "utterance_index": 0,
                "speaker_label": "A",
                "source_start_seconds": 10.0,
                "source_end_seconds": 30.0,
                "text": "I like Memphis plus three and a half here.",
                "words": words,
            }
        ],
    }


def test_quote_evidence_requires_exact_substring_and_finds_word_times() -> None:
    utterances = {0: transcript()["utterances"][0]}
    segment = quote_evidence(utterances, 0, "Memphis plus three")
    assert segment["source_start_seconds"] == 12.0
    assert segment["source_end_seconds"] == 14.5
    assert quote_evidence(utterances, 0, "Memphis +3.5") is None
    assert quote_evidence(utterances, 7, "Memphis") is None


def test_build_payload_validates_and_drops_unusable_picks(tmp_path) -> None:
    candidate = classify(
        CHANNEL, entry("Monday NBA Picks - 11/9/26 l Craig Trapp & Indian Cowboy"), 2026
    )
    schedule = [game("401", "2026-11-10T00:30:00Z", "Memphis Grizzlies", "Toronto Raptors")]
    pick = {
        "picker_name": "Craig Trapp",
        "event_id": "401",
        "market_family": "game_side",
        "contract_type": "spread",
        "period": "game",
        "direction": "Memphis Grizzlies",
        "line": 3.5,
        "opinion_status": "active",
        "conviction_score": 3,
        "explicit_markers": ["like", "love"],
        "quotes": [{"utterance_index": 0, "exact_text": "I like Memphis plus three and a half here."}],
    }
    extraction = {
        "speakers": {"A": "Craig Trapp"},
        "picks": [
            pick,
            {**pick, "picker_name": "Someone Else"},
            {**pick, "event_id": "999"},
            {**pick, "direction": "Boston Celtics"},
            {**pick, "quotes": [{"utterance_index": 0, "exact_text": "made up"}]},
            {**pick, "market_family": "player_prop"},
        ],
    }
    published = datetime(2026, 11, 9, 6, 25, tzinfo=UTC)
    payload, dropped = build_payload(candidate, published, transcript(), schedule, extraction)
    assert len(dropped) == 5
    observations = payload["observations"]
    assert len(observations) == 1
    assert observations[0]["selection"] == "Memphis Grizzlies +3.5"
    assert observations[0]["source_seconds"] == 10.0
    assert observations[0]["conviction"]["explicit_markers"] == ["like"]
    assert payload["content"]["valid_from_date"] == "2026-11-09"
    validate_payload(payload, candidate.url)


def test_build_payload_returns_none_without_picks() -> None:
    candidate = classify(CHANNEL, entry("Monday NBA Picks - 11/9/26 l Craig Trapp"), 2026)
    payload, dropped = build_payload(
        candidate, datetime(2026, 11, 9, tzinfo=UTC), transcript(), [], {"picks": []}
    )
    assert payload is None and dropped == []


def test_parse_json_object_accepts_fenced_json() -> None:
    assert parse_json_object('```json\n{"picks": []}\n```') == {"picks": []}


def test_audio_cut_short_while_youtube_processes_is_refused() -> None:
    import pytest

    from nfl_audio_parser.nba_channel import check_complete

    check_complete(501.4, 502)
    check_complete(300.0, None)  # no reported length: nothing to compare
    with pytest.raises(RuntimeError, match="177 s of the video's 502 s"):
        check_complete(176.7, 502)
