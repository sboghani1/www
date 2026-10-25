from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from decision_helper.models import MarketEvent, MarketQuote, timestamp


def make_events(now: datetime | None = None) -> list[MarketEvent]:
    start = (now or datetime.now(UTC)) + timedelta(days=2)
    return [
        MarketEvent(
            id="nfl-1",
            sport="nfl",
            commence_time=timestamp(start),
            away_team="Buffalo Bills",
            home_team="Kansas City Chiefs",
            markets={
                "winner:Buffalo Bills": MarketQuote(None, 120, 5),
                "winner:Kansas City Chiefs": MarketQuote(None, -140, 5),
                "spread:Buffalo Bills": MarketQuote(2.5, -110, 5),
                "spread:Kansas City Chiefs": MarketQuote(-2.5, -110, 5),
                "total:Over": MarketQuote(47.5, -110, 5),
                "total:Under": MarketQuote(47.5, -110, 5),
            },
        ),
        MarketEvent(
            id="nba-1",
            sport="nba",
            commence_time=timestamp(start + timedelta(hours=3)),
            away_team="Boston Celtics",
            home_team="Los Angeles Lakers",
            markets={
                "winner:Boston Celtics": MarketQuote(None, -150, 4),
                "winner:Los Angeles Lakers": MarketQuote(None, 130, 4),
            },
        ),
    ]


def extraction_payload() -> dict:
    return {
        "picks": [
            {
                "sport": "nfl",
                "event_id": "nfl-1",
                "pick_type": "spread",
                "selection": "Buffalo Bills",
                "line": 3.0,
                "my_line_low": 4.5,
                "my_line_high": 3.5,
                "confidence_probability": 0.65,
                "confidence_label": "65%",
                "reasoning": "Chiefs secondary is banged up",
                "uncertainties": ["Allen ankle"],
            },
            {
                "sport": "nfl",
                "event_id": "nfl-1",
                "pick_type": "total",
                "selection": "under",
                "line": None,
                "my_line_high": 44,
                "confidence_probability": None,
                "confidence_label": "lean",
                "reasoning": "wind",
                "uncertainties": [],
            },
            {
                "sport": "nba",
                "event_id": "nba-1",
                "pick_type": "winner",
                "selection": "Boston Celtics",
                "line": None,
                "confidence_probability": 0.7,
                "confidence_label": "",
                "reasoning": "rest edge",
                "uncertainties": [],
            },
        ],
        "immediate_feedback": "Good specificity; quantify the wind effect.",
    }


def write_fake_claude(path: Path, response: dict | str, *, exit_code: int = 0) -> Path:
    """Fake `claude` CLI that records argv/env and emits stream-json."""
    result = response if isinstance(response, str) else json.dumps(response)
    log = path.parent / "claude-calls.jsonl"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "        'env': {k: v for k, v in os.environ.items()\n"
        "                if k.startswith(('ANTHROPIC', 'CLAUDE'))}}) + '\\n')\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's'}))\n"
        f"print(json.dumps({{'type': 'result', 'is_error': {exit_code != 0}, "
        f"'result': {result!r}}}))\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return log
