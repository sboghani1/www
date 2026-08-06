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


_PAST_EVENTS_HEADING = "\n# Past Events"
_MODEL_CACHE_HEADING = "\n# Model Cache"
_ENTRY_NUMBER_RE = re.compile(r"(?m)^(\d+)[A-Za-z]")
_RESOLUTION_NAME_RE = re.compile(r"^(\d+)[A-Za-z][A-Za-z0-9_]*$")


def next_past_events_entry_number(document: str) -> int:
    past_start = document.find(_PAST_EVENTS_HEADING)
    cache_start = document.find(_MODEL_CACHE_HEADING)
    if past_start < 0:
        raise ValueError("path210 document is missing the Past Events section")
    section = document[past_start : cache_start if cache_start > past_start else None]
    numbers = [int(match) for match in _ENTRY_NUMBER_RE.findall(section)]
    return (max(numbers) + 1) if numbers else 1


def render_resolution_entry(
    *,
    entry_name: str,
    result: str,
    tags: str,
    line_movement: str,
    context: str,
    model_lean: str,
) -> str:
    if not _RESOLUTION_NAME_RE.fullmatch(entry_name):
        raise ValueError("invalid path210 resolution entry name")
    if result not in {"right", "wrong", "push"}:
        raise ValueError("path210 resolution result must be right, wrong, or push")
    if not tags.strip():
        raise ValueError("path210 resolution entry requires tags")
    if not context.strip().lower().startswith("context:"):
        raise ValueError("path210 resolution context must start with 'context:'")
    lines = [entry_name, result, tags.strip()]
    if line_movement.strip():
        lines.append(f"line movement: {line_movement.strip()}")
    lines.append(context.strip())
    if model_lean.strip():
        lines.append(f"model_lean: {model_lean.strip()}")
    return "\n".join(lines)


def apply_resolution_entry(
    document: str,
    *,
    event_id: str,
    entry_text: str,
) -> str:
    """Remove the event's active WNBA_LEAN_EVENT block (wherever it
    currently sits) and append `entry_text` as a new entry at the end of
    the Past Events section, immediately before the Model Cache heading.
    """
    existing = get_event_block(document, event_id)
    if existing is None:
        raise ValueError("path210 event block does not exist")
    pattern = _block_pattern(event_id)
    without_block = pattern.sub("", document, count=1)
    without_block = re.sub(r"\n{3,}", "\n\n", without_block)

    cache_start = without_block.find(_MODEL_CACHE_HEADING)
    if cache_start < 0:
        raise ValueError("path210 document is missing the Model Cache section")
    before_cache = without_block[:cache_start].rstrip("\n")
    after_cache = without_block[cache_start:]
    return f"{before_cache}\n\n{entry_text.strip()}\n{after_cache}"


def validate_resolution_change(
    before: str,
    after: str,
    *,
    event_id: str,
    entry_text: str,
) -> None:
    if get_event_block(after, event_id) is not None:
        raise ValueError(
            "path210 resolution did not remove the original event block"
        )
    pattern = _block_pattern(event_id)
    if pattern.search(before) is None:
        raise ValueError("path210 event block does not exist")
    entry_stripped = entry_text.strip()
    if entry_stripped not in after:
        raise ValueError(
            "path210 resolution entry is not present in the resulting document"
        )
    # The entry legitimately moves from wherever the block sat (typically
    # Upcoming Events) to the end of Past Events, so a positional diff
    # would fail on the move alone. Instead: remove the block from
    # `before` and the entry from `after`, then compare what's left --
    # this validates "nothing unrelated changed" regardless of where the
    # moving content landed.
    before_remainder = pattern.sub("", before, count=1)
    after_remainder = after.replace(entry_stripped, "", 1)
    before_normalized = re.sub(r"\n{2,}", "\n\n", before_remainder).strip()
    after_normalized = re.sub(r"\n{2,}", "\n\n", after_remainder).strip()
    if before_normalized != after_normalized:
        raise ValueError(
            "path210 resolution modified content outside the target event"
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
