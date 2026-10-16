from __future__ import annotations

import json
from pathlib import Path

from nfl_audio_parser.cli import main
from nfl_audio_parser_test_fixtures import (
    sample_opinion_payload,
    sample_payload,
)


def test_cli_init_ingest_list_and_show(
    tmp_path: Path, capsys
) -> None:
    database = tmp_path / "audio.sqlite3"
    input_path = tmp_path / "payload.json"
    input_path.write_text(json.dumps(sample_payload()), encoding="utf-8")

    assert main(["--database", str(database), "init"]) == 0
    assert main(
        [
            "--database",
            str(database),
            "ingest",
            "--input",
            str(input_path),
        ]
    ) == 0
    ingest_result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert ingest_result["ok"] is True

    assert main(["--database", str(database), "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["runs"][0]["run_id"] == ingest_result["run_id"]

    assert main(
        [
            "--database",
            str(database),
            "show",
            "--run-id",
            ingest_result["run_id"],
        ]
    ) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["revision"]["review"]["picks"][0]["selection"].endswith("-3.5")


def test_cli_reports_validation_error(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "invalid.json"
    input_path.write_text("{}", encoding="utf-8")
    assert main(
        [
            "--database",
            str(tmp_path / "audio.sqlite3"),
            "ingest",
            "--input",
            str(input_path),
        ]
    ) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["ok"] is False
    assert "schema_version" in error["error"]


def test_cli_opinion_lookup_grading_stats_and_consensus(
    tmp_path: Path, capsys
) -> None:
    database = tmp_path / "audio.sqlite3"
    input_path = tmp_path / "opinions.json"
    input_path.write_text(
        json.dumps(sample_opinion_payload()),
        encoding="utf-8",
    )
    source_url = "https://www.youtube.com/watch?v=opinion-video"

    assert main(
        [
            "--database",
            str(database),
            "opinions-ingest",
            "--source-url",
            source_url,
            "--input",
            str(input_path),
        ]
    ) == 0
    ingested = json.loads(capsys.readouterr().out)
    assert ingested["observation_count"] == 1

    assert main(
        [
            "--database",
            str(database),
            "source",
            "--source-url",
            source_url,
        ]
    ) == 0
    source = json.loads(capsys.readouterr().out)
    assert source["observations"][0]["picker_name"] == "Simon Hunter"

    assert main(
        [
            "--database",
            str(database),
            "result",
            "--event-id",
            "event-1",
            "--away-score",
            "28",
            "--home-score",
            "20",
        ]
    ) == 0
    graded = json.loads(capsys.readouterr().out)
    assert graded["wrong"] == 1

    assert main(
        [
            "--database",
            str(database),
            "stats",
            "--group-by",
            "picker",
            "--group-by",
            "side_role",
        ]
    ) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["groups"][0]["side_role"] == "underdog"

    assert main(
        [
            "--database",
            str(database),
            "consensus",
            "--event-id",
            "event-1",
        ]
    ) == 0
    consensus = json.loads(capsys.readouterr().out)
    assert consensus["groups"][0]["direction"] == "Las Vegas Raiders"
