from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nfl_audio_parser.opinions import OpinionStore, source_reference


def opinion_payload(
    *,
    sport: str = "nfl",
    published_at_utc: str = "2026-10-15T16:00:00Z",
    valid_from_date: str = "2026-10-18",
    valid_through_date: str = "2026-10-19",
    event_id: str = "event-1",
    commence_time_utc: str = "2026-10-19T00:20:00Z",
    away_team: str = "Kansas City Chiefs",
    home_team: str = "Las Vegas Raiders",
    observations: list[dict] | None = None,
) -> dict:
    if observations is None:
        observations = [
            {
                "observation_index": 1,
                "picker_name": "Simon Hunter",
                "event_id": event_id,
                "market_family": "game_side",
                "contract_type": "spread",
                "period": "game",
                "source_seconds": 900.0,
                "selection": f"{home_team} +4.5",
                "direction": home_team,
                "line": 4.5,
                "opinion_status": "active",
                "opinion_stage": "early_look",
                "conviction": {
                    "score": 4,
                    "label": "strong",
                    "explicit_markers": ["love"],
                    "hedged": False,
                    "conditional": False,
                    "condition_text": None,
                },
                "evidence_segments": [
                    {"exact_text": f"I love {home_team} plus four and a half."}
                ],
                "extraction_confidence": 0.97,
                "review_flags": [],
            }
        ]
    return {
        "schema_version": "sports_opinions_v1",
        "content": {
            "source_provider": "youtube",
            "program_name": "Sharp or Square",
            "published_at_utc": published_at_utc,
            "sport": sport,
            "valid_from_date": valid_from_date,
            "valid_through_date": valid_through_date,
            "valid_timezone": "America/New_York",
            "window_label": f"{sport.upper()} slate",
            "source_start_seconds": 797.0,
            "source_end_seconds": 3086.0,
            "segment_sha256": "a" * 64,
        },
        "events": [
            {
                "event_id": event_id,
                "sport": sport,
                "commence_time_utc": commence_time_utc,
                "away_team": away_team,
                "home_team": home_team,
                "status": "scheduled",
            }
        ],
        "observations": observations,
    }


def test_source_reference_canonicalizes_youtube_and_supports_spotify() -> None:
    watch = source_reference("https://www.youtube.com/watch?v=abc123")
    short = source_reference("https://youtu.be/abc123?t=20")
    assert watch == short
    provider, reference_hash = source_reference(
        "https://open.spotify.com/episode/episode123"
    )
    assert provider == "spotify"
    assert len(reference_hash) == 64


def test_ingest_queries_url_without_storing_it(tmp_path: Path) -> None:
    path = tmp_path / "opinions.sqlite3"
    store = OpinionStore(path)
    source_url = "https://www.youtube.com/watch?v=video-one"
    result = store.ingest(opinion_payload(), source_url=source_url)
    repeated = store.ingest(opinion_payload(), source_url=source_url)

    assert result["created_revision"] is True
    assert repeated["created_revision"] is False
    source = store.source("https://youtu.be/video-one")
    assert source["content"]["program_name"] == "Sharp or Square"
    assert source["games"] == [
        {
            "event_id": "event-1",
            "sport": "nfl",
            "away_team": "Kansas City Chiefs",
            "home_team": "Las Vegas Raiders",
            "commence_time_utc": "2026-10-19T00:20:00Z",
            "opinion_count": 1,
            "pickers": ["Simon Hunter"],
        }
    ]
    assert source["observations"][0]["picker_name"] == "Simon Hunter"
    assert source["observations"][0]["side_role"] == "underdog"
    assert source["observations"][0]["picker_window_strength_rank"] == 1

    with sqlite3.connect(path) as connection:
        values = "\n".join(
            str(value)
            for row in connection.execute(
                """
                SELECT source_provider, source_reference_hash
                FROM content_items
                """
            )
            for value in row
        )
    assert "youtube.com" not in values
    assert "video-one" not in values


