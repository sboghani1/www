# nba-live-odds

Captures Bovada live NBA lines (full game, 1H/2H/quarter spread, total,
moneyline) at the end of Q1 and at halftime, plus the pre-match line ~10 min
before tip, with ESPN's game state (score by quarter) at that moment. Purpose:
test live-betting spots against real prices instead of a proxy.

Runs every day: `nba-live-odds.timer` (10:00 ET) starts `nba-live-odds.service`
(oneshot, `receptionist-agent`), which exits at once on a no-game day, else
sleeps until 10 min before the first tip and exits once every game is past
halftime. Data: `/var/lib/nba-live-odds/nba/<YYYYMMDD>/` (`pregame.jsonl`,
`captures.jsonl`, `raw/`, `poll.log`, `state.json`).

API use is deliberately small:
- ESPN scoreboard (free): one call per poll; 30 s only near a Q1/half break
  still needed, 60-150 s otherwise, nothing between halftime and the final.
- Bovada (free; the paid Odds API is never used): one pre-match read per tip
  slot, then only while a game is in a break, 2 captures per game per break
  (2nd one 60 s later at Q1, 5 min later at half — Bovada's first post-break
  line is sometimes the previous quarter's, so use the LAST capture; rows
  carry `same_line_as_previous_checkpoint`), capped at 90 reads per night.
- Bovada's `/basketball/nba` path returns a CDN-cached empty body, so the
  gzip'd `/basketball` sport feed is read and its `/basketball/nba` block kept.
  That feed is occasionally a stale CDN copy too; an unmatched capture does not
  count and is retried on the next poll.

Test: `python3 -m unittest discover -s tests`. Self-test (1 ESPN + 1 Bovada
read): `python3 track_live.py --self-test`.

Install / update (root, via the deploy broker):

    systemd-run --unit=nba-live-odds-install-$(date +%s) --collect --wait --pipe \
      /bin/bash /home/receptionist/repos/www/apps/nba-live-odds/deploy/install-nba-live-odds
