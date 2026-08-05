from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from .models import eastern_timestamp, utc_timestamp

HELPER_VERSION = "1"
VALIDATION_VERSION = "1"

LEAN_REVISION_HEADERS = [
    "record_id",
    "revision_id",
    "record_type",
    "event_id",
    "espn_event_id",
    "commence_time_utc",
    "commence_time_et",
    "away_team",
    "home_team",
    "operation",
    "resulting_status",
    "target_revision_id",
    "supersedes_revision_id",
    "requested_at_utc",
    "requested_at_et",
    "source",
    "telegram_user_id",
    "telegram_chat_id",
    "telegram_message_id",
    "request_text",
    "full_game_side_selection",
    "full_game_side_strength",
    "full_game_side_evidence_json",
    "full_game_side_watch_json",
    "full_game_total_selection",
    "full_game_total_strength",
    "full_game_total_evidence_json",
    "full_game_total_watch_json",
    "first_half_side_selection",
    "first_half_side_strength",
    "first_half_side_evidence_json",
    "first_half_side_watch_json",
    "first_half_total_selection",
    "first_half_total_strength",
    "first_half_total_evidence_json",
    "first_half_total_watch_json",
    "summary",
    "source_snapshot_ids_json",
    "path_block_id",
    "content_hash",
    "git_base_sha",
    "git_commit_sha",
    "pushed_branch",
    "validation_version",
    "helper_version",
    "receipt_for_revision_id",
    "error",
]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _choice_fields(
    output: Mapping[str, Any] | None,
    period: str,
    market: str,
) -> dict[str, Any]:
    choice = (
        output.get(period, {}).get(market)
        if isinstance(output, Mapping)
        and isinstance(output.get(period), Mapping)
        else None
    )
    prefix = f"{period}_{market}"
    if not isinstance(choice, Mapping):
        return {
            f"{prefix}_selection": "",
            f"{prefix}_strength": "",
            f"{prefix}_evidence_json": "",
            f"{prefix}_watch_json": "",
        }
    return {
        f"{prefix}_selection": choice.get("selection", ""),
        f"{prefix}_strength": choice.get("strength", ""),
        f"{prefix}_evidence_json": _json(choice.get("evidence", [])),
        f"{prefix}_watch_json": _json(
            choice.get("watch_conditions", [])
        ),
    }


