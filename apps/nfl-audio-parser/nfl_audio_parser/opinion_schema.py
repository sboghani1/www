from __future__ import annotations

import sqlite3

from www_sqlite import Migration


def _create_opinion_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE prediction_windows (
            window_id TEXT PRIMARY KEY,
            sport TEXT NOT NULL CHECK(sport IN ('nfl', 'nba')),
            valid_from_date TEXT NOT NULL,
            valid_through_date TEXT NOT NULL,
            valid_timezone TEXT NOT NULL,
            label TEXT,
            created_at_utc TEXT NOT NULL,
            UNIQUE(
                sport, valid_from_date, valid_through_date, valid_timezone
            )
        )
        """,
        """
        CREATE TABLE programs (
            program_id TEXT PRIMARY KEY,
            normalized_name TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE pickers (
            picker_id TEXT PRIMARY KEY,
            normalized_name TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE content_items (
            content_id TEXT PRIMARY KEY,
            source_provider TEXT NOT NULL,
            source_reference_hash TEXT NOT NULL,
            program_id TEXT REFERENCES programs(program_id),
            published_at_utc TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(source_provider, source_reference_hash)
        )
        """,
        """
        CREATE TABLE sporting_events (
            event_id TEXT PRIMARY KEY,
            sport TEXT NOT NULL CHECK(sport IN ('nfl', 'nba')),
            commence_time_utc TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_team TEXT NOT NULL,
            status TEXT NOT NULL,
            away_score INTEGER,
            home_score INTEGER,
            result_updated_at_utc TEXT
        )
        """,
        """
        CREATE TABLE content_revisions (
            content_revision_id TEXT PRIMARY KEY,
            content_id TEXT NOT NULL
                REFERENCES content_items(content_id) ON DELETE CASCADE,
            window_id TEXT NOT NULL
                REFERENCES prediction_windows(window_id),
            source_start_seconds REAL NOT NULL,
            source_end_seconds REAL NOT NULL,
            segment_sha256 TEXT,
            extraction_revision_id TEXT
                REFERENCES extraction_revisions(revision_id),
            payload_sha256 TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(
                content_id, window_id, source_start_seconds,
                source_end_seconds, payload_sha256
            )
        )
        """,
        """
        CREATE TABLE opinion_threads (
            thread_id TEXT PRIMARY KEY,
            picker_id TEXT NOT NULL REFERENCES pickers(picker_id),
            event_id TEXT NOT NULL REFERENCES sporting_events(event_id),
            market_family TEXT NOT NULL,
            period TEXT NOT NULL,
            subject_key TEXT NOT NULL DEFAULT '',
            prop_stat TEXT NOT NULL DEFAULT '',
            created_at_utc TEXT NOT NULL,
            UNIQUE(
                picker_id, event_id, market_family, period,
                subject_key, prop_stat
            )
        )
        """,
        """
        CREATE TABLE opinion_observations (
            observation_id TEXT PRIMARY KEY,
            content_revision_id TEXT NOT NULL
                REFERENCES content_revisions(content_revision_id)
                ON DELETE CASCADE,
            observation_index INTEGER NOT NULL,
            thread_id TEXT NOT NULL
                REFERENCES opinion_threads(thread_id),
            source_seconds REAL NOT NULL,
            contract_type TEXT NOT NULL,
            direction TEXT,
            line REAL,
            price INTEGER,
            side_role TEXT NOT NULL
                CHECK(side_role IN (
                    'favorite', 'underdog', 'pickem', 'unknown',
                    'not_applicable'
                )),
            opinion_status TEXT NOT NULL
                CHECK(opinion_status IN (
                    'active', 'conditional', 'pass', 'retracted'
                )),
            opinion_stage TEXT NOT NULL
                CHECK(opinion_stage IN (
                    'early_look', 'analysis', 'betting_card',
                    'contest', 'final'
                )),
            conviction_score INTEGER NOT NULL
                CHECK(conviction_score BETWEEN 0 AND 5),
            conviction_label TEXT NOT NULL,
            declared_units REAL,
            declared_rank INTEGER,
            hedged INTEGER NOT NULL CHECK(hedged IN (0, 1)),
            conditional INTEGER NOT NULL CHECK(conditional IN (0, 1)),
            condition_text TEXT,
            bet_structure TEXT NOT NULL,
            group_id TEXT,
            leg_index INTEGER,
            selection TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            explicit_markers_json TEXT NOT NULL,
            extraction_confidence REAL,
            review_flags_json TEXT NOT NULL,
            normalized_observation_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(content_revision_id, observation_index)
        )
        """,
        """
        CREATE TABLE opinion_grades (
            observation_id TEXT NOT NULL
                REFERENCES opinion_observations(observation_id)
                ON DELETE CASCADE,
            grading_method TEXT NOT NULL,
            result TEXT NOT NULL
                CHECK(result IN ('right', 'wrong', 'push', 'ungradeable')),
            graded_at_utc TEXT NOT NULL,
            details_json TEXT NOT NULL,
            PRIMARY KEY(observation_id, grading_method)
        )
        """,
        """
        CREATE INDEX content_items_source
            ON content_items(source_provider, source_reference_hash)
        """,
        """
        CREATE INDEX content_revisions_window
            ON content_revisions(window_id, content_id, created_at_utc)
        """,
        """
        CREATE INDEX sporting_events_sport_time
            ON sporting_events(sport, commence_time_utc)
        """,
        """
        CREATE INDEX opinion_threads_event
            ON opinion_threads(event_id, market_family, period)
        """,
        """
        CREATE INDEX opinion_observations_thread
            ON opinion_observations(thread_id, content_revision_id, source_seconds)
        """,
        """
        CREATE VIEW current_content_revisions AS
        SELECT revision.*
        FROM content_revisions revision
        WHERE NOT EXISTS (
            SELECT 1
            FROM content_revisions newer
            WHERE newer.content_id=revision.content_id
              AND newer.window_id=revision.window_id
              AND newer.source_start_seconds=revision.source_start_seconds
              AND newer.source_end_seconds=revision.source_end_seconds
              AND (
                    newer.created_at_utc > revision.created_at_utc
                    OR (
                        newer.created_at_utc = revision.created_at_utc
                        AND newer.content_revision_id >
                            revision.content_revision_id
                    )
              )
        )
        """,
        """
        CREATE VIEW current_opinion_observations AS
        SELECT observation.*, revision.content_id, revision.window_id,
               content.published_at_utc, window.sport,
               window.valid_from_date, window.valid_through_date,
               picker.picker_id, picker.display_name AS picker_name,
               thread.event_id, thread.market_family, thread.period,
               thread.subject_key, thread.prop_stat,
               event.commence_time_utc, event.away_team, event.home_team
        FROM opinion_observations observation
        JOIN current_content_revisions revision
          ON revision.content_revision_id=observation.content_revision_id
        JOIN content_items content ON content.content_id=revision.content_id
        JOIN prediction_windows window ON window.window_id=revision.window_id
        JOIN opinion_threads thread ON thread.thread_id=observation.thread_id
        JOIN pickers picker ON picker.picker_id=thread.picker_id
        JOIN sporting_events event ON event.event_id=thread.event_id
        """,
        """
        CREATE VIEW opinion_timeline AS
        WITH ordered AS (
            SELECT observation.*,
                   LAG(direction) OVER (
                       PARTITION BY thread_id
                       ORDER BY published_at_utc, source_seconds,
                                observation_index
                   ) AS previous_direction,
                   LAG(contract_type) OVER (
                       PARTITION BY thread_id
                       ORDER BY published_at_utc, source_seconds,
                                observation_index
                   ) AS previous_contract_type,
                   LAG(line) OVER (
                       PARTITION BY thread_id
                       ORDER BY published_at_utc, source_seconds,
                                observation_index
                   ) AS previous_line,
                   LAG(conviction_score) OVER (
                       PARTITION BY thread_id
                       ORDER BY published_at_utc, source_seconds,
                                observation_index
                   ) AS previous_conviction_score,
                   ROW_NUMBER() OVER (
                       PARTITION BY thread_id
                       ORDER BY published_at_utc, source_seconds,
                                observation_index
                   ) AS timeline_position
            FROM current_opinion_observations observation
        )
        SELECT ordered.*,
               CASE
                   WHEN timeline_position=1 THEN 'initial'
                   WHEN opinion_status='pass' THEN 'passed'
                   WHEN opinion_status='retracted' THEN 'retracted'
                   WHEN direction IS NOT previous_direction
                       THEN 'switched_direction'
                   WHEN contract_type != previous_contract_type
                       THEN 'changed_contract'
                   WHEN conviction_score > previous_conviction_score
                       THEN 'strengthened'
                   WHEN conviction_score < previous_conviction_score
                       THEN 'weakened'
                   WHEN line IS NOT previous_line THEN 'changed_line'
                   ELSE 'reaffirmed'
               END AS change_type
        FROM ordered
        """,
        """
        CREATE VIEW opinion_strength_context AS
        SELECT observation.*,
               RANK() OVER (
                   PARTITION BY picker_id, window_id
                   ORDER BY conviction_score DESC,
                            (declared_rank IS NULL), declared_rank,
                            published_at_utc, source_seconds
               ) AS picker_window_strength_rank,
               COUNT(*) OVER (
                   PARTITION BY picker_id, window_id
               ) AS picker_window_observation_count,
               RANK() OVER (
                   PARTITION BY content_id
                   ORDER BY conviction_score DESC,
                            (declared_rank IS NULL), declared_rank,
                            published_at_utc, source_seconds
               ) AS content_strength_rank,
               COUNT(*) OVER (
                   PARTITION BY content_id
               ) AS content_observation_count
        FROM current_opinion_observations observation
        """,
        """
        CREATE VIEW latest_pregame_opinions AS
        WITH ranked AS (
            SELECT timeline.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY thread_id
                       ORDER BY published_at_utc DESC, source_seconds DESC,
                                observation_index DESC
                   ) AS recency_rank
            FROM opinion_timeline timeline
            WHERE published_at_utc < commence_time_utc
        )
        SELECT ranked.*
        FROM ranked
        WHERE recency_rank=1
          AND opinion_status IN ('active', 'conditional')
        """,
        """
        CREATE VIEW latest_pregame_consensus_context AS
        WITH counted AS (
            SELECT opinion.*,
                   COUNT(*) OVER (
                       PARTITION BY event_id, market_family, period,
                                    subject_key, prop_stat
                   ) AS picker_count,
                   COUNT(*) OVER (
                       PARTITION BY event_id, market_family, period,
                                    subject_key, prop_stat, direction
                   ) AS agreement_count
            FROM latest_pregame_opinions opinion
        )
        SELECT counted.*,
               CASE
                   WHEN picker_count < 2 THEN 'solo'
                   WHEN agreement_count = picker_count THEN 'unanimous'
                   WHEN agreement_count * 2 > picker_count THEN 'majority'
                   WHEN agreement_count * 2 = picker_count THEN 'split'
                   ELSE 'minority'
               END AS agreement_bucket
        FROM counted
        """,
    )
    for statement in statements:
        connection.execute(statement)


OPINION_MIGRATION = Migration(
    2,
    "add-sports-opinion-analytics",
    _create_opinion_schema,
)
