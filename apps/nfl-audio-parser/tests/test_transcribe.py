from __future__ import annotations

from pathlib import Path

import pytest

from nfl_audio_parser.transcribe import (
    _sports_context,
    build_parser,
    load_api_key,
    normalize_transcript,
    parse_timestamp,
    transcribe_youtube,
    validate_youtube_url,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("90", 90.0),
        ("10:47", 647.0),
        ("1:40:25", 6025.0),
        ("01:02:03.5", 3723.5),
    ],
)
def test_parse_timestamp(value: str, expected: float) -> None:
    assert parse_timestamp(value) == expected


@pytest.mark.parametrize("value", ["", "-1", "1:60", "1:2:60", "1:2:3:4"])
def test_parse_timestamp_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_timestamp(value)


def test_load_api_key_requires_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    config = tmp_path / "env"
    config.write_text("ASSEMBLYAI_API_KEY=secret\n", encoding="utf-8")
    config.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_api_key(config)
    config.chmod(0o600)
    assert load_api_key(config) == "secret"


def test_normalize_transcript_adds_source_offsets() -> None:
    normalized = normalize_transcript(
        {
            "id": "transcript-1",
            "audio_duration": 10,
            "language_code": "en",
            "utterances": [
                {
                    "speaker": "A",
                    "start": 1000,
                    "end": 2500,
                    "confidence": 0.9,
                    "text": "Packers minus three.",
                    "words": [
                        {
                            "text": "Packers",
                            "start": 1000,
                            "end": 1400,
                            "confidence": 0.95,
                        }
                    ],
                }
            ],
        },
        source_start_seconds=647,
        source_end_seconds=657,
        segment_sha256="a" * 64,
    )
    utterance = normalized["utterances"][0]
    assert utterance["source_start_seconds"] == 648
    assert utterance["source_end_seconds"] == 649.5
    assert utterance["words"][0]["source_start_seconds"] == 648


def test_normalize_transcript_expands_utterance_to_contain_words() -> None:
    normalized = normalize_transcript(
        {
            "id": "transcript-1",
            "utterances": [
                {
                    "speaker": "A",
                    "start": 1100,
                    "end": 2400,
                    "text": "Provider boundary mismatch.",
                    "words": [
                        {
                            "text": "Provider",
                            "start": 1000,
                            "end": 1400,
                        },
                        {
                            "text": "mismatch.",
                            "start": 1400,
                            "end": 2500,
                        },
                    ],
                }
            ],
        },
        source_start_seconds=647,
        source_end_seconds=657,
        segment_sha256="a" * 64,
    )

    utterance = normalized["utterances"][0]
    assert utterance["segment_start_seconds"] == 1
    assert utterance["segment_end_seconds"] == 2.5
    assert utterance["source_start_seconds"] == 648
    assert utterance["source_end_seconds"] == 649.5


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=uZIO0qZslXE",
        "https://youtu.be/uZIO0qZslXE",
        "https://music.youtube.com/watch?v=uZIO0qZslXE",
    ],
)
def test_validate_youtube_url_accepts_video_hosts(url: str) -> None:
    assert validate_youtube_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/video.mp4",
        "https://example.com/watch?v=uZIO0qZslXE",
        "https://youtube.com",
    ],
)
def test_validate_youtube_url_rejects_other_sources(url: str) -> None:
    with pytest.raises(ValueError):
        validate_youtube_url(url)


def test_parser_requires_exactly_one_audio_source() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--audio",
                "episode.mp3",
                "--youtube-url",
                "https://youtu.be/uZIO0qZslXE",
                "--start",
                "1:04",
                "--end",
                "9:54",
                "--output",
                "transcript.json",
            ]
        )


def test_sports_context_requires_complete_date_window() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--youtube-url",
            "https://youtu.be/uZIO0qZslXE",
            "--start",
            "1:04",
            "--end",
            "9:54",
            "--output",
            "transcript.json",
            "--sport",
            "nba",
            "--games-from",
            "2026-10-20",
            "--games-through",
            "2026-10-21",
        ]
    )
    assert _sports_context(args) == {
        "sport": "nba",
        "valid_from_date": "2026-10-20",
        "valid_through_date": "2026-10-21",
        "valid_timezone": "America/New_York",
    }

    incomplete = parser.parse_args(
        [
            "--youtube-url",
            "https://youtu.be/uZIO0qZslXE",
            "--start",
            "1:04",
            "--end",
            "9:54",
            "--output",
            "transcript.json",
            "--sport",
            "nfl",
        ]
    )
    with pytest.raises(ValueError, match="provided together"):
        _sports_context(incomplete)


def test_transcribe_youtube_removes_downloaded_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Path] = {}

    def fake_download(url: str, directory: Path) -> Path:
        assert url == "https://youtu.be/uZIO0qZslXE"
        source = directory / "source.webm"
        source.write_bytes(b"temporary audio")
        return source

    def fake_transcribe(
        source: Path,
        *,
        start_timestamp: str,
        end_timestamp: str,
        config_path: Path,
        poll_seconds: float,
    ) -> dict[str, object]:
        assert source.is_file()
        assert start_timestamp == "1:04"
        assert end_timestamp == "9:54"
        observed["source"] = source
        return {"provider": "assemblyai"}

    monkeypatch.setattr(
        "nfl_audio_parser.transcribe.download_youtube_audio",
        fake_download,
    )
    monkeypatch.setattr(
        "nfl_audio_parser.transcribe.transcribe",
        fake_transcribe,
    )

    result = transcribe_youtube(
        "https://youtu.be/uZIO0qZslXE",
        start_timestamp="1:04",
        end_timestamp="9:54",
    )

    assert result == {"provider": "assemblyai"}
    assert not observed["source"].exists()
