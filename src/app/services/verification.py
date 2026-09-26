"""Post-ingestion verification: complete the links extraction identified, check counts.

Offline and idempotent, so the pipeline runs it on every start. Nothing is deleted
or re-valued: only links that the stored data already names are filled in, and
inconsistencies are reported rather than repaired.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database.models import Election, Elector, ElectionResult, ElectoralRoll, PollingStation
from ..states import BOOTH_STATE

# Validation notes name the record a duplicate repeats: "same name+relative+age as
# serial 332" or "duplicate serial (first seen at row 637)" — 637 is a serial (F-8).
_DUP_REF = re.compile(r"(?:as serial|first seen at row) (\d+)")


@dataclass
class VerificationResult:
    duplicates_linked: int = 0
    duplicates_unresolved: int = 0
    results_linked: int = 0
    results_unlinked: int = 0
    roll_count_mismatches: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.duplicates_linked} duplicate(s) linked to their original"]
        if self.duplicates_unresolved:
            parts.append(f"{self.duplicates_unresolved} duplicate(s) without a resolvable original")
        parts.append(f"{self.results_linked} result(s) linked to their constituency")
        if self.results_unlinked:
            parts.append(f"{self.results_unlinked} result(s) with no matching constituency")
        parts.append(f"{len(self.roll_count_mismatches)} roll count mismatch(es)")
        return "; ".join(parts)


def link_duplicates(db: Session) -> tuple[int, int]:
    """Fill `electors.duplicate_of_id` from the serial named in the validation note."""
    linked = unresolved = 0
    pending = db.query(Elector).filter(Elector.duplicate_kind.is_not(None),
                                       Elector.duplicate_of_id.is_(None)).all()
    for e in pending:
        m = _DUP_REF.search(e.validation_notes or "")
        target = None
        if m:
            target = (db.query(Elector.id)
                      .filter(Elector.electoral_roll_id == e.electoral_roll_id,
                              Elector.serial_number == int(m.group(1)),
                              Elector.id != e.id)
                      .order_by(Elector.id).first())
        if target:
            e.duplicate_of_id = target[0]
            linked += 1
        else:
            unresolved += 1
    db.flush()
    return linked, unresolved


def link_results(db: Session) -> tuple[int, int]:
    """Fill `election_results.ac_id` for rows stored with only an AC number (F-5)."""
    from .pipeline import form20_ac

    linked = unlinked = 0
    cache: dict[int | None, object] = {}
    # form20_ac resolves numbers within the booth state, so only its results are linked here.
    for r in (db.query(ElectionResult).join(Election, ElectionResult.election_id == Election.id)
              .filter(ElectionResult.ac_id.is_(None), Election.state == BOOTH_STATE).all()):
        if r.ac_number not in cache:
            cache[r.ac_number] = form20_ac(db, r.ac_number) if r.ac_number is not None else None
        ac = cache[r.ac_number]
        if ac is not None:
            r.ac_id = ac.id
            linked += 1
        else:
            unlinked += 1
    db.flush()
    return linked, unlinked


def check_roll_counts(db: Session) -> list[str]:
    """Every extracted roll must store exactly as many rows as its parser produced."""
    rows = (db.query(ElectoralRoll, PollingStation.part_number, func.count(Elector.id))
            .join(PollingStation, ElectoralRoll.polling_station_id == PollingStation.id)
            .outerjoin(Elector, Elector.electoral_roll_id == ElectoralRoll.id)
            .filter(ElectoralRoll.extraction_status == "extracted")
            .group_by(ElectoralRoll.id, PollingStation.part_number).all())
    return [f"part {part}: {n} rows stored but {roll.extracted_elector_count} extracted"
            for roll, part, n in rows
            if roll.extracted_elector_count is not None and n != roll.extracted_elector_count]


def verify_all(db: Session) -> VerificationResult:
    v = VerificationResult()
    v.duplicates_linked, v.duplicates_unresolved = link_duplicates(db)
    v.results_linked, v.results_unlinked = link_results(db)
    v.roll_count_mismatches = check_roll_counts(db)
    db.commit()
    return v
