from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nfl_audio_parser_test_fixtures import sample_payload
from nfl_audio_parser.store import AudioParserStore


def payload() -> dict:
    return sample_payload()


def test_ingest_is_idempotent_and_queryable(tmp_path: Path) -> None:
    store = AudioParserStore(tmp_path / "audio.sqlite3")
    first = store.ingest(payload())
    second = store.ingest(payload())

    assert first["created_run"] is True
    assert first["created_revision"] is True
    assert second == {
        "run_id": first["run_id"],
        "revision_id": first["revision_id"],
        "created_run": False,
        "created_revision": False,
    }
    stored = store.get_run(first["run_id"])
    assert stored["utterances"][0]["exact_text"].startswith("I absolutely")
    assert stored["revision"]["review"]["picks"][0]["event_id"] == "game-1"
    assert store.list_runs()[0]["revision_count"] == 1


def test_ingest_rejects_source_metadata_and_nonverbatim_quote(
    tmp_path: Path,
) -> None:
    store = AudioParserStore(tmp_path / "audio.sqlite3")
    with_source = payload()
    with_source["review"]["request"]["audio_path"] = "/Users/example/source.mp3"
    with pytest.raises(ValueError, match="prohibited"):
        store.ingest(with_source)

    with_url = payload()
    with_url["review"]["request"]["youtube_url"] = "https://youtu.be/example"
    with pytest.raises(ValueError, match="prohibited"):
        store.ingest(with_url)

    bad_quote = payload()
    bad_quote["review"]["picks"][0]["evidence_segments"][0][
        "exact_text"
    ] = "A paraphrase that is not present."
    with pytest.raises(ValueError, match="exact transcript substring"):
        store.ingest(bad_quote)


def test_ingest_rejects_event_outside_schedule(tmp_path: Path) -> None:
    store = AudioParserStore(tmp_path / "audio.sqlite3")
    invalid = payload()
    invalid["review"]["picks"][0]["event_id"] = "unknown-game"
    with pytest.raises(ValueError, match="absent from schedule"):
        store.ingest(invalid)


def test_ingest_accepts_provider_word_with_zero_duration(tmp_path: Path) -> None:
    store = AudioParserStore(tmp_path / "audio.sqlite3")
    provider_payload = payload()
    utterance = provider_payload["transcript"]["utterances"][0]
    utterance["words"] = [
        {
            "text": "Packers",
            "source_start_seconds": utterance["source_start_seconds"],
            "source_end_seconds": utterance["source_start_seconds"],
            "confidence": 0.95,
        }
    ]

    store.ingest(provider_payload)


def test_ingest_preserves_parlay_and_teaser_grouping(tmp_path: Path) -> None:
    store = AudioParserStore(tmp_path / "audio.sqlite3")
    grouped = payload()
    first = grouped["review"]["picks"][0]
    first.update(
        {
            "bet_structure": "parlay",
            "group_id": "parlay-1",
            "leg_index": 1,
        }
    )
    second = json.loads(json.dumps(first))
    second.update(
        {
            "pick_index": 2,
            "bet_structure": "teaser",
            "group_id": "teaser-1",
            "leg_index": 1,
            "teaser_points": 6.0,
        }
    )
    grouped["review"]["picks"].append(second)

    result = store.ingest(grouped)
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute(
            """
            SELECT bet_structure, group_id, leg_index, teaser_points
            FROM parsed_picks
            WHERE revision_id=?
            ORDER BY pick_index
            """,
            (result["revision_id"],),
        ).fetchall()
    assert rows == [
        ("parlay", "parlay-1", 1, None),
        ("teaser", "teaser-1", 1, 6.0),
    ]


def test_ingest_rejects_inconsistent_group_fields(tmp_path: Path) -> None:
    store = AudioParserStore(tmp_path / "audio.sqlite3")
    invalid = payload()
    invalid["review"]["picks"][0]["group_id"] = "not-straight"
    with pytest.raises(ValueError, match="grouped-leg"):
        store.ingest(invalid)


def test_review_decisions_are_append_only(tmp_path: Path) -> None:
    path = tmp_path / "audio.sqlite3"
    store = AudioParserStore(path)
    result = store.ingest(payload())
    decision_id = store.add_review_decision(
        result["revision_id"],
        reviewer="operator",
        decision="approved",
        notes="Checked against audio.",
    )

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT decision_id, decision, notes FROM review_decisions
            """
        ).fetchone()
    assert row == (decision_id, "approved", "Checked against audio.")


def test_reingest_changed_transcript_for_same_segment_is_rejected(
    tmp_path: Path,
) -> None:
    store = AudioParserStore(tmp_path / "audio.sqlite3")
    store.ingest(payload())
    changed = payload()
    changed["transcript"]["utterances"][0]["text"] += " Changed."
    with pytest.raises(ValueError, match="different transcript"):
        store.ingest(changed)


def test_database_contains_no_source_or_audio_columns(tmp_path: Path) -> None:
    path = tmp_path / "audio.sqlite3"
    store = AudioParserStore(path)
    store.ingest(payload())
    with sqlite3.connect(path) as connection:
        schemas = "\n".join(
            row[0]
            for row in connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type='table' AND sql IS NOT NULL
                """
            )
        ).casefold()
    assert "audio_path" not in schemas
    assert "source_filename" not in schemas
    assert "audio_bytes" not in schemas
