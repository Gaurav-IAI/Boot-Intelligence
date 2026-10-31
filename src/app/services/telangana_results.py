"""Telangana Form 20 (booth level): 2018 read by OCR; 2023 not loaded.

The CEO Telangana result sheets are scanned. For 2018 each sheet is read cell by cell
(extraction/parsers/form20_tg_ocr.py), and a booth row is stored only when exactly one
reading of its digits satisfies the sheet's own printed arithmetic. Unread booths are
left out and the coverage is recorded ("N of M booths"), never filled in.

A sheet is stored only if, on top of that:
* its candidate columns number exactly the candidates in the ECI statistical report
  for that constituency (load `results --state Telangana` first);
* no candidate column's read booths already add up to more than the sheet's printed
  per-candidate Total row (that would prove a misread);
* at least FORM20_MIN_BOOTHS booths reconcile.

Candidate names are attached only by exact numbers: a column is named when its printed
Total row equals one candidate's EVM (general) votes, and its grand Total row, where
read, equals that candidate's total in the ECI report. If any column stays unnamed,
the sheet's names are not stored at all.

2023: local OCR reconciled under 10% of rows on sample sheets, so it is not attempted.
Constituency-level results for both years come from the ECI report (services/ac_results.py).
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor

from sqlalchemy.orm import Session

from ..config import settings
from ..database import repositories as repo
from ..database.models import AcResult, Election
from ..extraction.parsers import form20_tg_ocr as ocr
from ..extraction.pdf.downloader import download_file
from ..sources.ceo_telangana.client import SOURCE_TG_FORM20, TG_FORM20_INDEXES, TgForm20Client
from .state_results import FORM20_MIN_BOOTHS, FORM20_SOURCE_URLS, TG, note_prefix, processed, state_ac

log = logging.getLogger(__name__)
OCR_YEARS = (2018,)


def _workers() -> int:
    return max(1, int(os.environ.get("TG_OCR_WORKERS") or (os.cpu_count() or 2) - 2))


def eci_candidates(db: Session, year: int, ac_number: int) -> list[AcResult]:
    """The ECI report's candidates for this constituency-year, NOTA excluded."""
    return (db.query(AcResult).join(Election, AcResult.election_id == Election.id)
            .filter(Election.state == TG, Election.election_year == year, AcResult.ac_number == ac_number)
            .filter(AcResult.party != "NOTA").all())


def name_columns(sheet: ocr.TgSheet, eci: list[AcResult]) -> list[dict] | None:
    """[{name, party}] per candidate column, or None unless every column is named.

    A column is named when one ECI candidate matches it twice over: one reading of the
    column's printed EVM Total equals the candidate's general votes, and one reading of its
    grand Total (after postal ballots) equals the candidate's total votes."""
    evm, grand = sheet.label_row("evm"), sheet.label_row("grand")
    if evm is None or grand is None or not evm.options or not grand.options:
        return None
    n = len(eci)
    if len(evm.options) < n or len(grand.options) < n:
        return None
    picked: list[int | None] = []
    for j in range(n):
        hits = [i for i, c in enumerate(eci) if str(c.general_votes) in evm.options[j]
                and str(c.total_votes) in grand.options[j]]
        picked.append(hits[0] if len(hits) == 1 else None)
    if None in picked or len(set(picked)) != len(picked):
        return None
    return [{"name": eci[i].candidate_name, "party": eci[i].party} for i in picked]


def read_sheet(path, pool: ProcessPoolExecutor) -> ocr.TgSheet:
    import pymupdf
    with pymupdf.open(str(path)) as doc:
        n = doc.page_count
    pages = list(pool.map(ocr.read_page, [str(path)] * n, range(n)))
    return ocr.assemble(pages)


def gate(sheet: ocr.TgSheet, eci: list[AcResult], names: list[dict] | None = None) -> str | None:
    """Why the sheet cannot be stored, or None."""
    if not eci:
        return "no ECI constituency result to check the columns against (run `results --state Telangana` first)"
    if sheet.candidate_columns != len(eci):
        return (f"{sheet.candidate_columns} candidate columns found; the ECI report lists "
                f"{len(eci)} candidates")
    over = ocr.column_overreads(sheet)
    if over:
        return f"read booths exceed the printed Total row in column(s) {', '.join(str(j + 1) for j in over)}"
    read = sheet.booths_read
    if len(read) < FORM20_MIN_BOOTHS:
        return f"only {len(read)} booths reconcile"
    sums = [sum(r.votes[j] for r in read) for j in range(sheet.candidate_columns)]
    complete = len(read) == sheet.booths_expected
    if names:
        # named columns: the ECI report's EVM (general) votes cap what the booths can add up to
        general = {(c.candidate_name, c.party): c.general_votes for c in eci}
        cap = [general[(n["name"], n["party"])] for n in names]
        over = [j for j, (s_, c) in enumerate(zip(sums, cap)) if c is not None and s_ > c]
        if over:
            return ("read booths add up to more than the ECI report's EVM votes for "
                    + ", ".join(names[j]["name"] for j in over))
        if complete and sums != cap:
            return "every booth was read but the columns do not add up to the ECI report's EVM votes"
    evm = sheet.total_row("evm")
    if evm is not None and complete and sums != evm.votes:
        return "every booth was read but the columns do not add up to the printed Total row"
    return None


