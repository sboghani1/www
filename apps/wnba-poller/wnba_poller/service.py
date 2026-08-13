from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Protocol

import httpx

from .espn import fetch_schedule, fetch_scores_for_date
from .models import ET, WNBA_TEAMS, parse_timestamp
from .odds import OddsClient, OddsFetchResult
from .scheduler import due_games


class Store(Protocol):
    def read_games(self) -> list[dict[str, Any]]: ...

    def upsert_schedule(
        self, games: list[Any], *, now: datetime
    ) -> tuple[int, int]: ...

    def upsert_results(
        self, records: list[dict[str, Any]]
    ) -> tuple[int, int]: ...

    def upsert_season_streaks(
        self, records: list[dict[str, Any]]
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


def backfill_results(
    store: Store,
    *,
    start_date: str,
    now: datetime,
    http_client: httpx.Client | None = None,
    timeout: float = 20,
) -> tuple[int, int]:
    """Upsert every completed WNBA game from ``start_date`` (YYYYMMDD) through
    today's Eastern date into the results log, one free ESPN request per date.

    Idempotent (keyed by espn_event_id), so the one-time season seed and the
    daily incremental pass share this path -- the seed passes the season-open
    date; the daily job passes a short lookback.
    """
    try:
        start = datetime.strptime(start_date, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("start_date must be an 8-digit YYYYMMDD string") from exc
    end = now.astimezone(ET).date()

    records: list[dict[str, Any]] = []
    current = start
    while current <= end:
        fetched = fetch_scores_for_date(
            current.strftime("%Y%m%d"), client=http_client, timeout=timeout
        )
        iso_date = current.isoformat()
        for game in fetched:
            if (
                game.status != "final"
                or game.away_score is None
                or game.home_score is None
            ):
                continue
            # Drop exhibitions (e.g. the All-Star Game) so streaks only cover
            # real franchises.
            if (
                game.away_team not in WNBA_TEAMS
                or game.home_team not in WNBA_TEAMS
            ):
                continue
            winner = (
                game.away_team
                if game.away_score > game.home_score
                else game.home_team
            )
            records.append(
                {
                    "espn_event_id": game.espn_event_id,
                    "game_date_et": iso_date,
                    "away_team": game.away_team,
                    "home_team": game.home_team,
                    "away_score": game.away_score,
                    "home_score": game.home_score,
                    "winner": winner,
                    "away_q1": game.away_q1,
                    "away_q2": game.away_q2,
                    "away_q3": game.away_q3,
                    "away_q4": game.away_q4,
                    "home_q1": game.home_q1,
                    "home_q2": game.home_q2,
                    "home_q3": game.home_q3,
                    "home_q4": game.home_q4,
                }
            )
        current += timedelta(days=1)

    if not records:
        return 0, 0
    return store.upsert_results(records)


def backfill_season_streaks(
    store: Store,
    *,
    season: int,
    now: datetime,
    http_client: httpx.Client | None = None,
    timeout: float = 20,
) -> tuple[int, int]:
    """Compute each franchise's longest win/loss streak for a full season from
    ESPN (free) and upsert one row per team into wnba_season_streaks. Sweeps
    May 1 -> Oct 31 of `season` (covers regular season + playoffs); re-runnable.
    """
    from .streaks import compute_streaks

    games: list[dict[str, Any]] = []
    current = date(season, 5, 1)
    end = date(season, 10, 31)
    while current <= end:
        for game in fetch_scores_for_date(
            current.strftime("%Y%m%d"), client=http_client, timeout=timeout
        ):
            if (
                game.status != "final"
                or game.away_score is None
                or game.home_score is None
                or game.away_team not in WNBA_TEAMS
                or game.home_team not in WNBA_TEAMS
            ):
                continue
            winner = (
                game.away_team
                if game.away_score > game.home_score
                else game.home_team
            )
            games.append(
                {
                    "game_date_et": current.isoformat(),
                    "away_team": game.away_team,
                    "home_team": game.home_team,
                    "winner": winner,
                }
            )
        current += timedelta(days=1)

    teams = compute_streaks(games)["teams"]
    if not teams:
        return 0, 0
    stamp = now.astimezone(timezone.utc).isoformat()
    records = [
        {
            "season": season,
            "team": team,
            "wins": data["wins"],
            "losses": data["losses"],
            "games": data["wins"] + data["losses"],
            "longest_win_streak": data["longest_win"],
            "longest_loss_streak": data["longest_loss"],
            "updated_at_utc": stamp,
        }
        for team, data in sorted(teams.items())
    ]
    return store.upsert_season_streaks(records)


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
