"""Verification review: write what needs a human decision, plus the quality summary.

Files (UTF-8 with BOM so Hindi opens correctly in Excel), rewritten on every run:

  mapping_verification.csv  every official 2003 -> current mapping row and its status
  record_review.csv         flagged or low-confidence elector records, identified by
                            document, page and serial only — no names, so the file can
                            be handed to a reviewer who checks against the source PDF
  result_review.csv         Form 20 rows that fail, or lack, the vote-sum check
  quality_report.json       headline quality, per-roll status and review-queue counts
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database.models import AssemblyConstituency as AC
from ..database.models import Election, ElectionResult, Elector, ElectoralRoll, PollingStation


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _mapping_rows(db: Session) -> list[list]:
    from ..analytics.intelligence import evaluate_mappings, mapping_table

    targets = sorted({m.to_ac_number for m in evaluate_mappings(db) if m.to_ac_number is not None})
    out = []
    for number in targets:
        ac = (db.query(AC).filter(AC.ac_number == number, AC.delimitation == "current")
              .order_by(AC.id).first())
        if ac is None:
            continue
        _, _, rows, _ = mapping_table(db, ac.id)
        out += [[ac.ac_number, ac.ac_name, r.hist_ac_number, r.hist_ac_name or "", r.hist_part,
                 r.hist_area or "", r.current_part or "", r.current_area or "", r.status, r.note]
                for r in rows]
    return out


def _record_rows(db: Session) -> list[list]:
    from ..analytics.intelligence import LOW_CONF_ROW

    q = (db.query(Elector, PollingStation.part_number, ElectoralRoll.roll_year,
                  AC.ac_number, AC.ac_name)
         .join(ElectoralRoll, Elector.electoral_roll_id == ElectoralRoll.id)
         .join(PollingStation, Elector.polling_station_id == PollingStation.id)
         .join(AC, PollingStation.ac_id == AC.id)
         .filter(or_(Elector.is_valid.is_(False), Elector.extraction_confidence < LOW_CONF_ROW))
         .order_by(AC.ac_number, PollingStation.part_number, Elector.serial_number, Elector.id))
    found = q.all()
    ref_ids = {e.duplicate_of_id for e, *_ in found if e.duplicate_of_id}
    serial_of = dict(db.query(Elector.id, Elector.serial_number)
                     .filter(Elector.id.in_(ref_ids)).all()) if ref_ids else {}
    rows = []
    for e, part, year, acn, acname in found:
        issue = ("duplicate" if e.duplicate_kind else
                 "validation" if not e.is_valid else "low_confidence")
        rows.append([year, acn, acname, part, e.serial_number, issue, e.duplicate_kind or "",
                     serial_of.get(e.duplicate_of_id, ""), e.validation_notes or "",
                     round(e.extraction_confidence, 4) if e.extraction_confidence is not None else "",
                     e.source_document or "", e.source_page or ""])
    return rows


def _result_rows(results) -> list[list]:
    """Every Form 20 row that is not verified by the shared row check."""
    return [[r.year, r.ac_number, r.part_number, r.station_name or "", r.total_valid_votes,
             r.rejected_votes, r.tendered_votes, r.total_votes, r.reliability_note,
             r.source_file or "", r.source_page or ""]
            for r in results if not r.verified]


def write_review(db: Session, out_dir: Path, backend: str = "") -> dict:
    from ..analytics.intelligence import quality_overview

    out_dir.mkdir(parents=True, exist_ok=True)
    qo = quality_overview(db, backend)
    mapping = _mapping_rows(db)
    records = _record_rows(db)
    results = _result_rows(qo["results"])

    _write_csv(out_dir / "mapping_verification.csv",
               ["current_ac", "current_ac_name", "historical_ac", "historical_ac_name",
                "historical_part", "historical_area", "current_part", "current_area",
                "status", "reason"], mapping)
    _write_csv(out_dir / "record_review.csv",
               ["roll_year", "ac", "ac_name", "part", "serial", "issue", "duplicate_kind",
                "duplicate_of_serial", "notes", "extraction_confidence", "source_document",
                "source_page"], records)
    _write_csv(out_dir / "result_review.csv",
               ["election_year", "ac", "part", "polling_station", "total_valid_votes",
                "rejected_votes", "tendered_votes", "total_votes", "reason", "source_file",
                "source_page"], results)

    t = qo["totals"]
    status_counts: dict[str, int] = {}
    for row in mapping:
        status_counts[row[8]] = status_counts.get(row[8], 0) + 1
    queue = {
        "mapping_rows": sum(n for s, n in status_counts.items() if s != "Verified"),
        "records": len(records),
        "results": len(results),
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backend": backend,
        "records": {
            "extracted": t.total,
            "mean_confidence": round(t.mean_confidence, 4) if t.mean_confidence is not None else None,
            "validation_flags": t.invalid, "duplicates": t.duplicates,
            "other_flags": t.other_invalid, "low_confidence_rows": t.low_conf,
            "provenance_pct": t.provenance_pct,
        },
        "rolls": [{
            "ac_number": s.ac_number, "part": s.part_number, "extracted": s.total_electors,
            "derived_count": s.official_count, "derived_count_basis": s.official_count_basis,
            "difference": s.difference, "duplicates": s.duplicate_rows,
            "validation_flags": s.invalid_rows, "low_confidence_rows": s.low_confidence_rows,
            "serial_gaps": s.serial_gaps, "stray_serials": s.serial_outliers,
            "mean_confidence": s.mean_confidence,
            "status": "clean" if not s.invalid_rows and not s.serial_outliers else "review",
        } for s in (row["stats"] for row in qo["rolls"])],
        "mapping": status_counts,
        "form20": {k: qo["form20"][k] for k in ("rows", "verified", "review", "zero_margin")},
        "review_queue": queue,
        "files": ["mapping_verification.csv", "record_review.csv", "result_review.csv",
                  "quality_report.json"],
    }
    (out_dir / "quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"dir": out_dir, "queue": queue, "mapping": status_counts, "report": report}