def test_timeline_change_stats_and_consensus(tmp_path: Path) -> None:
    path = tmp_path / "opinions.sqlite3"
    store = OpinionStore(path)
    first = opinion_payload()
    store.ingest(
        first,
        source_url="https://www.youtube.com/watch?v=early-video",
    )
    second = opinion_payload(
        published_at_utc="2026-10-17T16:00:00Z",
        observations=[
            {
                "observation_index": 1,
                "picker_name": "Simon Hunter",
                "event_id": "event-1",
                "market_family": "game_side",
                "contract_type": "spread",
                "period": "game",
                "source_seconds": 850.0,
                "selection": "Kansas City Chiefs -4.5",
                "direction": "Kansas City Chiefs",
                "line": -4.5,
                "opinion_status": "active",
                "opinion_stage": "final",
                "conviction": {
                    "score": 2,
                    "label": "lean",
                    "explicit_markers": ["lean"],
                    "hedged": True,
                    "conditional": False,
                    "condition_text": None,
                },
                "evidence_segments": [
                    {"exact_text": "My final lean is Kansas City minus four and a half."}
                ],
                "extraction_confidence": 0.95,
                "review_flags": [],
            },
            {
                "observation_index": 2,
                "picker_name": "Chad Millman",
                "event_id": "event-1",
                "market_family": "game_side",
                "contract_type": "spread",
                "period": "game",
                "source_seconds": 900.0,
                "selection": "Las Vegas Raiders +4.5",
                "direction": "Las Vegas Raiders",
                "line": 4.5,
                "opinion_status": "active",
                "opinion_stage": "final",
                "conviction": {
                    "score": 4,
                    "label": "strong",
                    "explicit_markers": ["betting"],
                    "hedged": False,
                    "conditional": False,
                    "condition_text": None,
                },
                "evidence_segments": [
                    {"exact_text": "I am betting Las Vegas plus four and a half."}
                ],
                "extraction_confidence": 0.98,
                "review_flags": [],
            },
        ],
    )
    store.ingest(
        second,
        source_url="https://www.youtube.com/watch?v=final-video",
    )

    timeline = store.timeline(picker_name="Simon Hunter", event_id="event-1")
    assert [row["change_type"] for row in timeline] == [
        "initial",
        "switched_direction",
    ]

    consensus = store.consensus(event_id="event-1")
    assert {row["direction"]: row["picker_count"] for row in consensus} == {
        "Kansas City Chiefs": 1,
        "Las Vegas Raiders": 1,
    }

    grade = store.record_result(
        event_id="event-1",
        away_score=27,
        home_score=20,
    )
    assert grade["right"] == 1
    assert grade["wrong"] == 2

    latest = store.stats(
        group_by=["picker", "conviction_tier", "side_role"],
        policy="latest_pregame",
    )
    simon = next(row for row in latest if row["picker"] == "Simon Hunter")
    assert simon["right"] == 1
    assert simon["win_rate"] == 1.0

    all_observations = store.stats(
        group_by=["picker"],
        policy="all_observations",
    )
    simon_all = next(
        row for row in all_observations if row["picker"] == "Simon Hunter"
    )
    assert simon_all["right"] == 1
    assert simon_all["wrong"] == 1

    agreement = store.stats(group_by=["agreement_bucket"])
    assert agreement == [
        {
            "agreement_bucket": "split",
            "total": 2,
            "right": 1,
            "wrong": 1,
            "push": 0,
            "ungradeable": 0,
            "win_rate": 0.5,
        }
    ]


def test_nba_payload_and_total_grading(tmp_path: Path) -> None:
    store = OpinionStore(tmp_path / "opinions.sqlite3")
    payload = opinion_payload(
        sport="nba",
        published_at_utc="2026-10-20T14:00:00Z",
        valid_from_date="2026-10-21",
        valid_through_date="2026-10-21",
        event_id="nba-1",
        commence_time_utc="2026-10-22T00:00:00Z",
        away_team="Boston Celtics",
        home_team="New York Knicks",
        observations=[
            {
                "observation_index": 1,
                "picker_name": "NBA Picker",
                "event_id": "nba-1",
                "market_family": "game_total",
                "contract_type": "total",
                "period": "game",
                "source_seconds": 1000.0,
                "selection": "Celtics-Knicks Over 220.5",
                "direction": "Over",
                "line": 220.5,
                "opinion_status": "active",
                "opinion_stage": "final",
                "conviction": {
                    "score": 5,
                    "label": "best_bet",
                    "explicit_markers": ["best bet"],
                    "hedged": False,
                    "conditional": False,
                    "condition_text": None,
                },
                "evidence_segments": [
                    {"exact_text": "The over is my best bet tonight."}
                ],
                "extraction_confidence": 0.99,
                "review_flags": [],
            }
        ],
    )
    store.ingest(
        payload,
        source_url="https://www.youtube.com/watch?v=nba-video",
    )
    result = store.record_result(
        event_id="nba-1",
        away_score=115,
        home_score=110,
    )
    assert result["right"] == 1
    stats = store.stats(group_by=["sport", "conviction_tier"])
    assert stats == [
        {
            "sport": "nba",
            "conviction_tier": "strong",
            "total": 1,
            "right": 1,
            "wrong": 0,
            "push": 0,
            "ungradeable": 0,
            "win_rate": 1.0,
        }
    ]


def test_event_must_fall_inside_declared_date_window(tmp_path: Path) -> None:
    store = OpinionStore(tmp_path / "opinions.sqlite3")
    payload = opinion_payload(
        valid_from_date="2026-10-10",
        valid_through_date="2026-10-10",
    )
    with pytest.raises(ValueError, match="outside the prediction window"):
        store.ingest(
            payload,
            source_url="https://www.youtube.com/watch?v=bad-window",
        )


def test_payload_cannot_persist_raw_source_metadata(tmp_path: Path) -> None:
    store = OpinionStore(tmp_path / "opinions.sqlite3")
    payload = opinion_payload()
    payload["content"]["source_url"] = "https://youtu.be/secret-source"
    with pytest.raises(ValueError, match="prohibited"):
        store.ingest(
            payload,
            source_url="https://www.youtube.com/watch?v=secret-source",
        )
