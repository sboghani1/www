from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .config import Config
from .odds_alerts import OddsAlertNotifier
from .service import backfill_scores, odds_client_factory, poll_odds, sync_schedule
from .sheets import SheetsStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wnba-poller",
        description="Maintain WNBA schedules, BetOnline lines, and thoughts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser(
        "initialize-sheet",
        help="Create or explicitly replace the WNBA workbook tabs.",
    )
    initialize.add_argument(
        "--confirm-replace-tabs",
        action="store_true",
        help="Allow replacement of existing WNBA tabs.",
    )
    initialize.add_argument(
        "--remove-legacy-nfl-tabs",
        action="store_true",
        help="Also delete nfl_games, nfl_line_snapshots, and nfl_leans.",
    )
    initialize.add_argument(
        "--backup",
        type=Path,
        help="Write a JSON workbook backup before any deletion.",
    )
    initialize.add_argument(
        "--seed-allowed-user-id",
        type=int,
        help="Add the initial WNBA Guesser Telegram user ID if absent.",
    )
    initialize.add_argument(
        "--seed-allowed-username",
        default="",
        help="Optional username for the initial allowlist row.",
    )
    initialize.add_argument(
        "--seed-allowed-display-name",
        default="",
        help="Optional display name for the initial allowlist row.",
    )

    subparsers.add_parser(
        "sync-schedule",
        help="Upsert the rolling 14-day ESPN WNBA schedule.",
    )
    subparsers.add_parser(
        "backfill-scores",
        help=(
            "Fill in final scores for games that already tipped off but "
            "have no recorded score."
        ),
    )
    subparsers.add_parser(
        "poll-odds",
        help="Poll only games due under the hourly/15-minute policy.",
    )
    subparsers.add_parser(
        "reconcile-thoughts",
        help="Freeze metadata on newly entered Sheet thoughts.",
    )
    status = subparsers.add_parser(
        "status",
        help="Report workbook health without contacting ESPN or The Odds API.",
    )
    status.add_argument("--json", action="store_true")
    return parser


def _store(config: Config) -> SheetsStore:
    return SheetsStore.connect(
        sheet_id=config.sheet_id,
        credentials_b64=config.google_credentials_b64,
        service_account_json=config.google_service_account_json,
    )


def _run(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    require_odds = args.command == "poll-odds"
    config = Config.from_env(require_google=True, require_odds=require_odds)
    store = _store(config)

    if args.command == "initialize-sheet":
        destructive = (
            args.confirm_replace_tabs or args.remove_legacy_nfl_tabs
        )
        if args.remove_legacy_nfl_tabs and not args.confirm_replace_tabs:
            raise ValueError(
                "--remove-legacy-nfl-tabs requires "
                "--confirm-replace-tabs"
            )
        if destructive and args.backup is None:
            raise ValueError(
                "--backup is required with destructive initialization"
            )
        created, removed = store.initialize(
            replace_tabs=args.confirm_replace_tabs,
            backup_path=args.backup,
            remove_legacy_nfl_tabs=args.remove_legacy_nfl_tabs,
            seed_allowed_user_id=args.seed_allowed_user_id,
            seed_allowed_username=args.seed_allowed_username,
            seed_allowed_display_name=args.seed_allowed_display_name,
        )
        print(
            f"Workbook initialized: {created} tab(s) created, "
            f"{removed} tab(s) removed."
        )
        if args.backup:
            print(f"Backup written to {args.backup}.")
        return 0

    if args.command == "sync-schedule":
        created, updated = sync_schedule(
            store,
            now=now,
            timeout=config.http_timeout_seconds,
        )
        print(
            f"Schedule sync succeeded: {created} game(s) added, "
            f"{updated} game(s) updated."
        )
        return 0

    if args.command == "backfill-scores":
        created, updated = backfill_scores(
            store,
            now=now,
            timeout=config.http_timeout_seconds,
        )
        print(f"Score backfill succeeded: {updated} game(s) updated.")
        return 0

    if args.command == "poll-odds":
        alerts = OddsAlertNotifier(
            bot_token=config.odds_alert_bot_token,
            chat_id=config.odds_alert_chat_id,
            low_remaining=config.odds_low_remaining,
            state_path=config.odds_alert_state_path,
        )
        try:
            outcome = poll_odds(
                store,
                now=now,
                client_factory=odds_client_factory(
                    api_key=config.odds_api_key,
                    fallback_api_key=config.odds_api_fallback_key,
                    on_primary_unavailable=lambda reason: (
                        alerts.primary_unavailable(
                            reason,
                            fallback_configured=bool(
                                config.odds_api_fallback_key
                            ),
                        )
                    ),
                    timeout=config.http_timeout_seconds,
                    retries=config.http_retries,
                ),
            )
        finally:
            alerts.close()
        if not outcome.api_called:
            print("No games are due; no Odds API request was made.")
            return 0
        print(
            f"Odds poll succeeded: {outcome.due_games} due, "
            f"{outcome.updated_games} updated, "
            f"{outcome.appended_snapshots} snapshot(s) appended; "
            f"quota used={outcome.requests_used or 'unknown'}, "
            f"remaining={outcome.requests_remaining or 'unknown'}, "
            f"key={'fallback' if outcome.used_fallback else 'primary'}."
        )
        if not outcome.used_fallback:
            try:
                remaining = int(outcome.requests_remaining)
            except (TypeError, ValueError):
                remaining = None
            try:
                used = int(outcome.requests_used)
            except (TypeError, ValueError):
                used = None
            alerts = OddsAlertNotifier(
                bot_token=config.odds_alert_bot_token,
                chat_id=config.odds_alert_chat_id,
                low_remaining=config.odds_low_remaining,
                state_path=config.odds_alert_state_path,
            )
            try:
                alerts.primary_healthy(remaining=remaining, used=used)
            finally:
                alerts.close()
            if (
                remaining is not None
                and remaining <= config.odds_low_remaining
                and not alerts.enabled
            ):
                print(
                    "WARNING: paid Odds API quota is low "
                    f"({remaining} remaining).",
                    file=sys.stderr,
                )
        return 0

    if args.command == "reconcile-thoughts":
        reconciled, unresolved = store.reconcile_thoughts(now=now)
        print(
            f"Thought reconciliation succeeded: {reconciled} reconciled, "
            f"{unresolved} unresolved."
        )
        return 0

    if args.command == "status":
        status = store.status(now=now)
        if args.json:
            print(json.dumps(status, sort_keys=True))
        else:
            for key, value in status.items():
                print(f"{key}: {value}")
        return 0

    raise ValueError(f"Unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except (RuntimeError, ValueError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"ERROR: unexpected {type(exc).__name__}; operation aborted.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
