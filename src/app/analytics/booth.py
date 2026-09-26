"""Booth-level statistics and data-quality metrics.

Discrepancies are never hidden: where the extracted count differs from the
official count the difference is reported in absolute and percentage terms, and
the basis of the "official" figure is always stated.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database.models import (
    AssemblyConstituency, District, Election, Elector, ElectoralRoll, ElectionResult,
    PartMapping, PollingStation, SourceFetch, State,
)

AGE_BANDS = [("18-25", 18, 25), ("26-40", 26, 40), ("41-60", 41, 60), ("61+", 61, 200)]


@dataclass
class BoothStats:
    ac_number: int
    ac_name: str
    district_name: str
    part_number: int
    polling_station_name: str | None
    edition: str
    roll_year: int | None
    roll_type: str | None

    total_electors: int = 0
    male: int = 0
    female: int = 0
    other_unknown: int = 0
    age_bands: dict[str, int] = field(default_factory=dict)
    age_missing: int = 0

    serial_min: int | None = None
    serial_max: int | None = None
    serial_gaps: int = 0
    serial_outliers: list[int] = field(default_factory=list)
    sections: int = 0

    official_count: int | None = None
    official_count_basis: str | None = None
    difference: int | None = None
    difference_pct: float | None = None

    mean_confidence: float = 0.0
    low_confidence_rows: int = 0
    invalid_rows: int = 0
    duplicate_rows: int = 0
    with_epic: int = 0

    source: str | None = None
    source_document: str | None = None
    pdf_url: str | None = None
    page_count: int | None = None
    extraction_method: str | None = None


def booth_stats(db: Session, roll: ElectoralRoll) -> BoothStats:
    station = db.get(PollingStation, roll.polling_station_id)
    ac = db.get(AssemblyConstituency, station.ac_id)
    district = db.get(District, ac.district_id)

    rows = db.scalars(select(Elector).where(
        Elector.electoral_roll_id == roll.id)).all()

    st = BoothStats(
        ac_number=ac.ac_number, ac_name=ac.ac_name,
        district_name=district.district_name, part_number=station.part_number,
        polling_station_name=(station.polling_station_name
                              or station.polling_station_name_local),
        edition=station.edition, roll_year=roll.roll_year, roll_type=roll.roll_type,
        total_electors=len(rows),
        official_count=roll.official_elector_count,
        official_count_basis=roll.official_count_basis,
        source=roll.source, source_document=roll.local_file_path,
        pdf_url=roll.pdf_url, page_count=roll.page_count,
        extraction_method=roll.extraction_method,
    )

    bands = {name: 0 for name, _, _ in AGE_BANDS}
    serials, confs = [], []
    for r in rows:
        if r.gender == "M":
            st.male += 1
        elif r.gender == "F":
            st.female += 1
        else:
            st.other_unknown += 1

        if r.age is None:
            st.age_missing += 1
        else:
            for name, lo, hi in AGE_BANDS:
                if lo <= r.age <= hi:
                    bands[name] += 1
                    break

        if r.serial_number is not None:
            serials.append(r.serial_number)
        if r.extraction_confidence is not None:
            confs.append(r.extraction_confidence)
            if r.extraction_confidence < 0.95:
                st.low_confidence_rows += 1
        if not r.is_valid:
            st.invalid_rows += 1
        if r.duplicate_kind:
            st.duplicate_rows += 1
        if r.epic_number:
            st.with_epic += 1
        if r.section_number is not None:
            st.sections = max(st.sections, r.section_number)

    st.age_bands = bands
    if serials:
        st.serial_min, st.serial_max = min(serials), max(serials)
        present = set(serials)
        # Count gaps up to the roll's expected length, not up to the highest
        # serial printed: a stray serial in the source (AC15 part 10 ends at
        # 2220 after 1275 rows) would otherwise be reported as ~900 gaps.
        upper = st.official_count or st.serial_max
        st.serial_gaps = sum(1 for n in range(st.serial_min, upper + 1)
                             if n not in present)
        st.serial_outliers = sorted(n for n in present if n > upper)
    st.mean_confidence = round(sum(confs) / len(confs), 4) if confs else 0.0

    if st.official_count:
        st.difference = st.total_electors - st.official_count
        st.difference_pct = round(st.difference / st.official_count * 100.0, 2)
    return st


def format_booth_stats(s: BoothStats) -> str:
    lines = [
        f"Booth / Part: {s.part_number}   (AC {s.ac_number} {s.ac_name}, "
        f"{s.district_name}, edition {s.edition})",
        f"Polling station: {s.polling_station_name or '-'}",
        f"Roll: {s.roll_type} {s.roll_year}   extraction: {s.extraction_method}",
        "",
        f"Total electors extracted: {s.total_electors:,}",
        f"  Male:           {s.male:,}",
        f"  Female:         {s.female:,}",
        f"  Other/Unknown:  {s.other_unknown:,}",
        "",
        "Age distribution:",
    ]
    for name, _, _ in AGE_BANDS:
        n = s.age_bands.get(name, 0)
        pct = (n / s.total_electors * 100) if s.total_electors else 0
        lines.append(f"  {name:<6} {n:>6,}  ({pct:5.1f}%)")
    if s.age_missing:
        lines.append(f"  {'n/a':<6} {s.age_missing:>6,}  (age not extractable)")

    lines += [
        "",
        f"Serial range: {s.serial_min}-{s.serial_max}   gaps: {s.serial_gaps}"
        + (f"   stray serials in the source: {s.serial_outliers}"
           if s.serial_outliers else ""),
        f"Sections: {s.sections or 'not printed in this roll format'}",
        f"Records carrying an EPIC: {s.with_epic:,} "
        f"({s.with_epic / s.total_electors * 100:.1f}%)" if s.total_electors else "",
        "",
        "Counts:",
        f"  Official count:  {s.official_count if s.official_count is not None else 'unknown':>8}"
        + (f"   (basis: {s.official_count_basis})" if s.official_count_basis else ""),
        f"  Extracted:       {s.total_electors:>8}",
    ]
    if s.difference is not None:
        lines.append(f"  Difference:      {s.difference:>+8}   ({s.difference_pct:+.2f}%)")
    else:
        lines.append("  Difference:      n/a (no official count available)")

    lines += [
        "",
        "Extraction quality:",
        f"  Mean confidence:        {s.mean_confidence * 100:.2f}%",
        f"  Rows below 95% conf.:   {s.low_confidence_rows:,}",
        f"  Rows failing validation:{s.invalid_rows:,}",
        f"  Duplicates flagged:     {s.duplicate_rows:,}",
        "",
        f"Source: {s.source}",
        f"  document: {s.source_document}  ({s.page_count} pages)",
        f"  url:      {s.pdf_url}",
    ]
    return "\n".join(l for l in lines if l != "")


@dataclass
class QualityReport:
    backend: str = ""
    states: int = 0
    districts: int = 0
    acs_current: int = 0
    acs_2003: int = 0
    polling_stations: int = 0
    rolls: int = 0
    rolls_downloaded: int = 0
    rolls_extracted: int = 0
    electors: int = 0
    electors_valid: int = 0
    electors_duplicate: int = 0
    electors_with_epic: int = 0
    mean_confidence: float = 0.0
    fetches: int = 0
    fetches_ok: int = 0
    part_mappings: int = 0
    election_results: int = 0
    official_total: int = 0
    extracted_total: int = 0

    @property
    def fetch_success_rate(self) -> float:
        return (self.fetches_ok / self.fetches * 100.0) if self.fetches else 0.0

    @property
    def extraction_rate(self) -> float:
        return (self.extracted_total / self.official_total * 100.0
                if self.official_total else 0.0)

    @property
    def validity_rate(self) -> float:
        return (self.electors_valid / self.electors * 100.0) if self.electors else 0.0


def quality_report(db: Session, backend: str = "", state_id: int | None = None) -> QualityReport:
    """Counts over the whole database, or over one state's rows when `state_id` is given."""
    from ..states import booth_state_id, mapping_state_id

    q = QualityReport(backend=backend)
    ac_ids = station_ids = roll_ids = None
    if state_id is not None:
        ac_ids = (select(AssemblyConstituency.id)
                  .join(District, AssemblyConstituency.district_id == District.id)
                  .where(District.state_id == state_id))
        station_ids = select(PollingStation.id).where(PollingStation.ac_id.in_(ac_ids))
        roll_ids = select(ElectoralRoll.id).where(ElectoralRoll.polling_station_id.in_(station_ids))

    def count(model, *where):
        stmt = select(func.count()).select_from(model)
        if state_id is not None:
            scope = {State: State.id == state_id, District: District.state_id == state_id,
                     AssemblyConstituency: AssemblyConstituency.id.in_(ac_ids),
                     PollingStation: PollingStation.id.in_(station_ids),
                     ElectoralRoll: ElectoralRoll.id.in_(roll_ids),
                     Elector: Elector.electoral_roll_id.in_(roll_ids)}
            if model in scope:
                stmt = stmt.where(scope[model])
        return db.scalar(stmt.where(*where)) or 0

    def roll_sum(col):
        stmt = select(func.sum(col))
        if roll_ids is not None:
            stmt = stmt.where(ElectoralRoll.id.in_(roll_ids))
        return db.scalar(stmt) or 0

    q.states = count(State)
    q.districts = count(District)
    q.acs_current = count(AssemblyConstituency, AssemblyConstituency.delimitation == "current")
    q.acs_2003 = count(AssemblyConstituency, AssemblyConstituency.delimitation == "2003")
    q.polling_stations = count(PollingStation)
    q.rolls = count(ElectoralRoll)
    q.rolls_downloaded = count(ElectoralRoll, ElectoralRoll.download_status == "downloaded")
    q.rolls_extracted = count(ElectoralRoll, ElectoralRoll.extraction_status == "extracted")
    q.electors = count(Elector)
    q.electors_valid = count(Elector, Elector.is_valid.is_(True))
    q.electors_duplicate = count(Elector, Elector.duplicate_kind.is_not(None))
    q.electors_with_epic = count(Elector, Elector.epic_number.is_not(None))
    conf = select(func.avg(Elector.extraction_confidence))
    if roll_ids is not None:
        conf = conf.where(Elector.electoral_roll_id.in_(roll_ids))
    q.mean_confidence = round(db.scalar(conf) or 0.0, 4)
    # fetches are shared across states (the ECI gateway serves all of them)
    q.fetches = count(SourceFetch)
    q.fetches_ok = count(SourceFetch, SourceFetch.ok.is_(True))
    if state_id is None:
        q.part_mappings = count(PartMapping)
        q.election_results = count(ElectionResult)
    else:
        booth = booth_state_id(db)
        q.part_mappings = sum(1 for (sid,) in db.execute(select(PartMapping.state_id))
                              if mapping_state_id(sid, booth) == state_id)
        state_name = db.scalar(select(State.state_name).where(State.id == state_id))
        q.election_results = db.scalar(
            select(func.count()).select_from(ElectionResult)
            .join(Election, ElectionResult.election_id == Election.id)
            .where(Election.state == state_name)) or 0
    q.official_total = roll_sum(ElectoralRoll.official_elector_count)
    q.extracted_total = roll_sum(ElectoralRoll.extracted_elector_count)
    return q
