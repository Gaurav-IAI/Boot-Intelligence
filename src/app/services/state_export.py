"""Export one state's data to a small file, and import it into another database.

For sharing a state's data with a deployment that already has other states: the
production database keeps everything it has, and gains (or has replaced) only the
exported states.

Export writes a SQLite file (optionally gzipped) holding, for the chosen states: the
state, districts, constituencies, polling stations with their rolls and electors,
elections and results, part mappings, and the load records (SourceFetch rows of the
state's CEO sources, which let later refreshes skip work already done). Contact
records are never exported.

Import runs in one transaction. Rows of an exported state already in the target are
removed first (only that state's), then every row is inserted with its id shifted past
the target's highest id — and every link shifted the same way — so ids never collide.
PostgreSQL sequences are moved past the new ids afterwards.
"""
from __future__ import annotations

import gzip
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, create_engine, delete, func, or_, select, text

from ..database.migrations import apply_additive_migrations
from ..database.models import (
    AssemblyConstituency, Base, District, Election, ElectionResult, ElectoralRoll, Elector,
    PartMapping, PollingStation, SourceFetch, State,
)
from ..states import spec

BATCH = 5000
# parents before children; every table an exported state's rows live in
TABLES = [State, District, AssemblyConstituency, PollingStation, ElectoralRoll, Elector,
          Election, ElectionResult, PartMapping, SourceFetch]
# column -> the table whose id it holds
LINKS = {
    District: {"state_id": State},
    AssemblyConstituency: {"district_id": District},
    PollingStation: {"ac_id": AssemblyConstituency},
    ElectoralRoll: {"polling_station_id": PollingStation},
    Elector: {"polling_station_id": PollingStation, "electoral_roll_id": ElectoralRoll,
              "duplicate_of_id": Elector},
    ElectionResult: {"election_id": Election, "ac_id": AssemblyConstituency},
    PartMapping: {"state_id": State},
}


@dataclass
class TransferReport:
    states: list[str] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.rows.values())


def _prepare(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    apply_additive_migrations(engine)


def _fetch_prefixes(names: list[str]) -> list[str]:
    return [p for n in names for p in (spec(n).source_prefixes if spec(n) else ())]


def _scopes(names: list[str]) -> dict:
    """SELECT filters picking one state's rows in each table (as subqueries, no id lists)."""
    states = select(State.id).where(State.state_name.in_(names))
    districts = select(District.id).where(District.state_id.in_(states))
    acs = select(AssemblyConstituency.id).where(AssemblyConstituency.district_id.in_(districts))
    stations = select(PollingStation.id).where(PollingStation.ac_id.in_(acs))
    elections = select(Election.id).where(Election.state.in_(names))
    prefixes = _fetch_prefixes(names)
    return {
        State: State.id.in_(states),
        District: District.id.in_(districts),
        AssemblyConstituency: AssemblyConstituency.id.in_(acs),
        PollingStation: PollingStation.id.in_(stations),
        ElectoralRoll: ElectoralRoll.polling_station_id.in_(stations),
        Elector: Elector.polling_station_id.in_(stations),
        Election: Election.id.in_(elections),
        ElectionResult: ElectionResult.election_id.in_(elections),
        PartMapping: PartMapping.state_id.in_(states),
        SourceFetch: or_(*[SourceFetch.source.like(p + "%") for p in prefixes]) if prefixes else False,
    }


def export_states(source_url: str, out: Path, names: list[str]) -> TransferReport:
    """Write the named states' rows (ids as in the source) to a new SQLite file."""
    src = create_engine(source_url, future=True)
    with src.connect() as conn:
        found = [n for n in names if conn.execute(select(State.id).where(State.state_name == n)).first()]
    missing = sorted(set(names) - set(found))
    if missing:
        raise LookupError(f"not in the source database: {', '.join(missing)}")
    gz = out.suffix == ".gz"
    db_path = Path(tempfile.mkdtemp()) / "export.sqlite3" if gz else out
    if db_path.exists():
        db_path.unlink()
    dst = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    _prepare(dst)
    report = TransferReport(states=found)
    scopes = _scopes(found)
    with src.connect() as inp, dst.begin() as outc:
        for t in TABLES:
            cols = list(t.__table__.columns)
            result = inp.execution_options(stream_results=True).execute(select(*cols).where(scopes[t]))
            n = 0
            while batch := result.fetchmany(BATCH):
                outc.execute(t.__table__.insert(), [dict(r._mapping) for r in batch])
                n += len(batch)
            report.rows[t.__tablename__] = n
    dst.dispose()
    if gz:
        with open(db_path, "rb") as f, gzip.open(out, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g)
        shutil.rmtree(db_path.parent, ignore_errors=True)
    return report


def import_states(bundle: Path, target_url: str) -> TransferReport:
    """Add the states in an export file to the target database, replacing only those states."""
    tmp = None
    if bundle.suffix == ".gz":
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "import.sqlite3"
        with gzip.open(bundle, "rb") as g, open(path, "wb") as f:
            shutil.copyfileobj(g, f)
    else:
        path = bundle
    try:
        src = create_engine(f"sqlite:///{path.as_posix()}", future=True)
        _prepare(src)
        dst = create_engine(target_url, future=True)
        _prepare(dst)
        with src.connect() as inp:
            names = list(inp.execute(select(State.state_name)).scalars())
        if not names:
            raise LookupError(f"{bundle} holds no state")
        report = TransferReport(states=names)
        with dst.begin() as out:
            # 1. remove what the target already has for these states (children first)
            scopes = _scopes(names)
            for t in reversed(TABLES):
                if t is Elector:        # an elector may point at a duplicate in the same state
                    out.execute(Elector.__table__.update().where(scopes[Elector]).values(duplicate_of_id=None))
                out.execute(delete(t.__table__).where(scopes[t]))
            # 2. shift ids past the target's highest, links alike
            offset = {t: (out.execute(select(func.max(t.__table__.c.id))).scalar() or 0) for t in TABLES}
            with src.connect() as inp:
                for t in TABLES:
                    links = LINKS.get(t, {})
                    result = inp.execution_options(stream_results=True).execute(select(t.__table__))
                    n = 0
                    while batch := result.fetchmany(BATCH):
                        rows = []
                        for r in batch:
                            row = dict(r._mapping)
                            row["id"] += offset[t]
                            for col, parent in links.items():
                                if row.get(col) is not None:
                                    row[col] += offset[parent]
                            rows.append(row)
                        out.execute(t.__table__.insert(), rows)
                        n += len(rows)
                    report.rows[t.__tablename__] = n
            if dst.dialect.name == "postgresql":
                for t in TABLES:
                    name = t.__tablename__
                    out.execute(text(f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), "
                                     f"COALESCE((SELECT MAX(id) FROM {name}), 0) + 1, false)"))
        src.dispose()
        dst.dispose()
        return report
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
