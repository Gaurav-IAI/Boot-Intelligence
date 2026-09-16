"""Additive, idempotent schema migrations.

The POC has no Alembic. `Base.metadata.create_all` creates missing tables but
never adds a column to an existing table, so columns introduced after the first
`init-db` are added here. Rules: ADD COLUMN only, always nullable, never drop,
rename or rewrite data. Safe to run on every start.
"""
from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect, text

log = logging.getLogger(__name__)

# (table, column, SQL type) — types chosen to be valid on PostgreSQL and SQLite.
ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("election_results", "candidate_votes_json", "TEXT"),
    ("election_results", "vote_sum_matches", "BOOLEAN"),
]


def apply_additive_migrations(engine: Engine) -> list[str]:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    added: list[str] = []
    for table, column, sql_type in ADDITIVE_COLUMNS:
        if table not in tables:
            continue          # create_all will create it with the column
        existing = {c["name"] for c in insp.get_columns(table)}
        if column in existing:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
        log.info("migration: added %s.%s", table, column)
        added.append(f"{table}.{column}")
    return added
