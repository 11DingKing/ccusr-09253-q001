"""Idempotent SQLite schema migration for persisted databases.

The event order used to be derived from the business ``event_id`` string,
which breaks for mixed upstream numbering schemes. Order now lives in an
explicit server-assigned monotonic ``events.seq`` per plan. This module
upgrades databases created by older versions without dropping data:

* ``events.seq`` is added and backfilled from ``rowid`` (which is exactly
  the historical server-side insertion order);
* a partial unique index guards seq allocation afterwards;
* a per-plan counter table is created and seeded;
* ``freezes.event_cutoff_seq`` records the numeric cutoff of every freeze
  (legacy freeze rows simply stay NULL and keep being read from JSON).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

_COUNTER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS plan_event_counters (
    plan_version VARCHAR(128) NOT NULL PRIMARY KEY,
    next_seq INTEGER NOT NULL
)
"""

_PLAN_SEQ_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_events_plan_seq "
    "ON events (plan_version, seq)"
)

_PLAN_SEQ_UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_events_plan_seq "
    "ON events (plan_version, seq) WHERE seq IS NOT NULL"
)


def _table_columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).all()
    return {row[1] for row in rows}


def _has_column(conn, table_name: str, column_name: str) -> bool:
    return column_name in _table_columns(conn, table_name)


def run_migrations(engine: Engine) -> None:
    """Upgrade a database to the current schema. Safe to run repeatedly."""
    with engine.begin() as conn:
        event_columns = _table_columns(conn, "events")
        if "seq" not in event_columns:
            conn.execute(text("ALTER TABLE events ADD COLUMN seq INTEGER"))
        # rowid of an INTEGER PRIMARY KEY table is the insertion order, which
        # is precisely the order the server implicitly used before this fix.
        conn.execute(text("UPDATE events SET seq = id WHERE seq IS NULL"))
        conn.execute(text(_PLAN_SEQ_INDEX))
        conn.execute(text(_PLAN_SEQ_UNIQUE_INDEX))

        conn.execute(text(_COUNTER_TABLE_SQL))
        # Seed counters above every already-persisted seq. INSERT OR IGNORE
        # keeps a counter that already exists untouched.
        conn.execute(
            text(
                "INSERT OR IGNORE INTO plan_event_counters (plan_version, next_seq) "
                "SELECT plan_version, COALESCE(MAX(seq), 0) + 1 "
                "FROM events GROUP BY plan_version"
            )
        )

        freeze_columns = _table_columns(conn, "freezes")
        if "event_cutoff_seq" not in freeze_columns:
            conn.execute(
                text("ALTER TABLE freezes ADD COLUMN event_cutoff_seq INTEGER")
            )
