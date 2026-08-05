from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_START = "<!-- WNBA_LEAN_EVENT_START event_id={event_id} -->"
_END = "<!-- WNBA_LEAN_EVENT_END event_id={event_id} -->"


def _validate_event_id(event_id: str) -> str:
    if not _EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("invalid event_id for path210 marker")
    return event_id


def _block_pattern(event_id: str) -> re.Pattern[str]:
    event_id = _validate_event_id(event_id)
    start = re.escape(_START.format(event_id=event_id))
    end = re.escape(_END.format(event_id=event_id))
    return re.compile(rf"{start}\n.*?\n{end}", re.DOTALL)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _render_choice(label: str, choice: Mapping[str, Any]) -> list[str]:
    evidence = choice.get("evidence") or []
    watch = choice.get("watch_conditions") or []
    lines = [
        (
            f"- **{label}:** {choice['selection']} "
            f"({choice['strength']})"
        )
    ]
    if evidence:
        lines.append(
            "  - Evidence: " + "; ".join(str(item) for item in evidence)
        )
    if watch:
        lines.append(
            "  - Watch: " + "; ".join(str(item) for item in watch)
        )
    return lines


def render_event_block(
    *,
    game: Mapping[str, Any],
    revision_id: str,
    status: str,
    output: Mapping[str, Any] | None,
) -> str:
    event_id = _validate_event_id(str(game["event_id"]))
    if status not in {"active", "deleted"}:
        raise ValueError("path210 lean status must be active or deleted")
    matchup = f"{game['away_team']} @ {game['home_team']}"
    lines = [
        _START.format(event_id=event_id),
        f"## WNBA Lean: {matchup}",
        f"- Event ID: `{event_id}`",
        f"- Revision: `{revision_id}`",
        f"- Status: `{status}`",
    ]
    if status == "deleted":
        lines.append("- This lean was deleted through append-only revision history.")
    else:
        if output is None:
            raise ValueError("active path210 block requires lean output")
        lines.extend(["", "### Full game"])
        lines.extend(_render_choice("Side", output["full_game"]["side"]))
        lines.extend(_render_choice("Total", output["full_game"]["total"]))
        first_half = output.get("first_half") or {}
        if first_half:
            lines.extend(["", "### First half"])
            if first_half.get("side"):
                lines.extend(
                    _render_choice("Side", first_half["side"])
                )
            if first_half.get("total"):
                lines.extend(
                    _render_choice("Total", first_half["total"])
                )
        lines.extend(["", f"**Summary:** {output['summary']}"])
    lines.append(_END.format(event_id=event_id))
    return "\n".join(lines)


def get_event_block(document: str, event_id: str) -> str | None:
    matches = _block_pattern(event_id).findall(document)
    if len(matches) > 1:
        raise ValueError("duplicate path210 event blocks")
    return matches[0] if matches else None


def apply_event_block(
    document: str,
    *,
    event_id: str,
    operation: str,
    new_block: str,
) -> str:
    pattern = _block_pattern(event_id)
    matches = list(pattern.finditer(document))
    if len(matches) > 1:
        raise ValueError("duplicate path210 event blocks")
    if operation == "create":
        if matches:
            raise ValueError("path210 event block already exists")
        separator = "\n\n" if document.endswith("\n") else "\n\n"
        return document.rstrip("\n") + separator + new_block + "\n"
    if operation not in {"revise", "delete", "undo"}:
        raise ValueError("unsupported path210 operation")
    if not matches:
        raise ValueError("path210 event block does not exist")
    return (
        document[: matches[0].start()]
        + new_block
        + document[matches[0].end() :]
    )


def validate_event_change(
    before: str,
    after: str,
    *,
    event_id: str,
    expected_block: str,
) -> None:
    actual = get_event_block(after, event_id)
    if actual != expected_block:
        raise ValueError("path210 target block does not match expected output")
    pattern = _block_pattern(event_id)
    before_block = pattern.search(before)
    if before_block is None:
        after_without = pattern.sub("", after).rstrip("\n")
        if before.rstrip("\n") != after_without:
            raise ValueError(
                "path210 change modified content outside target block"
            )
        return
    before_without = pattern.sub("<WNBA_TARGET_BLOCK>", before)
    after_without = pattern.sub("<WNBA_TARGET_BLOCK>", after)
    if before_without != after_without:
        raise ValueError("path210 change modified content outside target block")
