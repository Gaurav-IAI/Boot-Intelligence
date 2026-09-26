"""Form 20 booth results for states other than the booth state (Uttar Pradesh, Telangana).

Kept apart from the pipeline on purpose: `serve` runs every pipeline stage whose
data is missing, and these states have ~1,400 result files between them, so they
are loaded only on request (`python -m app results`). Every step is resumable: a
constituency-year already stored or blocked by the current parser version is not
fetched again.

Stored rows follow the Uttarakhand rules exactly: results are replaced only when a
sheet passes the quality gate, the AC is matched by number *inside its own state*,
and "Verified" is decided later, per row, by `form20_row_check`.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..config import settings
from ..database import repositories as repo
from ..database.models import AssemblyConstituency, District, SourceFetch, State
from ..extraction.parsers.form20_up_xls import PARSER_NAME as UP_PARSER
from ..extraction.parsers.form20_up_xls import PARSER_VERSION as UP_PARSER_VERSION
from ..extraction.parsers.form20_up_xls import parse_form20_up
from ..extraction.pdf.downloader import download_file
from ..http_client import XLS_MAGIC, XLSX_MAGIC, HttpClient
from ..sources.ceo_uttar_pradesh.client import SOURCE_UP_FORM20, UpForm20Client
from ..states import spec

log = logging.getLogger(__name__)

DELIM_CURRENT = "current"
FORM20_MIN_BOOTHS = 10          # same gate as the Uttarakhand pipeline
FORM20_MIN_SUM_CHECK = 0.9
FORM20_MIN_COVERAGE = 0.9
FORM20_MIN_DISTINCT = 0.75     # distinct booth numbers per row (auxiliary stations repeat a few)

UP = "Uttar Pradesh"
TG = "Telangana"
FORM20_SOURCE_URLS = {UP: "https://ceouttarpradesh.nic.in/rollpdf/form20.aspx",
                      TG: "https://ceotelangana.nic.in/Form20.html"}


@dataclass
class ResultsRun:
    state: str
    tally: Counter = field(default_factory=Counter)
    booths: int = 0
    verified_rows: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.state}: {self.tally['stored']} constituency-year(s) stored "
                f"({self.booths:,} booth rows, {self.verified_rows:,} pass the vote-sum check), "
                f"{self.tally['skipped']} already done, {self.tally['blocked']} blocked, "
                f"{self.tally['failed']} failed")


def state_ac(db: Session, state_name: str, ac_number: int) -> AssemblyConstituency | None:
    """The current-delimitation AC with this number in this state; None if absent or ambiguous.
    AC numbers repeat across states, so a number is never looked up outside its state."""
    rows = (db.query(AssemblyConstituency)
            .join(District, AssemblyConstituency.district_id == District.id)
            .join(State, District.state_id == State.id)
            .filter(State.state_name == state_name, AssemblyConstituency.ac_number == ac_number,
                    AssemblyConstituency.delimitation == DELIM_CURRENT).all())
    return rows[0] if len(rows) == 1 else None


def _state_code(db: Session, state_name: str) -> str:
    st = db.query(State).filter(State.state_name == state_name).first()
    if st is None:
        raise LookupError(f"{state_name} is not discovered yet — run `python -m app pipeline "
                          f"--states \"{state_name}\"` (or `serve`) first")
    return st.state_code


def note_prefix(parser: str, version: str, code: str, year: int, ac: int) -> str:
    return f"form20 parser={parser}/{version} state={code} year={year} ac={ac} "


def processed(db: Session, parser: str, version: str, code: str) -> dict[tuple[int, int], str]:
    """(year, ac) -> final status of the current parser version (stored wins over blocked)."""
    out: dict[tuple[int, int], str] = {}
    like = f"form20 parser={parser}/{version} state={code} year=%"
    for (note,) in db.query(SourceFetch.note).filter(SourceFetch.note.like(like)):
        m = re.search(r" year=(\d+) ac=(\d+) status=(\w+)", note or "")
        if m:
            key = (int(m.group(1)), int(m.group(2)))
            if out.get(key) != "stored":
                out[key] = m.group(3)
    return out


def ingest_up(db: Session, http: HttpClient, run: ResultsRun, *, code: str, year: int,
              entry, resume: bool = True) -> str:
    """One UP constituency-year: download its workbook, parse, gate, store."""
    ac_number = entry.ac_number
    prefix = note_prefix(UP_PARSER, UP_PARSER_VERSION, code, year, ac_number)

    def outcome(status: str, detail: str, note_status: str | None = None) -> str:
        if note_status:
            repo.record_fetch(db, source=SOURCE_UP_FORM20, url=entry.url, ok=status == "stored",
                              note=prefix + f"status={note_status}")
        db.commit()
        if status != "stored":
            run.warnings.append(f"{UP} {year} AC{ac_number}: {detail}")
        return status

    ext = ".xlsx" if entry.url.lower().endswith(".xlsx") else ".xls"
    dest = settings.raw_dir / "form20" / "up" / f"{year}_AC{ac_number}{ext}"
    try:
        stored = download_file(lambda: http.get_xls(entry.url), dest, entry.url,
                               magic=(XLS_MAGIC, XLSX_MAGIC), resume=resume)
    except Exception as exc:
        return outcome("failed", f"download failed: {str(exc)[:160]}")
    repo.record_fetch(db, source=SOURCE_UP_FORM20, url=entry.url, ok=True,
                      bytes_received=stored.size_bytes, checksum=stored.sha256,
                      local_path=str(stored.path))

    try:
        parsed = parse_form20_up(stored.path, ac_number, year)
    except Exception as exc:        # e.g. a password-encrypted workbook: the file itself is unreadable
        return outcome("blocked", f"workbook cannot be opened: {str(exc)[:120]}",
                       note_status="blocked:unreadable")
    n = len(parsed.booths)
    if (n < FORM20_MIN_BOOTHS or parsed.sum_check_rate < FORM20_MIN_SUM_CHECK
            or parsed.serial_coverage < FORM20_MIN_COVERAGE or parsed.distinct_share < FORM20_MIN_DISTINCT):
        return outcome("blocked",
                       f"sheet not read reliably: {n} booths, {parsed.sum_check_rate:.0%} pass the "
                       f"vote-sum check, {parsed.serial_coverage:.0%} serial coverage, "
                       f"{parsed.distinct_share:.0%} distinct booth numbers; nothing stored"
                       + (f" ({'; '.join(parsed.warnings)})" if parsed.warnings else ""),
                       note_status="blocked:layout")

    election = repo.upsert_election(db, election_year=year, election_type="VIDHAN_SABHA",
                                    state=UP, source_url=FORM20_SOURCE_URLS[UP])
    ac_row = state_ac(db, UP, ac_number)
    candidates = json.dumps([{"name": c.name, "party": c.party} for c in parsed.candidates],
                            ensure_ascii=False)
    rows = [dict(
        ac_id=ac_row.id if ac_row else None,
        part_number=b.part_number, polling_station_name=b.polling_station_name,
        candidate_name=None, party=None, votes=None,
        candidate_votes_json=json.dumps(b.candidate_votes), vote_sum_matches=b.sum_matches_total,
        candidates_json=candidates,
        total_valid_votes=b.total_valid_votes, rejected_votes=b.rejected_votes, nota_votes=b.nota_votes,
        tendered_votes=b.tendered_votes, total_votes=b.total_votes,
        source=SOURCE_UP_FORM20, source_file=stored.path.name, source_url=entry.url,
        source_page=b.source_page, parser_version=f"{UP_PARSER}/{UP_PARSER_VERSION}",
        extraction_confidence=b.confidence) for b in parsed.booths]
    repo.replace_results(db, election=election, ac_number=ac_number, rows=rows)
    for w in parsed.warnings:
        run.warnings.append(f"{UP} {year} AC{ac_number}: {w}")
    if ac_row is None:
        run.warnings.append(f"{UP} {year} AC{ac_number}: no unique current AC {ac_number} in {UP}; "
                            "rows stored unlinked")
    run.booths += n
    run.verified_rows += parsed.booths_with_matching_sum
    return outcome("stored", f"{n} booths", note_status=f"stored booths={n}")



def load_state_results(db: Session, http: HttpClient, state_name: str, *,
                       years: tuple[int, ...] | None = None, districts: list[str] | None = None,
                       acs: tuple[int, ...] | None = None, resume: bool = True,
                       refresh: bool = False) -> ResultsRun:
    """Load Form 20 results for one state. `refresh` re-processes constituency-years that
    are already stored or blocked; otherwise they are skipped."""
    sp = spec(state_name)
    if sp is None or not sp.result_sources:
        raise LookupError(f"no Form 20 adapter for {state_name!r}")
    years = tuple(years or sp.form20_years)
    unsupported = [y for y in years if y not in sp.form20_years]
    if unsupported:
        raise LookupError(f"{state_name} Form 20 years supported: {sp.form20_years}; not {unsupported} "
                          "(earlier years use the pre-2008 constituency numbers)")
    code = _state_code(db, state_name)
    run = ResultsRun(state_name)
    if state_name == UP:
        done = processed(db, UP_PARSER, UP_PARSER_VERSION, code)
        client = UpForm20Client(http)
        for year in years:
            try:
                entries = client.list_acs(year, districts)
            except Exception as exc:
                run.tally["failed"] += 1
                run.warnings.append(f"{UP} {year} index: {str(exc)[:200]}")
                continue
            for entry in sorted(entries, key=lambda e: e.ac_number or 0):
                if entry.ac_number is None or (acs and entry.ac_number not in acs):
                    continue
                if not refresh and (year, entry.ac_number) in done:
                    run.tally["skipped"] += 1
                    continue
                try:
                    status = ingest_up(db, http, run, code=code, year=year, entry=entry, resume=resume)
                except Exception as exc:
                    db.rollback()
                    status = "failed"
                    run.warnings.append(f"{UP} {year} AC{entry.ac_number}: {str(exc)[:200]}")
                run.tally[status] += 1
                log.info("%s %s AC%s -> %s", UP, year, entry.ac_number, status)
    elif state_name == TG:
        from .telangana_results import load_telangana
        load_telangana(db, http, run, code=code, years=years, acs=acs, resume=resume, refresh=refresh)
    return run
