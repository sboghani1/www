from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any, Mapping

from .config import Config
from .guesser_bot import select_games, selection_id
from .lean_context import resolve_current_game
from .lean_workflow import (
    build_request_template,
    build_skill_prompt,
    parse_request_template,
)
from .sheets import SheetsStore

MAX_REQUEST_BYTES = 16_384


def _validated_event_id(value: Any) -> str:
    event_id = str(value or "")
    if not event_id or len(event_id) > 128:
        raise ValueError("event_id is invalid")
    return event_id


def _public_game(game: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "event_id",
        "espn_event_id",
        "commence_time_utc",
        "commence_time_et",
        "away_team",
        "home_team",
        "bookmaker",
        "latest_captured_at",
        "latest_away_spread",
        "latest_away_spread_price",
        "latest_home_spread",
        "latest_home_spread_price",
        "latest_away_moneyline",
        "latest_home_moneyline",
        "latest_total",
        "latest_over_price",
        "latest_under_price",
        "latest_first_half_away_spread",
        "latest_first_half_away_spread_price",
        "latest_first_half_home_spread",
        "latest_first_half_home_spread_price",
        "latest_first_half_total",
        "latest_first_half_over_price",
        "latest_first_half_under_price",
    )
    return {key: game.get(key, "") for key in allowed}


def _bounded_history(history: Mapping[str, Any]) -> dict[str, Any]:
    thoughts = list(history.get("thoughts") or [])[-50:]
    revisions = list(history.get("revision_history") or [])[-50:]
    thought_fields = (
        "thought_id",
        "submitted_at_et",
        "period",
        "market",
        "side",
        "thought_text",
    )
    revision_fields = (
        "revision_id",
        "requested_at_et",
        "operation",
        "effective_status",
        "full_game_side_selection",
        "full_game_side_strength",
        "full_game_total_selection",
        "full_game_total_strength",
        "first_half_side_selection",
        "first_half_side_strength",
        "first_half_total_selection",
        "first_half_total_strength",
        "summary",
        "git_commit_sha",
    )
    return {
        "game": _public_game(history["game"]),
        "thoughts": [
            {key: item.get(key, "") for key in thought_fields}
            for item in thoughts
        ],
        "active_revision": next(
            (
                {key: item.get(key, "") for key in revision_fields}
                for item in revisions
                if item.get("effective_status") == "active"
            ),
            None,
        ),
        "revision_history": [
            {key: item.get(key, "") for key in revision_fields}
            for item in revisions
        ],
    }


def handle_request(
    request: Mapping[str, Any],
    *,
    store: SheetsStore,
    now: datetime,
) -> dict[str, Any]:
    action = request.get("action")
    if action == "list_games":
        games = select_games(store.read_games(), now=now)
        return {"games": [_public_game(game) for game in games]}
    if action == "validate_template":
        template = str(request.get("template") or "")
        parsed = parse_request_template(template)
        if parsed is None:
            raise ValueError("not a WNBA lean request template")
        game = resolve_current_game(
            store,
            event_id=parsed["event_id"],
            expected_matchup=parsed["matchup"],
            now=now,
        )
        canonical = build_request_template(
            event_id=str(game["event_id"]),
            away_team=str(game["away_team"]),
            home_team=str(game["home_team"]),
            additional_thoughts=parsed["additional_thoughts"],
        )
        return {
            "template": canonical,
            "skill_prompt": build_skill_prompt(canonical),
            "game": _public_game(game),
        }

    event_id = _validated_event_id(request.get("event_id"))
    if action == "game":
        matches = [
            game
            for game in select_games(store.read_games(), now=now)
            if selection_id(game) == event_id
        ]
        if len(matches) != 1:
            raise ValueError("event_id does not identify an upcoming game")
        return {"game": _public_game(matches[0])}
    if action == "history":
        return _bounded_history(
            store.read_game_history(event_id=event_id)
        )
    if action in {"build_generation", "build_undo"}:
        matchup = str(request.get("matchup") or "")
        game = resolve_current_game(
            store,
            event_id=event_id,
            expected_matchup=matchup,
            now=now,
        )
        if action == "build_generation":
            additional = str(request.get("additional_thoughts") or "")
            template = build_request_template(
                event_id=str(game["event_id"]),
                away_team=str(game["away_team"]),
                home_team=str(game["home_team"]),
                additional_thoughts=additional,
            )
            return {
                "template": template,
                "skill_prompt": build_skill_prompt(template),
                "game": _public_game(game),
            }
        return {
            "skill_prompt": (
                "Use the wnba-lean skill to undo the latest published "
                f"lean for immutable event_id {game['event_id']} "
                f"({game['away_team']} @ {game['home_team']}). "
                "Validate the game against current Sheet state and use "
                "the deterministic undo workflow."
            ),
            "game": _public_game(game),
        }
    raise ValueError("unsupported WNBA helper action")


def main() -> None:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        print(json.dumps({"ok": False, "error": "request too large"}))
        raise SystemExit(2)
    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        config = Config.from_env(require_google=True)
        store = SheetsStore.connect(
            sheet_id=config.sheet_id,
            credentials_b64=config.google_credentials_b64,
            service_account_json=config.google_service_account_json,
        )
        result = handle_request(
            request,
            store=store,
            now=datetime.now(tz=UTC),
        )
    except Exception as exc:
        message = str(exc)[:500] or type(exc).__name__
        print(json.dumps({"ok": False, "error": message}))
        raise SystemExit(1) from None
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
