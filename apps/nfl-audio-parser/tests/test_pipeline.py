from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from nfl_audio_parser.channel_registry import (
    ChannelConfig,
    classify_candidate,
    load_registry,
)
from nfl_audio_parser.pipeline import (
    ProductionIngester,
    parse_weeks,
    validate_job,
)
from nfl_audio_parser.pipeline_store import (
    DiscoveredVideo,
    PipelineStore,
)
from nfl_audio_parser_test_fixtures import sample_payload


def sharp_channel() -> ChannelConfig:
    return next(
        channel
        for channel in load_registry()
        if channel.key == "sharp-or-square"
    )


def test_registry_records_expected_speakers_and_classifies_nfl_videos() -> None:
    channel = sharp_channel()
    assert channel.expected_speakers == ("Chad Millman", "Simon Hunter")

    candidate = classify_candidate(
        channel,
        title="Sharp NFL Week 6 Bets",
        description="Our picks for every game.",
    )
    assert candidate.candidate is True
    assert candidate.season == 2026
    assert candidate.week == 6

    reaction = classify_candidate(
        channel,
        title="NFL Week 6 Reactions",
        description="Postgame recap.",
    )
    assert reaction.candidate is False
    assert reaction.reason == "matched_exclude_pattern"

    hotline_hash = (
        "19b39f35c3815c23d65e1631e463167b4c299cfd7b7d0761d2d5148630b3e1e0"
    )
    hotline = next(
        seed
        for seed in channel.seeds
        if seed.source_reference_hash == hotline_hash
    )
    assert hotline.status == "duplicate"
    assert hotline.duplicate_of_hash == (
        "971a1d5bae2b656a0b78684e4abbbac4aee8dc86d792e5e16fa33ace2035493f"
    )


