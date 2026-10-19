from pathlib import Path

from receptionist2.database import Database


def make_database(tmp_path: Path) -> Database:
    result = Database(tmp_path / "state" / "receptionist2.db")
    result.initialize()
    return result


def test_message_acceptance_is_deduplicated_and_snapshots_runtime(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    database.ensure_user(
        10,
        20,
        default_model="claude-opus-4-8",
        default_effort="max",
    )
    first, created = database.accept_message(
        user_id=10,
        chat_id=20,
        message_id=30,
        text="do the work",
        default_model="ignored",
        default_effort="low",
    )
    duplicate, duplicate_created = database.accept_message(
        user_id=10,
        chat_id=20,
        message_id=30,
        text="do the work",
        default_model="ignored",
        default_effort="low",
    )

    assert created
    assert not duplicate_created
    assert duplicate["id"] == first["id"]
    assert first["model"] == "claude-opus-4-8"
    assert first["effort"] == "max"


def test_nfl_audio_collection_queues_once_and_cleans_paths(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    collection, created = database.start_nfl_audio_collection(
        user_id=10,
        chat_id=20,
        season=2026,
        week=7,
        default_model="model",
        default_effort="max",
    )
    duplicate, duplicate_created = database.start_nfl_audio_collection(
        user_id=10,
        chat_id=20,
        season=2026,
        week=8,
        default_model="ignored",
        default_effort="ignored",
    )
    assert created
    assert not duplicate_created
    assert duplicate["id"] == collection["id"]
    assert duplicate["week"] == 7

    audio_path = tmp_path / "clip.ogg"
    audio_path.write_bytes(b"audio")
    item, item_created = database.add_nfl_audio_item(
        collection_id=collection["id"],
        chat_id=20,
        message_id=30,
        kind="voice",
        mime_type="audio/ogg",
        local_path=str(audio_path),
        duration_seconds=45,
        size_bytes=5,
        max_items=24,
    )
    item_duplicate, item_duplicate_created = database.add_nfl_audio_item(
        collection_id=collection["id"],
        chat_id=20,
        message_id=30,
        kind="voice",
        mime_type="audio/ogg",
        local_path=str(audio_path),
        duration_seconds=45,
        size_bytes=5,
        max_items=24,
    )
    assert item_created
    assert not item_duplicate_created
    assert item_duplicate["id"] == item["id"]

    run, run_created = database.queue_nfl_audio_collection(
        collection_id=collection["id"],
        message_id=40,
        exact_prompt="review only",
    )
    assert run_created
    assert run["source"] == "message"
    assert run["exact_prompt"] == "review only"
    assert database.active_nfl_audio_collection(10, 20) is None
    assert database.nfl_audio_paths_for_run(run["id"]) == [str(audio_path)]

    database.mark_nfl_audio_run_cleaned(run["id"], succeeded=True)
    finished = database.get_nfl_audio_collection(collection["id"])
    assert finished["status"] == "completed"
    assert database.nfl_audio_paths_for_run(run["id"]) == []


def test_nfl_audio_collection_cancellation_returns_owned_paths(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    collection, _ = database.start_nfl_audio_collection(
        user_id=10,
        chat_id=20,
        season=2026,
        week=7,
        default_model="model",
        default_effort="max",
    )
    database.add_nfl_audio_item(
        collection_id=collection["id"],
        chat_id=20,
        message_id=30,
        kind="audio",
        mime_type="audio/mpeg",
        local_path="/state/nfl-audio/clip.mp3",
        duration_seconds=60,
        size_bytes=100,
        max_items=24,
    )

    cancelled, paths = database.cancel_nfl_audio_collection(10, 20)
    assert cancelled and cancelled["id"] == collection["id"]
    assert paths == ["/state/nfl-audio/clip.mp3"]
    assert (
        database.get_nfl_audio_collection(collection["id"])["status"]
        == "cancelled"
    )
    assert database.pending_nfl_audio_cleanup()[0]["id"] == collection["id"]

    database.mark_nfl_audio_collection_cleaned(collection["id"])
    assert database.nfl_audio_items(collection["id"])[0]["local_path"] == ""
    assert database.pending_nfl_audio_cleanup() == []


def test_runtime_changes_only_affect_new_runs(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    database.ensure_user(
        10,
        20,
        default_model="claude-opus-4-8",
        default_effort="medium",
    )
    first, _ = database.accept_message(
        user_id=10,
        chat_id=20,
        message_id=1,
        text="first",
        default_model="ignored",
        default_effort="ignored",
    )
    database.set_session_runtime(
        10, model="claude-sonnet-4-6", effort="max"
    )
    second, _ = database.accept_message(
        user_id=10,
        chat_id=20,
        message_id=2,
        text="second",
        default_model="ignored",
        default_effort="ignored",
    )

    assert (first["model"], first["effort"]) == (
        "claude-opus-4-8",
        "medium",
    )
    assert (second["model"], second["effort"]) == (
        "claude-sonnet-4-6",
        "max",
    )


def test_run_completion_and_outbox_are_atomic(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    session = database.ensure_user(
        10,
        20,
        default_model="model",
        default_effort="max",
    )
    run, _ = database.accept_message(
        user_id=10,
        chat_id=20,
        message_id=1,
        text="prompt",
        default_model=session["model"],
        default_effort=session["effort"],
    )
    claimed = database.claim_next_run()
    assert claimed and claimed["id"] == run["id"]

    database.finish_run(
        run["id"],
        status="succeeded",
        final_response="answer",
        provider_session_id="provider-session",
        usage={"total_cost_usd": 0.1},
    )

    outbox = database.claim_outbox()
    assert outbox and outbox["body"] == "answer"
    assert (
        database.active_session(10)["provider_session_id"]
        == "provider-session"
    )


def test_schedule_has_dedicated_session_and_idempotent_occurrences(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    active = database.ensure_user(
        10,
        20,
        default_model="model",
        default_effort="max",
    )
    schedule = database.create_schedule(
        user_id=10,
        name="morning",
        cron="0 8 * * *",
        timezone="America/Los_Angeles",
        prompt="check updates",
        model="model",
        effort="max",
        next_run_at="2026-09-27T15:00:00+00:00",
    )

    first, created = database.enqueue_scheduled_run(
        schedule, "2026-09-27T15:00:00+00:00"
    )
    duplicate, duplicate_created = database.enqueue_scheduled_run(
        schedule, "2026-09-27T15:00:00+00:00"
    )

    assert schedule["session_id"] != active["id"]
    assert first["session_id"] == schedule["session_id"]
    assert first["scheduled_for"] == "2026-09-27T15:00:00+00:00"
    assert created
    assert not duplicate_created
    assert duplicate["id"] == first["id"]


def test_schedule_resumes_its_dedicated_provider_session(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    database.ensure_user(
        1, 2, default_model="model", default_effort="max"
    )
    schedule = database.create_schedule(
        user_id=1,
        name="morning",
        cron="0 8 * * *",
        timezone="America/Los_Angeles",
        prompt="check",
        model="model",
        effort="max",
        next_run_at="2026-09-27T15:00:00+00:00",
    )
    first, _ = database.enqueue_scheduled_run(
        schedule, "2026-09-27T15:00:00+00:00"
    )
    database.finish_run(
        first["id"],
        status="succeeded",
        final_response="done",
        provider_session_id="provider-session",
    )

    second, _ = database.enqueue_scheduled_run(
        schedule, "2026-09-28T15:00:00+00:00"
    )

    assert second["provider_session_id"] == "provider-session"


def test_restart_timeout_can_be_immediate_bounded_or_unlimited(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    assert database.create_restart_request(1, 2, 0)["timeout_seconds"] == 0
    assert (
        database.create_restart_request(1, 2, 600)["timeout_seconds"] == 600
    )
    assert (
        database.create_restart_request(1, 2, None)["timeout_seconds"] is None
    )


def test_database_files_are_group_writable(tmp_path: Path) -> None:
    import os
    import stat

    previous = os.umask(0o022)
    try:
        database = make_database(tmp_path)
        database.set_state("probe", "1")
    finally:
        os.umask(previous)
    for suffix in ("", "-wal", "-shm"):
        path = database.path.with_name(database.path.name + suffix)
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) & 0o060 == 0o060, suffix