def build_revision_event(
    *,
    game: Mapping[str, Any],
    operation: str,
    output: Mapping[str, Any] | None,
    request_text: str,
    source: str,
    now: datetime,
    git_base_sha: str,
    content_hash: str,
    target_revision_id: str = "",
    supersedes_revision_id: str = "",
    telegram_metadata: Mapping[str, Any] | None = None,
    revision_id: str | None = None,
    id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> dict[str, Any]:
    if operation not in {"create", "revise", "delete", "undo"}:
        raise ValueError("unsupported lean revision operation")
    revision_id = revision_id or id_factory()
    if not revision_id or len(revision_id) > 100:
        raise ValueError("revision_id is invalid")
    resulting_status = "deleted" if operation == "delete" else "active"
    metadata = telegram_metadata or {}
    record = {header: "" for header in LEAN_REVISION_HEADERS}
    record.update(
        {
            "record_id": f"revision:{revision_id}",
            "revision_id": revision_id,
            "record_type": "revision",
            "event_id": game.get("event_id", ""),
            "espn_event_id": game.get("espn_event_id", ""),
            "commence_time_utc": game.get("commence_time_utc", ""),
            "commence_time_et": game.get("commence_time_et", ""),
            "away_team": game.get("away_team", ""),
            "home_team": game.get("home_team", ""),
            "operation": operation,
            "resulting_status": resulting_status,
            "target_revision_id": target_revision_id,
            "supersedes_revision_id": supersedes_revision_id,
            "requested_at_utc": utc_timestamp(now),
            "requested_at_et": eastern_timestamp(now),
            "source": source,
            "telegram_user_id": metadata.get("telegram_user_id", ""),
            "telegram_chat_id": metadata.get("telegram_chat_id", ""),
            "telegram_message_id": metadata.get(
                "telegram_message_id", ""
            ),
            "request_text": request_text,
            "summary": output.get("summary", "") if output else "",
            "source_snapshot_ids_json": _json(
                output.get("source_snapshot_ids", []) if output else []
            ),
            "path_block_id": f"wnba-lean:{game.get('event_id', '')}",
            "content_hash": content_hash,
            "git_base_sha": git_base_sha,
            "validation_version": VALIDATION_VERSION,
            "helper_version": HELPER_VERSION,
        }
    )
    for period in ("full_game", "first_half"):
        for market in ("side", "total"):
            record.update(_choice_fields(output, period, market))
    return record


def build_publication_receipt(
    *,
    revision_id: str,
    event_id: str,
    commit_sha: str,
    branch: str,
    now: datetime,
) -> dict[str, Any]:
    if not revision_id or not commit_sha:
        raise ValueError("revision_id and commit_sha are required")
    record = {header: "" for header in LEAN_REVISION_HEADERS}
    record.update(
        {
            "record_id": f"publication:{revision_id}:{commit_sha}",
            "revision_id": revision_id,
            "record_type": "publication_receipt",
            "event_id": event_id,
            "requested_at_utc": utc_timestamp(now),
            "requested_at_et": eastern_timestamp(now),
            "git_commit_sha": commit_sha,
            "pushed_branch": branch,
            "validation_version": VALIDATION_VERSION,
            "helper_version": HELPER_VERSION,
            "receipt_for_revision_id": revision_id,
        }
    )
    return record


def build_abort_receipt(
    *,
    revision_id: str,
    event_id: str,
    error: str,
    now: datetime,
) -> dict[str, Any]:
    record = {header: "" for header in LEAN_REVISION_HEADERS}
    record.update(
        {
            "record_id": f"abort:{revision_id}",
            "revision_id": revision_id,
            "record_type": "abort_receipt",
            "event_id": event_id,
            "requested_at_utc": utc_timestamp(now),
            "requested_at_et": eastern_timestamp(now),
            "validation_version": VALIDATION_VERSION,
            "helper_version": HELPER_VERSION,
            "receipt_for_revision_id": revision_id,
            "error": error[:1000],
        }
    )
    return record


def derive_revision_history(
    records: Iterable[Mapping[str, Any]],
    *,
    event_id: str,
) -> dict[str, Any]:
    relevant = [
        dict(record)
        for record in records
        if str(record.get("event_id") or "") == event_id
    ]
    revisions = {
        str(record.get("revision_id") or ""): record
        for record in relevant
        if record.get("record_type") == "revision"
        and record.get("revision_id")
    }
    published = {
        str(record.get("receipt_for_revision_id") or "")
        for record in relevant
        if record.get("record_type") == "publication_receipt"
        and record.get("git_commit_sha")
    }
    aborted = {
        str(record.get("receipt_for_revision_id") or "")
        for record in relevant
        if record.get("record_type") == "abort_receipt"
    }
    ordered = sorted(
        (
            record
            for revision_id, record in revisions.items()
            if revision_id in published and revision_id not in aborted
        ),
        key=lambda record: str(record.get("requested_at_utc") or ""),
    )
    active_id = ""
    for record in ordered:
        if record.get("operation") == "delete":
            active_id = ""
        else:
            active_id = str(record["revision_id"])

    history: list[dict[str, Any]] = []
    for record in ordered:
        item = dict(record)
        if record.get("operation") == "delete":
            item["effective_status"] = "deleted"
        elif str(record.get("revision_id")) == active_id:
            item["effective_status"] = "active"
        else:
            item["effective_status"] = "superseded"
        receipt = next(
            (
                candidate
                for candidate in relevant
                if candidate.get("record_type")
                == "publication_receipt"
                and candidate.get("receipt_for_revision_id")
                == record.get("revision_id")
            ),
            None,
        )
        if receipt:
            item["git_commit_sha"] = receipt.get("git_commit_sha", "")
            item["pushed_branch"] = receipt.get("pushed_branch", "")
        history.append(item)
    active = next(
        (
            record
            for record in history
            if record.get("effective_status") == "active"
        ),
        None,
    )
    return {
        "active_revision": active,
        "revision_history": history,
        "unpublished_revision_ids": sorted(
            set(revisions) - published - aborted
        ),
        "aborted_revision_ids": sorted(aborted),
    }


def revision_to_output(record: Mapping[str, Any]) -> dict[str, Any]:
    def choice(period: str, market: str) -> dict[str, Any] | None:
        prefix = f"{period}_{market}"
        selection = str(record.get(f"{prefix}_selection") or "")
        if not selection:
            return None
        try:
            evidence = json.loads(
                str(record.get(f"{prefix}_evidence_json") or "[]")
            )
            watch = json.loads(
                str(record.get(f"{prefix}_watch_json") or "[]")
            )
        except json.JSONDecodeError as exc:
            raise ValueError("stored lean revision JSON is invalid") from exc
        return {
            "selection": selection,
            "strength": str(
                record.get(f"{prefix}_strength") or ""
            ),
            "evidence": evidence,
            "watch_conditions": watch,
        }

    try:
        snapshot_ids = json.loads(
            str(record.get("source_snapshot_ids_json") or "[]")
        )
    except json.JSONDecodeError as exc:
        raise ValueError("stored snapshot ID JSON is invalid") from exc
    output: dict[str, Any] = {
        "full_game": {
            "side": choice("full_game", "side"),
            "total": choice("full_game", "total"),
        },
        "first_half": {},
        "summary": str(record.get("summary") or ""),
        "source_snapshot_ids": snapshot_ids,
    }
    for market in ("side", "total"):
        value = choice("first_half", market)
        if value:
            output["first_half"][market] = value
    return output
