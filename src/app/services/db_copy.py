"""Copy a whole database (e.g. the local SQLite file) into another (the server's PostgreSQL).

Used when deploying: the data already loaded on one machine moves to the server
as-is, so the server neither downloads nor re-parses anything. Tables are copied
in dependency order with their primary keys, in batches; afterwards PostgreSQL's id
sequences are moved past the copied ids so new rows do not collide.

The target must be empty (a fresh database): a partial or repeated copy would mix
two datasets, so it is refused unless `replace=True`, which first empties every
table of the target.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import Engine, create_engine, func, inspect, select, text

from ..database.migrations import apply_additive_migrations
from ..database.models import Base

log = logging.getLogger(__name__)
BATCH = 5000


@dataclass
class CopyReport:
    rows: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.rows.values())


def _prepare(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    apply_additive_migrations(engine)


def copy_database(source_url: str, target_url: str, *, replace: bool = False,
                  progress=None) -> CopyReport:
    src = create_engine(source_url, future=True)
    dst = create_engine(target_url, future=True)
    _prepare(src)          # an older source file may lack additive columns: add them (nullable)
    _prepare(dst)
    tables = Base.metadata.sorted_tables                     # parents before children
    with dst.connect() as conn:
        filled = [t.name for t in tables if conn.execute(select(func.count()).select_from(t)).scalar()]
    if filled and not replace:
        raise RuntimeError(f"target database is not empty ({', '.join(filled)}); "
                           "use --replace to empty it first")
    report = CopyReport()
    with dst.begin() as out:
        if filled:
            for t in reversed(tables):                        # children before parents
                out.execute(t.delete())
        with src.connect() as inp:
            source_cols = {t.name: {c["name"] for c in inspect(src).get_columns(t.name)} for t in tables}
            for t in tables:
                cols = [c for c in t.columns if c.name in source_cols[t.name]]
                result = inp.execution_options(stream_results=True).execute(select(*cols))
                n = 0
                while batch := result.fetchmany(BATCH):
                    out.execute(t.insert(), [dict(r._mapping) for r in batch])
                    n += len(batch)
                    if progress:
                        progress(t.name, n)
                report.rows[t.name] = n
        if dst.dialect.name == "postgresql":
            for t in tables:
                pk = list(t.primary_key.columns)
                if len(pk) == 1 and pk[0].autoincrement and str(pk[0].type) == "INTEGER":
                    out.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{t.name}', '{pk[0].name}'), "
                        f"COALESCE((SELECT MAX({pk[0].name}) FROM {t.name}), 0) + 1, false)"))
    return report