def ingest_tg(db: Session, client: TgForm20Client, run, pool, *, code: str, year: int, entry,
              resume: bool = True) -> str:
    prefix = note_prefix(ocr.PARSER_NAME, ocr.PARSER_VERSION, code, year, entry.ac_number)

    def outcome(status: str, detail: str, note_status: str | None = None) -> str:
        if note_status:
            repo.record_fetch(db, source=SOURCE_TG_FORM20, url=entry.url, ok=status == "stored",
                              note=prefix + f"status={note_status}")
        db.commit()
        if status != "stored":
            run.warnings.append(f"{TG} {year} AC{entry.ac_number}: {detail}")
        return status

    dest = settings.raw_dir / "form20" / "tg" / str(year) / entry.url.rsplit("/", 1)[-1].replace("%20", " ")
    try:
        stored = download_file(lambda: client.get_pdf(entry.url), dest, entry.url, magic=b"%PDF", resume=resume)
    except Exception as exc:
        return outcome("failed", f"download failed: {str(exc)[:160]}")
    repo.record_fetch(db, source=SOURCE_TG_FORM20, url=entry.url, ok=True, bytes_received=stored.size_bytes,
                      checksum=stored.sha256, local_path=str(stored.path))
    sheet = read_sheet(stored.path, pool)
    eci = eci_candidates(db, year, entry.ac_number)
    names = name_columns(sheet, eci) if len(eci) == sheet.candidate_columns else None
    why = gate(sheet, eci, names)
    read, expected = sheet.booths_read, sheet.booths_expected
    if why:
        # the latest reading decides: rows an earlier run stored for this sheet go too
        election = db.query(Election).filter(Election.state == TG, Election.election_year == year,
                                             Election.election_type == "VIDHAN_SABHA").first()
        if election is not None:
            repo.replace_results(db, election=election, ac_number=entry.ac_number, rows=[])
        return outcome("blocked", f"{why}; nothing stored ({len(read)} of {expected} booths reconciled)",
                       note_status=f"blocked:ocr booths={len(read)} of={expected}")

    election = repo.upsert_election(db, election_year=year, election_type="VIDHAN_SABHA", state=TG,
                                    source_url=FORM20_SOURCE_URLS[TG])
    ac_row = state_ac(db, TG, entry.ac_number)
    candidates = json.dumps(names, ensure_ascii=False) if names else None
    rows = []
    for r in read:
        num, suffix = ocr.ps_number(r.ps_label)
        rows.append(dict(
            ac_id=ac_row.id if ac_row else None, part_number=num,
            polling_station_name=f"[{r.ps_label}]" if suffix else None,
            candidate_name=None, party=None, votes=None,
            candidate_votes_json=json.dumps(r.votes), vote_sum_matches=True, candidates_json=candidates,
            total_valid_votes=r.total_valid, rejected_votes=r.rejected, nota_votes=r.nota,
            tendered_votes=r.tendered, total_votes=r.total,
            source=SOURCE_TG_FORM20, source_file=stored.path.name, source_url=entry.url,
            source_page=r.page, parser_version=f"{ocr.PARSER_NAME}/{ocr.PARSER_VERSION}",
            extraction_confidence=None))
    repo.replace_results(db, election=election, ac_number=entry.ac_number, rows=rows)
    for w in sheet.warnings:
        run.warnings.append(f"{TG} {year} AC{entry.ac_number}: {w}")
    if names is None:
        run.warnings.append(f"{TG} {year} AC{entry.ac_number}: candidate columns not named "
                            "(the printed Total rows were not read, or did not match the ECI report exactly)")
    if ac_row is None:
        run.warnings.append(f"{TG} {year} AC{entry.ac_number}: no unique current AC; rows stored unlinked")
    run.booths += len(rows)
    run.verified_rows += len(rows)
    return outcome("stored", f"{len(rows)} of {expected} booths",
                   note_status=f"stored booths={len(rows)} of={expected}")


def load_telangana(db, http, run, *, code, years, acs, resume, refresh):
    for year in years:
        if year not in OCR_YEARS:
            run.warnings.append(f"{TG} {year}: booth-level OCR is not attempted for this year (sample "
                                "sheets reconciled under 10% of rows); constituency results come from "
                                "the ECI report. Index: " + TG_FORM20_INDEXES[year])
            continue
    years = [y for y in years if y in OCR_YEARS]
    if not years:
        return
    done = processed(db, ocr.PARSER_NAME, ocr.PARSER_VERSION, code)
    client = TgForm20Client(http)
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")       # one Tesseract thread per worker process
    with ProcessPoolExecutor(_workers()) as pool:
        for year in years:
            try:
                entries = client.list_acs(year)
            except Exception as exc:
                run.tally["failed"] += 1
                run.warnings.append(f"{TG} {year} index: {str(exc)[:200]}")
                continue
            for entry in entries:
                if acs and entry.ac_number not in acs:
                    continue
                if not refresh and (year, entry.ac_number) in done:
                    run.tally["skipped"] += 1
                    continue
                try:
                    status = ingest_tg(db, client, run, pool, code=code, year=year, entry=entry, resume=resume)
                except Exception as exc:
                    db.rollback()
                    status = "failed"
                    run.warnings.append(f"{TG} {year} AC{entry.ac_number}: {str(exc)[:200]}")
                run.tally[status] += 1
                log.info("%s %s AC%s -> %s", TG, year, entry.ac_number, status)
