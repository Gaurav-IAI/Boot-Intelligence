"""Current polling stations for Uttar Pradesh, from district polling-station lists.

Loaded on request (`python -m app stations`), like the UP Form 20 results. Every
district listed in the CEO's index is covered (see sources/ceo_uttar_pradesh/ps_lists.py
for which lists that means). For each (district, AC) the candidate PDFs on the district
page are tried — a district may post several annexures per AC — and the one that reads
best is kept. A list is stored only when it reads cleanly: every page's column header
found, at least 90% of station numbers present over the list's own range, and elector
counts on at least 90% of rows. Scanned lists are recorded as blocked (OCR required).

Stations are matched to the AC by number inside Uttar Pradesh only. A constituency split
across two districts gets each district's part of the list.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..config import settings
from ..database import repositories as repo
from ..database.models import AssemblyConstituency, District, SourceFetch, State
from ..extraction.parsers.ps_list_up import PARSER_NAME, PARSER_VERSION, parse_ps_list_up
from ..extraction.pdf.downloader import download_pdf
from ..http_client import HttpClient
from ..sources.ceo_uttar_pradesh.ps_lists import SOURCE_UP_PS_LIST, UpPsListClient
from .state_results import UP, state_ac

log = logging.getLogger(__name__)

EDITION_CURRENT = "SIR-2026"
MIN_COVERAGE = 0.9
MIN_ELECTOR_SHARE = 0.9
MAX_CANDIDATES = 3


@dataclass
class StationsRun:
    tally: Counter = field(default_factory=Counter)
    stations: int = 0
    electors: int = 0
    warnings: list[str] = field(default_factory=list)
    blocked_by: Counter = field(default_factory=Counter)

    def summary(self) -> str:
        why = ", ".join(f"{n} {k}" for k, n in self.blocked_by.most_common())
        return (f"{UP} polling stations: {self.tally['stored']} list(s) stored "
                f"({self.stations:,} stations, {self.electors:,} electors listed), "
                f"{self.tally['skipped']} already done, {self.tally['blocked']} blocked"
                f"{f' ({why})' if why else ''}, {self.tally['failed']} failed")


def _slug(district: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", district.lower()).strip("-")


def note_prefix(district: str, ac: int) -> str:
    return f"ps-list-up parser={PARSER_NAME}/{PARSER_VERSION} district={_slug(district)} ac={ac} "


def _done(db: Session) -> set[tuple[str, int]]:
    like = f"ps-list-up parser={PARSER_NAME}/{PARSER_VERSION} district=%"
    out = set()
    for (n,) in db.query(SourceFetch.note).filter(SourceFetch.note.like(like)):
        m = re.search(r" district=(\S+) ac=(\d+) status=(stored|blocked)", n or "")
        if m:
            out.add((m.group(1), int(m.group(2))))
    return out


def _district_acs(db: Session, district: str) -> set[int]:
    return {n for (n,) in db.query(AssemblyConstituency.ac_number)
            .join(District, AssemblyConstituency.district_id == District.id)
            .join(State, District.state_id == State.id)
            .filter(State.state_name == UP, District.district_name == district,
                    AssemblyConstituency.delimitation == "current")}


def _state_acs(db: Session) -> dict[int, list[str]]:
    """AC number -> its names (English, Hindi) across the state."""
    return {n: [x for x in (name, local) if x] for n, name, local in db.query(
                AssemblyConstituency.ac_number, AssemblyConstituency.ac_name, AssemblyConstituency.ac_name_local)
            .join(District, AssemblyConstituency.district_id == District.id)
            .join(State, District.state_id == State.id)
            .filter(State.state_name == UP, AssemblyConstituency.delimitation == "current")}


def _mostly_images(path) -> bool:
    """True when most pages carry almost no text (scans, perhaps behind a typed cover page)."""
    import pymupdf
    with pymupdf.open(path) as doc:
        sparse = sum(1 for page in doc if len(page.get_text().strip()) < 100)
        return sparse * 2 > doc.page_count


def _read(entry, stored) -> tuple[object | None, str]:
    """(parsed list, None) when it passes the gate, else (None, reason)."""
    if not stored.is_text_pdf:
        return None, "scanned"
    parsed = parse_ps_list_up(stored.path, entry.ac_number)
    if not parsed.rows and _mostly_images(stored.path):
        return None, "scanned"          # a typed cover page over scanned table pages
    n = len(parsed.rows)
    with_electors = sum(r.electors is not None for r in parsed.rows)
    headless = sum("header not found" in w for w in parsed.warnings)
    # counts either read on (nearly) every row, or absent from the text layer altogether (some
    # lists draw them as graphics): then the stations are kept without counts. Counts read on
    # only part of the rows mean the columns were not understood.
    counts_absent = with_electors <= 0.05 * n
    if (n < 10 or parsed.serial_coverage < MIN_COVERAGE or headless
            or not (with_electors >= MIN_ELECTOR_SHARE * n or counts_absent)):
        return parsed, (f"layout: {n} stations, {parsed.serial_coverage:.0%} coverage, {with_electors} with "
                        f"electors, {headless} page(s) without a header")
    if counts_absent:
        for r in parsed.rows:
            r.electors = None
    return parsed, ""


def ingest_ac(db: Session, http: HttpClient, run: StationsRun, district: str, ac_number: int,
              entries: list, *, resume: bool = True) -> str:
    prefix = note_prefix(district, ac_number)

    def outcome(status: str, detail: str, note: str | None, url: str) -> str:
        if note:
            repo.record_fetch(db, source=SOURCE_UP_PS_LIST, url=url, ok=status == "stored",
                              note=prefix + f"status={note}")
        db.commit()
        if status != "stored":
            run.warnings.append(f"{district} AC{ac_number}: {detail}")
            run.blocked_by[note.split(":", 1)[-1] if note else status] += 1
        return status

    ac = state_ac(db, UP, ac_number)
    if ac is None:
        return outcome("blocked", f"no unique current AC {ac_number} in {UP}", "blocked:no-ac", entries[0].url)
    best, reasons = None, []
    for k, entry in enumerate(entries[:MAX_CANDIDATES]):
        dest = settings.raw_dir / "ps_list" / "up" / _slug(district) / f"AC{ac_number}_{k}.pdf"
        try:
            stored = download_pdf(lambda: http.get_pdf(entry.url), dest, entry.url, resume=resume)
        except Exception as exc:
            reasons.append(f"download failed ({str(exc)[:80]})")
            continue
        repo.record_fetch(db, source=SOURCE_UP_PS_LIST, url=entry.url, ok=True, bytes_received=stored.size_bytes,
                          checksum=stored.sha256, local_path=str(stored.path))
        parsed, why = _read(entry, stored)
        if why:
            reasons.append(why)
            continue
        score = (sum(r.electors is not None for r in parsed.rows), len(parsed.rows))
        if best is None or score > best[0]:
            best = (score, entry, parsed)
    if best is None:
        if reasons and all(r == "scanned" for r in reasons):
            return outcome("blocked", "scanned PDF; OCR required", "blocked:scanned", entries[0].url)
        if reasons and all(r.startswith("download failed") for r in reasons):
            return outcome("failed", "; ".join(reasons), None, entries[0].url)
        return outcome("blocked", "list not read reliably: " + "; ".join(reasons), "blocked:layout", entries[0].url)

    _, entry, parsed = best
    for r in parsed.rows:
        repo.upsert_polling_station(
            db, ac=ac, part_number=r.number, edition=EDITION_CURRENT, source=SOURCE_UP_PS_LIST,
            source_url=entry.url, polling_station_number=r.number,
            polling_station_name=r.building, polling_station_name_local=r.building,
            polling_station_address=r.locality, area_description=r.area,
            elector_count=r.electors)
    n = len(parsed.rows)
    with_electors = sum(r.electors is not None for r in parsed.rows)
    listed = sum(r.electors or 0 for r in parsed.rows)
    for w in parsed.warnings:
        run.warnings.append(f"{district} AC{ac_number}: {w}")
    if parsed.printed_total is not None and parsed.printed_total != listed:
        run.warnings.append(f"{district} AC{ac_number}: station electors sum to {listed:,} "
                            f"({n - with_electors} withheld) but the list prints a total of "
                            f"{parsed.printed_total:,}")
    if with_electors == 0:
        run.warnings.append(f"{district} AC{ac_number}: elector counts are not in the PDF's text "
                            "(drawn as graphics); stations stored without counts")
    low = sum(r.confidence < 0.9 for r in parsed.rows)
    if low:
        run.warnings.append(f"{district} AC{ac_number}: {low} station name(s) only partly decoded")
    run.stations += n
    run.electors += listed
    return outcome("stored", f"{n} stations", f"stored stations={n}", entry.url)


def load_up_polling_stations(db: Session, http: HttpClient, *, districts: list[str] | None = None,
                             acs: tuple[int, ...] | None = None, resume: bool = True,
                             refresh: bool = False) -> StationsRun:
    client = UpPsListClient(http)
    available = client.districts()
    names = districts or available
    unknown = [d for d in names if d not in available]
    if unknown:
        raise LookupError(f"no polling-station page for {', '.join(unknown)}; "
                          f"available: {', '.join(available)}")
    run = StationsRun()
    done = set() if refresh else _done(db)
    state_acs = _state_acs(db)
    for district in names:
        try:
            entries = client.list_acs(district, _district_acs(db, district), state_acs)
        except Exception as exc:
            run.tally["failed"] += 1
            run.warnings.append(f"{district} page: {str(exc)[:200]}")
            continue
        if not entries:
            run.tally["blocked"] += 1
            run.blocked_by["no-lists"] += 1
            run.warnings.append(f"{district}: no polling-station PDFs recognised on {client.page(district)[0]}")
            continue
        by_ac: dict[int, list] = {}
        for e in entries:
            by_ac.setdefault(e.ac_number, []).append(e)
        for number, group in sorted(by_ac.items()):
            if acs and number not in acs:
                continue
            if (_slug(district), number) in done:
                run.tally["skipped"] += 1
                continue
            try:
                status = ingest_ac(db, http, run, district, number, group, resume=resume)
            except Exception as exc:
                db.rollback()
                status = "failed"
                run.warnings.append(f"{district} AC{number}: {str(exc)[:200]}")
            run.tally[status] += 1
            log.info("%s AC%s -> %s", district, number, status)
    return run
