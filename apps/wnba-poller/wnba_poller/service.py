from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

import httpx

from .espn import fetch_schedule
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
    )


def odds_client_factory(
    *,
    api_key: str,
    timeout: float,
    retries: int,
) -> Callable[[], OddsClient]:
    return lambda: OddsClient(
        api_key,
        timeout=timeout,
        retries=retries,
    )
