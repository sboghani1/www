from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

import httpx

from .espn import fetch_schedule, fetch_scores_for_date
from .models import ET, parse_timestamp
from .odds import OddsClient, OddsFetchResult
from .scheduler import due_games


class Store(Protocol):
    def read_games(self) -> list[dict[str, Any]]: ...

    def upsert_schedule(
        self, games: list[Any], *, now: datetime
    ) -> tuple[int, int]: ...

    def persist_odds_poll(
        self,
        *,
        due_records: list[dict[str, Any]],
        lines: list[Any],
        requests_used: str,
        requests_remaining: str,
        now: datetime,
    ) -> tuple[int, int]: ...


class DueOddsClient(Protocol):
    def fetch_due(
        self,
        due_records: list[dict[str, Any]],
        *,
        now: datetime,
    ) -> OddsFetchResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class PollOutcome:
    api_called: bool
    due_games: int
    updated_games: int
    appended_snapshots: int
    requests_used: str
    requests_remaining: str
    used_fallback: bool = False


def sync_schedule(
    store: Store,
    *,
    now: datetime,
    http_client: httpx.Client | None = None,
    timeout: float = 20,
) -> tuple[int, int]:
    games = fetch_schedule(
        now=now,
        client=http_client,
        timeout=timeout,
    )
    return store.upsert_schedule(games, now=now)


def backfill_scores(
    store: Store,
    *,
    now: datetime,
    http_client: httpx.Client | None = None,
    timeout: float = 20,
) -> tuple[int, int]:
    """Fill in away_score/home_score for games that have already tipped off
    but have no recorded score, and have aged out of sync_schedule's
    forward-looking rolling window.
    """
    candidates: dict[str, dict[str, Any]] = {}
    missing_dates: set[str] = set()
    for record in store.read_games():
        if str(record.get("away_score") or "") and str(
            record.get("home_score") or ""
        ):
            continue
        commence = str(record.get("commence_time_utc") or "")
        espn_id = str(record.get("espn_event_id") or "")
        if not commence or not espn_id:
            continue
        try:
            tip = parse_timestamp(commence)
        except ValueError:
            continue
        if tip >= now:
            continue
        candidates[espn_id] = record
        missing_dates.add(tip.astimezone(ET).strftime("%Y%m%d"))

    if not candidates:
        return 0, 0

    matched = []
    for date in sorted(missing_dates):
        fetched = fetch_scores_for_date(
            date, client=http_client, timeout=timeout
        )
        matched.extend(
            game
            for game in fetched
            if game.status == "final" and game.espn_event_id in candidates
        )

    if not matched:
        return 0, 0
    return store.upsert_schedule(matched, now=now)


def poll_odds(
    store: Store,
    *,
    now: datetime,
    client_factory: Callable[[], DueOddsClient],
) -> PollOutcome:
    due = due_games(store.read_games(), now)
    if not due:
        return PollOutcome(False, 0, 0, 0, "", "")

    client = client_factory()
    try:
        result = client.fetch_due(due, now=now)
    finally:
        client.close()
    updated, snapshots = store.persist_odds_poll(
        due_records=due,
        lines=result.lines,
        requests_used=result.requests_used,
        requests_remaining=result.requests_remaining,
        now=now,
    )
    return PollOutcome(
        True,
        len(due),
        updated,
        snapshots,
        result.requests_used,
        result.requests_remaining,
        result.used_fallback,
    )


def odds_client_factory(
    *,
    api_key: str,
    fallback_api_key: str = "",
    on_primary_unavailable: Callable[[str], None] | None = None,
    timeout: float,
    retries: int,
) -> Callable[[], OddsClient]:
    return lambda: OddsClient(
        api_key,
        fallback_api_key=fallback_api_key,
        on_primary_unavailable=on_primary_unavailable,
        timeout=timeout,
        retries=retries,
    )