def test_pipeline_store_tracks_opaque_lifecycle_without_source_metadata(
    tmp_path: Path,
) -> None:
    store = PipelineStore(tmp_path / "pipeline.sqlite3")
    channel = sharp_channel()
    store.sync_channel(channel)
    hotline = store.video(
        "19b39f35c3815c23d65e1631e463167b4c299cfd7b7d0761d2d5148630b3e1e0"
    )
    assert hotline is not None
    assert hotline["status"] == "duplicate"
    assert hotline["duplicate_of_hash"] == (
        "971a1d5bae2b656a0b78684e4abbbac4aee8dc86d792e5e16fa33ace2035493f"
    )
    reference_hash = "f" * 64
    assert (
        store.record_discovery(
            DiscoveredVideo(
                source_reference_hash=reference_hash,
                channel_key=channel.key,
                season=2026,
                week=6,
                published_at_utc="2026-10-15T12:00:00+00:00",
                duration_seconds=1200.0,
                metadata_sha256="e" * 64,
                candidate=True,
                reason="matched_nfl_pick_video",
            )
        )
        == "queued"
    )
    claimed = store.claim_next(
        channel_key=channel.key,
        season=2026,
        weeks=(6,),
    )
    assert claimed is not None
    assert claimed["status"] == "processing"
    store.mark_parsed(
        source_reference_hash=reference_hash,
        lease_token=claimed["lease_token"],
        provider_transcript_id="provider-1",
        segment_sha256="d" * 64,
        review_receipt={"run_id": "run-1", "revision_id": "revision-1"},
        opinion_receipt={"content_id": "content-1"},
    )
    assert store.video(reference_hash)["status"] == "parsed"

    with sqlite3.connect(store.path) as connection:
        values = "\n".join(
            str(value)
            for row in connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE sql IS NOT NULL
                UNION ALL
                SELECT CAST(source_reference_hash AS TEXT)
                FROM pipeline_videos
                """
            )
            for value in row
        ).casefold()
    assert "youtube.com" not in values
    assert "video_title" not in values
    assert "source_url" not in values


def test_pipeline_reclassifies_automatic_skip_when_rules_change(
    tmp_path: Path,
) -> None:
    store = PipelineStore(tmp_path / "pipeline.sqlite3")
    channel = sharp_channel()
    store.sync_channel(channel)
    reference_hash = "a" * 64
    skipped = DiscoveredVideo(
        source_reference_hash=reference_hash,
        channel_key=channel.key,
        season=2026,
        week=None,
        published_at_utc=None,
        duration_seconds=None,
        metadata_sha256="b" * 64,
        candidate=False,
        reason="week_unresolved",
    )
    assert store.record_discovery(skipped) == "skipped"
    queued = DiscoveredVideo(
        source_reference_hash=reference_hash,
        channel_key=channel.key,
        season=2026,
        week=6,
        published_at_utc="2026-10-15T12:00:00+00:00",
        duration_seconds=600.0,
        metadata_sha256="c" * 64,
        candidate=True,
        reason="matched_nfl_pick_video",
    )
    assert store.record_discovery(queued) == "queued"
    assert store.video(reference_hash)["status"] == "queued"


def test_transcript_fingerprint_detects_derivative_subclip(
    tmp_path: Path,
) -> None:
    store = PipelineStore(tmp_path / "pipeline.sqlite3")
    channel = sharp_channel()
    store.sync_channel(channel)
    parent_hash = "1" * 64
    child_hash = "2" * 64
    store.seed_video(
        channel_key=channel.key,
        source_reference_hash=parent_hash,
        season=2026,
        week=5,
        status="parsed",
        reason="test parent",
    )
    words = [f"token{index}" for index in range(120)]
    parent = {"utterances": [{"text": " ".join(words)}]}
    child = {"utterances": [{"text": " ".join(words[30:75])}]}
    store.save_fingerprint(
        source_reference_hash=parent_hash,
        transcript=parent,
    )

    duplicate = store.find_duplicate(
        channel_key=channel.key,
        source_reference_hash=child_hash,
        transcript=child,
    )
    assert duplicate is not None
    assert duplicate["source_reference_hash"] == parent_hash
    assert duplicate["containment"] == 1.0


def test_duplicate_terminal_state_retains_cleanup_warning(tmp_path: Path) -> None:
    store = PipelineStore(tmp_path / "pipeline.sqlite3")
    channel = sharp_channel()
    store.sync_channel(channel)
    reference_hash = "3" * 64
    store.record_discovery(
        DiscoveredVideo(
            source_reference_hash=reference_hash,
            channel_key=channel.key,
            season=2026,
            week=6,
            published_at_utc=None,
            duration_seconds=300.0,
            metadata_sha256="4" * 64,
            candidate=True,
            reason="matched_nfl_pick_video",
        )
    )
    claimed = store.claim_next(
        channel_key=channel.key,
        season=2026,
        weeks=(6,),
    )
    assert claimed is not None
    store.mark_duplicate(
        source_reference_hash=reference_hash,
        lease_token=claimed["lease_token"],
        duplicate_of_hash=channel.seeds[0].source_reference_hash,
        reason="transcript_containment=1.0000",
        provider_transcript_id="provider-duplicate",
        segment_sha256="5" * 64,
        cleanup_warning="provider cleanup unavailable",
    )

    video = store.video(reference_hash)
    assert video is not None
    assert video["status"] == "duplicate"
    assert video["last_error"] == "provider cleanup unavailable"


def test_parse_weeks_supports_ranges() -> None:
    assert parse_weeks("1-3,5,6") == (1, 2, 3, 5, 6)


def test_validate_job_requires_relative_confidence_and_expected_speakers(
    tmp_path: Path,
) -> None:
    ingest = sample_payload()
    first_utterance = ingest["transcript"]["utterances"][0]
    first_evidence = ingest["review"]["speakers"][0]["evidence"][0]
    ingest["review"]["speakers"][0]["name"] = "Chad Millman"
    first_pick = ingest["review"]["picks"][0]
    first_pick["speaker_name"] = "Chad Millman"
    first_pick["relative_confidence"] = {
        "within_speaker_video": {"tier": "top", "rank": 1},
        "versus_cohost": {"relation": "aligned_similar"},
    }

    simon_text = "I also love Green Bay laying three and a half."
    simon_evidence = {
        "utterance_index": 1,
        "speaker_label": "B",
        "source_start_seconds": 656.0,
        "source_end_seconds": 660.0,
        "exact_text": simon_text,
    }
    ingest["transcript"]["utterances"].append(
        {
            "utterance_index": 1,
            "speaker_label": "B",
            "source_start_seconds": 656.0,
            "source_end_seconds": 660.0,
            "confidence": 0.96,
            "text": simon_text,
            "words": [],
        }
    )
    ingest["review"]["speakers"].append(
        {
            "speaker_label": "B",
            "name": "Simon Hunter",
            "confidence": 0.95,
            "evidence": [simon_evidence],
        }
    )
    second_pick = json.loads(json.dumps(first_pick))
    second_pick.update(
        {
            "pick_index": 2,
            "speaker_label": "B",
            "speaker_name": "Simon Hunter",
            "evidence_segments": [simon_evidence],
            "rationale_segments": [],
        }
    )
    second_pick["conviction"]["explicit_markers"] = ["love"]
    ingest["review"]["picks"].append(second_pick)
    ingest["review"]["review_summary"]["active_pick_count"] = 2

    opinions = {
        "schema_version": "sports_opinions_v1",
        "content": {
            "source_provider": "youtube",
            "program_name": "Sharp or Square",
            "published_at_utc": "2026-09-20T16:00:00Z",
            "sport": "nfl",
            "valid_from_date": "2026-09-24",
            "valid_through_date": "2026-09-24",
            "valid_timezone": "America/New_York",
            "source_start_seconds": 647.0,
            "source_end_seconds": 6025.0,
            "segment_sha256": "a" * 64,
        },
        "events": [
            {
                "event_id": "game-1",
                "sport": "nfl",
                "commence_time_utc": "2026-09-25T00:15:00Z",
                "away_team": "Green Bay Packers",
                "home_team": "Atlanta Falcons",
                "status": "scheduled",
            }
        ],
        "observations": [],
    }
    for index, (name, exact_text) in enumerate(
        (
            ("Chad Millman", first_utterance["text"]),
            ("Simon Hunter", simon_text),
        ),
        start=1,
    ):
        opinions["observations"].append(
            {
                "observation_index": index,
                "picker_name": name,
                "event_id": "game-1",
                "market_family": "game_side",
                "contract_type": "spread",
                "period": "game",
                "source_seconds": 650.0 + index,
                "selection": "Green Bay Packers -3.5",
                "direction": "Green Bay Packers",
                "line": -3.5,
                "opinion_status": "active",
                "opinion_stage": "analysis",
                "conviction": {
                    "score": 4,
                    "label": "strong",
                    "explicit_markers": ["love"],
                    "hedged": False,
                    "conditional": False,
                    "condition_text": None,
                },
                "evidence_segments": [
                    {
                        "utterance_index": index - 1,
                        "exact_text": exact_text,
                    }
                ],
                "extraction_confidence": 0.95,
                "review_flags": [],
            }
        )

    transcript_path = tmp_path / "transcript.json"
    schedule_path = tmp_path / "schedule.json"
    ingest_path = tmp_path / "ingest.json"
    opinions_path = tmp_path / "opinions.json"
    markdown_path = tmp_path / "review.md"
    transcript_path.write_text(json.dumps(ingest["transcript"]))
    schedule_path.write_text(json.dumps(ingest["schedule"]))
    ingest_path.write_text(json.dumps(ingest))
    opinions_path.write_text(json.dumps(opinions))
    markdown_path.write_text("# Review\n")
    job_path = tmp_path / "job.json"
    job_path.write_text(
        json.dumps(
            {
                "source_url": "https://www.youtube.com/watch?v=test-job",
                "expected_speakers": ["Chad Millman", "Simon Hunter"],
                "transcript_path": str(transcript_path),
                "schedule_path": str(schedule_path),
                "ingest_output": str(ingest_path),
                "opinions_output": str(opinions_path),
                "markdown_output": str(markdown_path),
            }
        )
    )

    result = validate_job(job_path)
    assert result["pick_count"] == 2
    assert result["opinion_count"] == 2
    assert result["speaker_validation"]["unexpected_speakers"] == []


def test_production_ingester_links_opinions_to_review_revision() -> None:
    calls = []

    class RecordingIngester(ProductionIngester):
        def _run(self, arguments, payload):
            calls.append((arguments, payload))
            if arguments == ["ingest"]:
                return {"run_id": "run-1", "revision_id": "revision-1"}
            return {"content_id": "content-1"}

    opinions = {"content": {}, "observations": []}
    RecordingIngester().ingest(
        source_url="https://www.youtube.com/watch?v=test",
        ingest_payload={"schema_version": "nfl_audio_parser_ingest_v1"},
        opinions_payload=opinions,
    )

    assert opinions == {"content": {}, "observations": []}
    assert calls[1][1]["content"]["extraction_revision_id"] == "revision-1"
