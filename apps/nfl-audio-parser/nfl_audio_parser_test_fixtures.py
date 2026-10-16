from __future__ import annotations


def sample_payload() -> dict:
    exact_text = "I absolutely love Green Bay laying three and a half here."
    evidence = {
        "utterance_index": 0,
        "speaker_label": "A",
        "source_start_seconds": 650.0,
        "source_end_seconds": 655.0,
        "exact_text": exact_text,
    }
    return {
        "schema_version": "nfl_audio_parser_ingest_v1",
        "transcript": {
            "provider": "assemblyai",
            "provider_transcript_id": "transcript-1",
            "utterances": [
                {
                    "utterance_index": 0,
                    "speaker_label": "A",
                    "source_start_seconds": 650.0,
                    "source_end_seconds": 655.0,
                    "confidence": 0.97,
                    "text": exact_text,
                    "words": [],
                }
            ],
        },
        "schedule": {
            "season": 2026,
            "requested_week": 3,
            "games": [
                {
                    "event_id": "game-1",
                    "season": 2026,
                    "week": 3,
                    "away_team": "Green Bay Packers",
                    "home_team": "Atlanta Falcons",
                }
            ],
        },
        "review": {
            "schema_version": "nfl_audio_picks_v1",
            "request": {
                "start_timestamp": "10:47",
                "end_timestamp": "1:40:25",
                "start_seconds": 647.0,
                "end_seconds": 6025.0,
                "season": 2026,
                "week": 3,
                "segment_sha256": "a" * 64,
            },
            "speakers": [
                {
                    "speaker_label": "A",
                    "name": "Host",
                    "confidence": 0.9,
                    "evidence": [evidence],
                }
            ],
            "picks": [
                {
                    "pick_index": 1,
                    "speaker_label": "A",
                    "speaker_name": "Host",
                    "event_id": "game-1",
                    "away_team": "Green Bay Packers",
                    "home_team": "Atlanta Falcons",
                    "bet_structure": "straight",
                    "group_id": None,
                    "leg_index": None,
                    "teaser_points": None,
                    "bet_type": "spread",
                    "period": "game",
                    "selection": "Green Bay Packers -3.5",
                    "direction": "Green Bay Packers",
                    "line": -3.5,
                    "price": -110,
                    "player": None,
                    "prop_stat": None,
                    "final_status": "active",
                    "conviction": {
                        "score": 4,
                        "label": "strong",
                        "explicit_markers": ["absolutely love"],
                        "hedged": False,
                        "conditional": False,
                        "condition_text": None,
                    },
                    "evidence_segments": [evidence],
                    "rationale_segments": [],
                    "transcription_confidence": 0.97,
                    "extraction_confidence": 0.95,
                    "review_flags": [],
                }
            ],
            "non_pick_mentions": [],
            "review_summary": {
                "active_pick_count": 1,
                "retracted_pick_count": 0,
                "unresolved_speaker_count": 0,
                "unresolved_event_count": 0,
                "warnings": [],
            },
        },
    }


def sample_opinion_payload() -> dict:
    exact_text = "I love Las Vegas plus four and a half."
    return {
        "schema_version": "sports_opinions_v1",
        "content": {
            "source_provider": "youtube",
            "program_name": "Sharp or Square",
            "published_at_utc": "2026-10-15T16:00:00Z",
            "sport": "nfl",
            "valid_from_date": "2026-10-18",
            "valid_through_date": "2026-10-19",
            "valid_timezone": "America/New_York",
            "source_start_seconds": 797.0,
            "source_end_seconds": 3086.0,
            "segment_sha256": "b" * 64,
        },
        "events": [
            {
                "event_id": "event-1",
                "sport": "nfl",
                "commence_time_utc": "2026-10-19T20:25:00Z",
                "away_team": "Kansas City Chiefs",
                "home_team": "Las Vegas Raiders",
                "status": "scheduled",
            }
        ],
        "observations": [
            {
                "observation_index": 1,
                "picker_name": "Simon Hunter",
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
                    "explicit_markers": ["love"],
                    "hedged": False,
                    "conditional": False,
                    "condition_text": None,
                },
                "evidence_segments": [{"exact_text": exact_text}],
                "extraction_confidence": 0.97,
                "review_flags": [],
            }
        ],
    }
