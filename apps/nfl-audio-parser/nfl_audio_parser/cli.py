from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .opinions import OpinionStore
from .store import AudioParserStore

DEFAULT_DATABASE = Path("/home/forwarder/data/nfl_audio_parser.sqlite3")


def _database_path(value: str | None) -> Path:
    return Path(
        value
        or os.getenv("NFL_AUDIO_PARSER_DB", "")
        or DEFAULT_DATABASE
    ).expanduser()


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Store exact sports-audio evidence and query NFL/NBA opinions."
        )
    )
    parser.add_argument(
        "--database",
        help=f"SQLite path (default: {DEFAULT_DATABASE})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize or migrate the database.")

    ingest = subparsers.add_parser(
        "ingest", help="Validate and ingest parsed JSON."
    )
    ingest.add_argument(
        "--input",
        default="-",
        help="JSON file path, or - for stdin (default).",
    )

    list_parser = subparsers.add_parser("list", help="List recent parser runs.")
    list_parser.add_argument("--limit", type=int, default=20)

    show = subparsers.add_parser("show", help="Show one run and extraction.")
    show.add_argument("--run-id", required=True)
    show.add_argument("--revision-id")

    review = subparsers.add_parser(
        "review", help="Append an approval or rejection."
    )
    review.add_argument("--revision-id", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--decision", choices=("approved", "rejected"), required=True)
    review.add_argument("--notes", default="")

    opinions_ingest = subparsers.add_parser(
        "opinions-ingest",
        help="Ingest normalized NFL/NBA opinion observations.",
    )
    opinions_ingest.add_argument("--source-url", required=True)
    opinions_ingest.add_argument(
        "--input",
        default="-",
        help="JSON file path, or - for stdin (default).",
    )

    source = subparsers.add_parser(
        "source",
        help="Show all current opinions extracted from one source URL.",
    )
    source.add_argument("--source-url", required=True)

    timeline = subparsers.add_parser(
        "timeline",
        help="Show one picker's opinion timeline for an event.",
    )
    timeline.add_argument("--picker", required=True)
    timeline.add_argument("--event-id", required=True)
    timeline.add_argument("--market-family")

    result = subparsers.add_parser(
        "result",
        help="Record a final score and grade current observations.",
    )
    result.add_argument("--event-id", required=True)
    result.add_argument("--away-score", required=True, type=int)
    result.add_argument("--home-score", required=True, type=int)

    stats = subparsers.add_parser(
        "stats",
        help="Aggregate picker records by situational dimensions.",
    )
    stats.add_argument(
        "--group-by",
        action="append",
        required=True,
        choices=(
            "picker",
            "sport",
            "conviction_tier",
            "side_role",
            "market_family",
            "change_type",
            "opinion_stage",
            "program",
            "agreement_bucket",
            "agreement_count",
            "picker_count",
        ),
    )
    stats.add_argument(
        "--policy",
        choices=("latest_pregame", "all_observations"),
        default="latest_pregame",
    )
    stats.add_argument("--sport", choices=("nfl", "nba"))

    consensus = subparsers.add_parser(
        "consensus",
        help="Group latest pregame opinions by direction.",
    )
    consensus.add_argument("--event-id", required=True)
    consensus.add_argument("--market-family", default="game_side")
    consensus.add_argument(
        "--minimum-conviction",
        type=int,
        choices=range(0, 6),
        default=0,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = AudioParserStore(_database_path(args.database))
    opinions = OpinionStore(store.path)
    try:
        if args.command == "init":
            store.initialize()
            result: Any = {"ok": True, "database": str(store.path)}
        elif args.command == "ingest":
            result = {"ok": True, **store.ingest(_load_json(args.input))}
        elif args.command == "list":
            result = {"ok": True, "runs": store.list_runs(limit=args.limit)}
        elif args.command == "show":
            result = {
                "ok": True,
                **store.get_run(args.run_id, revision_id=args.revision_id),
            }
        elif args.command == "review":
            result = {
                "ok": True,
                "decision_id": store.add_review_decision(
                    args.revision_id,
                    reviewer=args.reviewer,
                    decision=args.decision,
                    notes=args.notes,
                ),
            }
        elif args.command == "opinions-ingest":
            result = {
                "ok": True,
                **opinions.ingest(
                    _load_json(args.input),
                    source_url=args.source_url,
                ),
            }
        elif args.command == "source":
            result = {"ok": True, **opinions.source(args.source_url)}
        elif args.command == "timeline":
            result = {
                "ok": True,
                "timeline": opinions.timeline(
                    picker_name=args.picker,
                    event_id=args.event_id,
                    market_family=args.market_family,
                ),
            }
        elif args.command == "result":
            result = {
                "ok": True,
                **opinions.record_result(
                    event_id=args.event_id,
                    away_score=args.away_score,
                    home_score=args.home_score,
                ),
            }
        elif args.command == "stats":
            result = {
                "ok": True,
                "policy": args.policy,
                "groups": opinions.stats(
                    group_by=args.group_by,
                    policy=args.policy,
                    sport=args.sport,
                ),
            }
        elif args.command == "consensus":
            result = {
                "ok": True,
                "groups": opinions.consensus(
                    event_id=args.event_id,
                    market_family=args.market_family,
                    minimum_conviction=args.minimum_conviction,
                ),
            }
        else:
            raise AssertionError(f"Unhandled command: {args.command}")
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        ValueError,
    ) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
