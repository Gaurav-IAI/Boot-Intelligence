"""Idempotent upserts. Re-running the pipeline must not duplicate rows."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    AssemblyConstituency, District, Elector, ElectoralRoll, Election,
    ElectionResult, PartMapping, PollingStation, SourceFetch, State,
)


def upsert_state(db: Session, *, state_code: str, state_name: str,
                 state_name_local: str | None, state_type: str | None,
                 external_id: int | None, source: str, source_url: str) -> State:
    row = db.scalar(select(State).where(State.state_code == state_code))
    if row is None:
        row = State(state_code=state_code)
        db.add(row)
    row.state_name = state_name
    row.state_name_local = state_name_local
    row.state_type = state_type
    row.external_id = external_id
    row.source = source
    row.source_url = source_url
    db.flush()
    return row


def upsert_district(db: Session, *, state: State, district_code: str,
                    district_number: int | None, district_name: str,
                    district_name_local: str | None, source: str,
                    source_url: str) -> District:
    row = db.scalar(select(District).where(
        District.state_id == state.id, District.district_code == district_code))
    if row is None:
        row = District(state_id=state.id, district_code=district_code)
        db.add(row)
    row.district_number = district_number
    row.district_name = district_name
    row.district_name_local = district_name_local
    row.source = source
    row.source_url = source_url
    db.flush()
    return row


def upsert_ac(db: Session, *, district: District, ac_number: int, ac_name: str,
              ac_name_local: str | None = None, official_code: str | None = None,
              category: str | None = None, pc_number: int | None = None,
              delimitation: str = "current", source: str = "",
              source_url: str | None = None) -> AssemblyConstituency:
    row = db.scalar(select(AssemblyConstituency).where(
        AssemblyConstituency.district_id == district.id,
        AssemblyConstituency.ac_number == ac_number,
        AssemblyConstituency.delimitation == delimitation))
    if row is None:
        row = AssemblyConstituency(district_id=district.id, ac_number=ac_number,
                                   delimitation=delimitation)
        db.add(row)
    row.ac_name = ac_name
    row.ac_name_local = ac_name_local
    row.official_code = official_code
    row.category = category
    row.pc_number = pc_number
    row.source = source
    row.source_url = source_url
    db.flush()
    return row


def upsert_polling_station(db: Session, *, ac: AssemblyConstituency,
                           part_number: int, edition: str, source: str,
                           source_url: str | None = None, **fields) -> PollingStation:
    row = db.scalar(select(PollingStation).where(
        PollingStation.ac_id == ac.id,
        PollingStation.part_number == part_number,
        PollingStation.edition == edition))
    if row is None:
        row = PollingStation(ac_id=ac.id, part_number=part_number, edition=edition)
        db.add(row)
    for k, v in fields.items():
        # Never overwrite a populated value with None: different sources fill in
        # different columns for the same station.
        if v is not None or getattr(row, k, None) is None:
            setattr(row, k, v)
    row.source = source
    if source_url:
        row.source_url = source_url
    db.flush()
    return row


def upsert_roll(db: Session, *, station: PollingStation, roll_year: int,
                roll_type: str, language: str | None, source: str,
                **fields) -> ElectoralRoll:
    row = db.scalar(select(ElectoralRoll).where(
        ElectoralRoll.polling_station_id == station.id,
        ElectoralRoll.roll_year == roll_year,
        ElectoralRoll.roll_type == roll_type,
        ElectoralRoll.language.is_(language) if language is None
        else ElectoralRoll.language == language))
    if row is None:
        row = ElectoralRoll(polling_station_id=station.id, roll_year=roll_year,
                            roll_type=roll_type, language=language)
        db.add(row)
    for k, v in fields.items():
        setattr(row, k, v)
    row.source = source
    db.flush()
    return row


def replace_electors(db: Session, *, roll: ElectoralRoll, rows: list[dict]) -> int:
    """Replace this roll's electors wholesale.

    A roll edition is a single published document: re-extracting it should yield
    the same set, so replacing is correct and keeps re-runs idempotent.
    """
    db.query(Elector).filter(Elector.electoral_roll_id == roll.id).delete()
    db.flush()
    objs = [Elector(electoral_roll_id=roll.id,
                    polling_station_id=roll.polling_station_id, **r) for r in rows]
    db.add_all(objs)
    db.flush()
    return len(objs)


def upsert_election(db: Session, *, election_year: int, election_type: str,
                    state: str, source_url: str | None) -> Election:
    row = db.scalar(select(Election).where(
        Election.election_year == election_year,
        Election.election_type == election_type,
        Election.state == state))
    if row is None:
        row = Election(election_year=election_year, election_type=election_type,
                       state=state)
        db.add(row)
    row.source_url = source_url
    db.flush()
    return row


def replace_results(db: Session, *, election: Election, ac_number: int,
                    rows: list[dict]) -> int:
    db.query(ElectionResult).filter(
        ElectionResult.election_id == election.id,
        ElectionResult.ac_number == ac_number).delete()
    db.flush()
    objs = [ElectionResult(election_id=election.id, ac_number=ac_number, **r)
            for r in rows]
    db.add_all(objs)
    db.flush()
    return len(objs)


def upsert_part_mapping(db: Session, **fields) -> PartMapping:
    row = db.scalar(select(PartMapping).where(
        PartMapping.from_edition == fields["from_edition"],
        PartMapping.from_ac_number == fields["from_ac_number"],
        PartMapping.from_part_number == fields["from_part_number"],
        PartMapping.to_edition == fields["to_edition"],
        PartMapping.to_part_number == fields.get("to_part_number"),
        PartMapping.area_name == fields.get("area_name")))
    if row is None:
        row = PartMapping(**fields)
        db.add(row)
    else:
        for k, v in fields.items():
            setattr(row, k, v)
    db.flush()
    return row


def record_fetch(db: Session, *, source: str, url: str, ok: bool,
                 http_status: int | None = None, content_type: str | None = None,
                 bytes_received: int | None = None, checksum: str | None = None,
                 local_path: str | None = None, note: str | None = None) -> SourceFetch:
    row = SourceFetch(source=source, url=url, ok=ok, http_status=http_status,
                      content_type=content_type, bytes_received=bytes_received,
                      checksum_sha256=checksum, local_path=local_path, note=note,
                      created_at=datetime.now(timezone.utc))
    db.add(row)
    db.flush()
    return row
