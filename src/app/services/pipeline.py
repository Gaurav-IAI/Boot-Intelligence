"""The POC pipeline: discovery -> download -> extract -> validate -> store.

Every step is resumable and idempotent. Nothing here touches a CAPTCHA-gated or
authenticated endpoint; the sources used are listed in config/sources.yaml.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..database import repositories as repo
from ..extraction.ocr.engine import ocr_available
from ..extraction.parsers.form20_2012 import PARSER_NAME as F20_PARSER
from ..extraction.parsers.form20_2012 import PARSER_VERSION as F20_PARSER_VERSION
from ..extraction.parsers.form20_2012 import parse_form20_2012
from ..extraction.parsers.ps_list_2026 import PARSER_NAME as PS_PARSER
from ..extraction.parsers.ps_list_2026 import PARSER_VERSION as PS_PARSER_VERSION
from ..extraction.parsers.ps_list_2026 import parse_ps_list_pdf
from ..extraction.parsers.roll_2003 import PARSER_NAME as ROLL_PARSER
from ..extraction.parsers.roll_2003 import parse_roll_pdf
from ..extraction.pdf.downloader import download_pdf
from ..http_client import HttpClient
from ..sources.ceo_uttarakhand.client import (
    SOURCE_FORM20, SOURCE_LEGACY_2003, SOURCE_PS_LIST, SOURCE_SIR_PARTS,
    Form20Client, LegacyRoll2003Client, SirPartsClient,
)
from ..sources.eci_api.client import SOURCE as ECI_SOURCE
from ..sources.eci_api.client import EciApiClient
from ..states import BOOTH_STATE, STATE_NAMES, booth_state_id
from .validation import normalize_age, normalize_gender, validate_rows

log = logging.getLogger(__name__)

EDITION_CURRENT = "SIR-2026"
EDITION_2003 = "ROLL-2003"
DELIM_CURRENT = "current"
DELIM_2003 = "2003"


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str
    count: int = 0


@dataclass
class PipelineReport:
    steps: list[StepResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, count: int = 0) -> None:
        self.steps.append(StepResult(name, ok, detail, count))
        log.info("[%s] %s — %s (%d)", "ok" if ok else "FAIL", name, detail, count)


# --------------------------------------------------------------------------
# Discovery (current delimitation, from the ECI gateway)
# --------------------------------------------------------------------------
def discover_hierarchy(db: Session, http: HttpClient, report: PipelineReport,
                       *, state_name: str = BOOTH_STATE, district_name: str | None = None,
                       limit_acs: int | None = None) -> dict:
    eci = EciApiClient(http)

    state_ref = eci.find_state(state_name)
    state = repo.upsert_state(
        db, state_code=state_ref.state_code, state_name=state_ref.state_name,
        state_name_local=state_ref.state_name_local, state_type=state_ref.state_type,
        external_id=state_ref.external_id, source=ECI_SOURCE,
        source_url=eci.states_url())
    repo.record_fetch(db, source=ECI_SOURCE, url=eci.states_url(), ok=True,
                      note=f"resolved {state_name} -> {state_ref.state_code}")
    report.add("discover-state", True,
               f"{state_ref.state_name} = {state_ref.state_code} "
               f"(discovered dynamically, not hard-coded)", 1)

    d_url = eci.districts_url(state.state_code)
    districts = eci.list_districts(state.state_code)
    repo.record_fetch(db, source=ECI_SOURCE, url=d_url, ok=True)
    stored_d = [repo.upsert_district(
        db, state=state, district_code=d.district_code,
        district_number=d.district_number, district_name=d.district_name,
        district_name_local=d.district_name_local, source=ECI_SOURCE,
        source_url=d_url) for d in districts]
    report.add("discover-districts", True,
               f"{len(stored_d)} districts for {state.state_code}", len(stored_d))

    targets = stored_d
    if district_name:
        targets = [d for d in stored_d
                   if d.district_name.casefold() == district_name.casefold()]
        if not targets:
            raise LookupError(
                f"district {district_name!r} not found. Available: "
                f"{', '.join(sorted(d.district_name for d in stored_d))}")

    all_acs = []
    for d in targets:
        a_url = eci.acs_url(d.district_code)
        acs = eci.list_acs(d.district_code)
        repo.record_fetch(db, source=ECI_SOURCE, url=a_url, ok=True)
        for a in acs:
            all_acs.append(repo.upsert_ac(
                db, district=d, ac_number=a.ac_number, ac_name=a.ac_name,
                ac_name_local=a.ac_name_local, official_code=a.official_code,
                category=a.category, pc_number=a.pc_number,
                delimitation=DELIM_CURRENT, source=ECI_SOURCE, source_url=a_url))
    if limit_acs:
        all_acs = all_acs[:limit_acs]
    report.add("discover-acs", True,
               f"{len(all_acs)} ACs across {len(targets)} district(s)", len(all_acs))
    db.commit()
    return {"state": state, "districts": stored_d, "acs": all_acs}


def discover_parts(db: Session, http: HttpClient, report: PipelineReport | None,
                   ac, *, limit: int | None = None) -> list:
    """Current polling stations for an AC, from CEO Uttarakhand's SIR 2026 list."""
    client = SirPartsClient(http)
    url = client.parts_url(ac.ac_number)
    parts = client.list_parts(ac.ac_number)
    # The count is noted so an AC with no published parts is not re-queried on every start.
    repo.record_fetch(db, source=SOURCE_SIR_PARTS, url=url, ok=True,
                      note=f"sir-parts ac={ac.ac_number} parts={len(parts)}")
    if limit:
        parts = parts[:limit]
    stations = [repo.upsert_polling_station(
        db, ac=ac, part_number=p.part_number, edition=EDITION_CURRENT,
        source=SOURCE_SIR_PARTS, source_url=url,
        part_name=p.part_name, polling_station_number=p.part_number,
        polling_station_name=p.part_name) for p in parts]
    if report is not None:
        report.add("discover-parts", True,
                   f"AC {ac.ac_number} {ac.ac_name}: {len(stations)} polling stations "
                   f"(SIR 2026)", len(stations))
    db.commit()
    return stations


PS_LIST_MIN_COVERAGE = 0.9     # share of the AC's polling stations the parse must find
PS_LIST_MAX_MISSING = 0.1      # share of parsed stations allowed to lack building or areas


def ps_list_note_prefix(ac_number: int) -> str:
    return f"ps-list parser={PS_PARSER}/{PS_PARSER_VERSION} ac={ac_number} "


def ps_list_parse_note(ac_number: int, stations: int) -> str:
    return ps_list_note_prefix(ac_number) + f"stations={stations}"


def ps_list_blocked_note(ac_number: int, reason: str) -> str:
    return ps_list_note_prefix(ac_number) + f"blocked={reason}"


def enrich_from_ps_list(db: Session, http: HttpClient, report: PipelineReport | None,
                        ac, *, resume: bool = True) -> dict:
    """Add station building names and areas from the official PS List 2026 PDF.

    Returns {"status": "stored" | "blocked" | "failed", "detail", "stations"}. Nothing
    is written unless the parse passes the quality gate (>= 90% of the AC's stations
    found, <= 10% missing building or areas). Blocked outcomes — unpublished list,
    scanned PDF, unrecognised layout — are recorded with the parser version, so they
    are retried only when the parser changes. Transient download failures are not recorded.
    """
    from sqlalchemy import func

    from ..database.models import PollingStation

    client = SirPartsClient(http)
    url = client.ps_list_pdf_url(ac.ac_number)
    dest = settings.raw_dir / "ps_list_2026" / f"{ac.ac_number}.pdf"

    def outcome(status: str, detail: str, stations: int = 0, note: str | None = None) -> dict:
        if note:
            repo.record_fetch(db, source=SOURCE_PS_LIST, url=url, ok=status == "stored", note=note)
        db.commit()
        if report is not None and status != "stored":
            report.warnings.append(f"ps-list AC{ac.ac_number}: {detail}")
        return {"status": status, "detail": detail, "stations": stations}

    try:
        stored = download_pdf(lambda: client.fetch_ps_list_pdf(ac.ac_number),
                              dest, url, resume=resume)
    except Exception as exc:
        if "404" in str(exc):
            return outcome("blocked", "Polling Station List 2026 not published (HTTP 404)",
                           note=ps_list_blocked_note(ac.ac_number, "not-published"))
        return outcome("failed", f"download failed: {str(exc)[:160]}")
    repo.record_fetch(db, source=SOURCE_PS_LIST, url=url, ok=True,
                      bytes_received=stored.size_bytes, checksum=stored.sha256,
                      local_path=str(stored.path))
    if stored.is_text_pdf is False:
        return outcome("blocked", f"scanned PDF ({stored.page_count} pages); needs OCR table reading",
                       note=ps_list_blocked_note(ac.ac_number, "scanned"))

    parsed = parse_ps_list_pdf(stored.path, ac.ac_number)
    expected = db.query(func.count(PollingStation.id)).filter(
        PollingStation.ac_id == ac.id, PollingStation.edition == EDITION_CURRENT).scalar() or 0
    rows = parsed.stations
    missing = sum(1 for r in rows if not r.station_name or not r.areas)
    coverage = len(rows) / expected if expected else 0.0
    if not rows or coverage < PS_LIST_MIN_COVERAGE or missing > PS_LIST_MAX_MISSING * len(rows):
        return outcome("blocked",
                       f"layout not recognised reliably: {len(rows)} of {expected} stations parsed, "
                       f"{missing} missing building or areas; nothing stored",
                       note=ps_list_blocked_note(ac.ac_number, "layout"))
    for row in rows:
        repo.upsert_polling_station(
            db, ac=ac, part_number=row.part_number, edition=EDITION_CURRENT,
            source=SOURCE_PS_LIST, source_url=url,
            polling_station_name_local=row.station_full,
            area_description=row.areas)
    return outcome("stored", f"{len(rows)} stations enriched ({stored.page_count} pages)", len(rows),
                   note=ps_list_parse_note(ac.ac_number, len(rows)))


def ps_list_2024_note(ac_number: int, outcome: str) -> str:
    from ..extraction.ocr.ps_list_scan import PARSER_NAME, PARSER_VERSION
    return f"ps-list-2024 parser={PARSER_NAME}/{PARSER_VERSION} ac={ac_number} {outcome}"


def ps_list_2024_path(ac_number: int) -> Path:
    return settings.processed_dir / "ocr" / f"ps_list_2024_AC{ac_number}.json"


PS_2024_MIN_SERIALS = 10       # rows with a sequence-confirmed serial needed to keep the OCR output


def ocr_ps_list_2024(db: Session, http: HttpClient | None, report: PipelineReport | None,
                     ac_number: int, *, resume: bool = True, http_factory=None) -> dict:
    """Download the scanned Polling Station List 2024 for an AC and read it by OCR.

    The rows are written to data/processed/ocr/ps_list_2024_AC{n}.json, where the
    mapping check uses them as a bridge between 2025 and current part numbers.
    Returns {"status": "stored" | "blocked" | "failed", "detail", "rows"}. A missing
    OCR engine is a failure (not recorded), so the stage retries once it is installed.
    """
    from ..extraction.ocr.ps_list_scan import PARSER_VERSION, read_scanned_ps_list
    from ..sources.ceo_uttarakhand.client import SOURCE_PS_LIST_2024

    out_path = ps_list_2024_path(ac_number)
    url = SirPartsClient(http).ps_list_2024_pdf_url(ac_number)

    def outcome(status: str, detail: str, rows: int = 0, note: str | None = None) -> dict:
        if note:
            repo.record_fetch(db, source=SOURCE_PS_LIST_2024, url=url, ok=status == "stored",
                              note=ps_list_2024_note(ac_number, note))
        db.commit()
        if report is not None and status != "stored":
            report.warnings.append(f"ps-list-2024 AC{ac_number}: {detail}")
        return {"status": status, "detail": detail, "rows": rows}

    if resume and out_path.exists():
        cached = json.loads(out_path.read_text(encoding="utf-8"))
        if cached.get("parser_version") == PARSER_VERSION:
            n = sum(1 for r in cached.get("rows", []) if r.get("serial") is not None)
            return outcome("stored", f"reused {out_path.name}", n, note=f"status=stored rows={n}")
    ok, reason = ocr_available()
    if not ok:
        return outcome("failed", f"OCR engine unavailable: {reason}")
    if http is None:
        http = http_factory()
    client = SirPartsClient(http)
    dest = settings.raw_dir / "ps_list_2024" / f"{ac_number}.pdf"
    try:
        stored = download_pdf(lambda: client.fetch_ps_list_2024_pdf(ac_number), dest, url, resume=resume)
    except Exception as exc:
        if "404" in str(exc):
            return outcome("blocked", "Polling Station List 2024 not published (HTTP 404)",
                           note="status=blocked:not-published")
        return outcome("failed", f"download failed: {str(exc)[:160]}")
    repo.record_fetch(db, source=SOURCE_PS_LIST_2024, url=url, ok=True, bytes_received=stored.size_bytes,
                      checksum=stored.sha256, local_path=str(stored.path))
    result = read_scanned_ps_list(stored.path)
    n = sum(1 for r in result["rows"] if r["serial"] is not None)
    if n < PS_2024_MIN_SERIALS:
        return outcome("blocked", f"table not readable by OCR: {n} rows with a confirmed serial",
                       note="status=blocked:unreadable")
    result.update(ac_number=ac_number, source_url=url, sha256=stored.sha256, local_path=str(stored.path))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return outcome("stored", f"{n} rows read ({result['pages_read']} pages)", n,
                   note=f"status=stored rows={n}")


# --------------------------------------------------------------------------
# Electoral roll ingestion (2003 legacy roll — the CAPTCHA-free real roll)
# --------------------------------------------------------------------------
def ingest_legacy_rolls(db: Session, http: HttpClient, report: PipelineReport,
                        *, district_name_hi: str, ac_name_hi: str,
                        parts: list[int] | None = None, limit: int = 5,
                        resume: bool = True) -> list:
    """Download and parse 2003 roll PDFs for a few parts of one AC."""
    from ..database.models import District, State

    client = LegacyRoll2003Client(http)
    st = db.query(State).filter(State.state_name == BOOTH_STATE).order_by(State.id).first()
    if st is None:
        raise LookupError(f"{BOOTH_STATE} not yet discovered — run the hierarchy stage first")
    districts_2003 = client.list_districts()
    match = next((d for d in districts_2003 if d.name.strip() == district_name_hi.strip()), None)
    if match is None:
        raise LookupError(
            f"district {district_name_hi!r} not in the 2003 roll. Available: "
            f"{', '.join(d.name for d in districts_2003)}")

    acs = client.list_acs(match.name)
    ac_match = next((a for a in acs if a.name.strip() == ac_name_hi.strip()), None)
    if ac_match is None:
        raise LookupError(
            f"AC {ac_name_hi!r} not in 2003 district {match.name!r}. Available: "
            f"{', '.join(a.name for a in acs)}")

    # The 2003 district list is its own delimitation; attach it to the matching
    # current district row where the name agrees, else create a 2003 district.
    district = db.query(District).filter(
        District.state_id == st.id, District.district_name_local == match.name).one_or_none()
    if district is None:
        district = repo.upsert_district(
            db, state=st, district_code=f"UK2003-{match.number}",
            district_number=match.number, district_name=match.name,
            district_name_local=match.name, source=SOURCE_LEGACY_2003,
            source_url=f"{client.__class__.__name__}/district-names")

    ac = repo.upsert_ac(
        db, district=district, ac_number=ac_match.number or 0,
        ac_name=ac_match.name, ac_name_local=ac_match.name,
        delimitation=DELIM_2003, source=SOURCE_LEGACY_2003)
    db.commit()

    all_parts = client.list_parts(ac_match.name)
    wanted = parts or [p.number for p in all_parts if p.number][:limit]
    report.add("legacy-parts", True,
               f"AC {ac_match.number} {ac_match.name} (2003): {len(all_parts)} parts "
               f"published; ingesting {len(wanted)}", len(all_parts))

    rolls = []
    for part_no in wanted:
        pref = next((p for p in all_parts if p.number == part_no), None)
        url = client.roll_pdf_url(ac_match.number, ac_match.name, part_no)
        dest = (settings.raw_dir / "roll2003" /
                f"AC{ac_match.number:02d}" / f"P{part_no:04d}.pdf")
        try:
            stored = download_pdf(
                lambda: client.fetch_roll_pdf(ac_match.number, ac_match.name, part_no)[0],
                dest, url, resume=resume)
        except Exception as exc:
            report.add(f"roll-download-part-{part_no}", False, str(exc)[:200])
            repo.record_fetch(db, source=SOURCE_LEGACY_2003, url=url, ok=False,
                              note=str(exc)[:400])
            continue
        repo.record_fetch(db, source=SOURCE_LEGACY_2003, url=url, ok=True,
                          bytes_received=stored.size_bytes, checksum=stored.sha256,
                          local_path=str(stored.path))

        if stored.is_text_pdf is False:
            # A scanned roll has no text layer: record it as blocked instead of
            # storing an "extracted" roll with zero electors.
            ocr_ok, ocr_why = ocr_available()
            why = (f"OCR engine available ({ocr_why}) but no OCR roll parser is implemented"
                   if ocr_ok else f"OCR unavailable: {ocr_why}")
            station = repo.upsert_polling_station(
                db, ac=ac, part_number=part_no, edition=EDITION_2003,
                source=SOURCE_LEGACY_2003, source_url=url,
                part_name=(pref.name if pref else None))
            repo.upsert_roll(
                db, station=station, roll_year=2003, roll_type="Final", language="HIN",
                source=SOURCE_LEGACY_2003, pdf_url=url, local_file_path=str(stored.path),
                download_status="downloaded", extraction_status="blocked_scanned_pdf",
                checksum_sha256=stored.sha256, file_size_bytes=stored.size_bytes,
                page_count=stored.page_count, is_text_pdf=False,
                error_message=f"scanned PDF with no text layer; {why}")
            db.commit()
            report.add(f"roll-part-{part_no}", False, f"scanned PDF, not extracted — {why}"[:300])
            continue

        parsed = parse_roll_pdf(stored.path)
        hdr = parsed.header
        station = repo.upsert_polling_station(
            db, ac=ac, part_number=part_no, edition=EDITION_2003,
            source=SOURCE_LEGACY_2003, source_url=url,
            polling_station_number=hdr.polling_station_number,
            polling_station_name=hdr.polling_station_name,
            polling_station_name_local=hdr.polling_station_name,
            part_name=(pref.name if pref else None),
            area_description=hdr.areas)

        vrep = validate_rows(parsed.electors, expected_part=hdr.part_number)
        roll = repo.upsert_roll(
            db, station=station, roll_year=hdr.roll_year or 2003,
            roll_type="Final", language="HIN", source=SOURCE_LEGACY_2003,
            qualifying_date=hdr.qualifying_date, pdf_url=url,
            local_file_path=str(stored.path), download_status="downloaded",
            extraction_status="extracted", extraction_method="text",
            checksum_sha256=stored.sha256, file_size_bytes=stored.size_bytes,
            page_count=stored.page_count, is_text_pdf=stored.is_text_pdf,
            official_elector_count=vrep.expected_count,
            official_count_basis=vrep.expected_basis,
            extracted_elector_count=len(parsed.electors),
            parser_version=f"{ROLL_PARSER}/{settings.parser_version}")

        rows = []
        for e in parsed.electors:
            rows.append(dict(
                part_number=hdr.part_number, serial_number=e.serial_number,
                elector_name=e.elector_name, elector_name_raw=e.elector_name_raw,
                relative_name=e.relative_name, relative_name_raw=e.relative_name_raw,
                relative_type=e.relative_type,
                age=normalize_age(e.age), gender=normalize_gender(e.gender),
                epic_number=e.epic_number, house_number=e.house_number,
                source=SOURCE_LEGACY_2003, source_url=url,
                source_document=stored.path.name, source_page=e.source_page,
                raw_text=e.raw_text,
                parser_version=f"{ROLL_PARSER}/{settings.parser_version}",
                extraction_method="text", extraction_confidence=e.confidence,
                is_valid=getattr(e, "is_valid", True),
                validation_notes=getattr(e, "validation_notes", None),
                duplicate_kind=getattr(e, "duplicate_kind", None),
            ))
        n = repo.replace_electors(db, roll=roll, rows=rows)
        db.commit()
        rolls.append(roll)
        report.add(f"roll-part-{part_no}", True,
                   f"{n} electors extracted; expected {vrep.expected_count} "
                   f"({vrep.expected_basis}); {len(vrep.serial_gaps)} serial gaps; "
                   f"{vrep.duplicates_exact + vrep.duplicates_fuzzy} duplicates flagged", n)
        for w in parsed.warnings:
            report.warnings.append(f"part {part_no}: {w}")
    return rolls


# --------------------------------------------------------------------------
# Official part mapping 2003 -> 2025
# --------------------------------------------------------------------------
def village_search_terms(area_description: str | None) -> list[str]:
    """Village names printed in a 2003 roll header, ready for the village search.

    "1 आमवाला तरला, 2 आमवाला शा˓ी पुरम, 3 तपोवन" -> ["आमवाला तरला", "तपोवन"]. Names
    carrying undecoded legacy-font glyphs are skipped rather than searched garbled.
    """
    terms: list[str] = []
    for raw in re.split(r"[,،]", area_description or ""):
        name = re.sub(r"^[\s\d०-९.\-]+", "", raw).strip()
        if not name or re.search(r"[^ऀ-ॿ\s]", name):
            continue
        if name not in terms:
            terms.append(name)
    return terms


def mapping_attempt_note(ac_name_hi: str, part_no: int, rows: int) -> str:
    return f"part-mapping ac={ac_name_hi.strip()} part={part_no} rows={rows}"


def ingest_part_mapping(db: Session, http: HttpClient, report: PipelineReport,
                        *, ac_name_hi: str, parts: list[int],
                        max_villages_per_part: int = 12) -> int:
    from ..database.models import PollingStation

    client = LegacyRoll2003Client(http)
    all_parts = client.list_parts(ac_name_hi)
    legacy = _legacy_ac(db, ac_name_hi)
    headers = ({s.part_number: s.area_description for s in db.query(PollingStation).filter(
        PollingStation.ac_id == legacy.id, PollingStation.edition == EDITION_2003)}
        if legacy else {})
    stored = 0
    unmapped: list[int] = []
    for part_no in parts:
        pref = next((p for p in all_parts if p.number == part_no), None)
        # Search by the villages printed in this part's own roll header, then by the
        # part name. The part name alone misses parts whose name is spelled
        # differently from their villages (2003 Part 8: "डाडा" vs "डांडा").
        terms = village_search_terms(headers.get(part_no)) + ([pref.name] if pref else [])
        villages: list[str] = []
        for term in terms:
            try:
                found = client.list_villages(term)[:max_villages_per_part]
            except Exception as exc:
                report.warnings.append(f"village lookup failed for {term!r}: {exc}")
                continue
            villages += [v for v in found if v not in villages]
        n_part, seen = 0, set()
        for v in villages:
            try:
                details = client.village_details(v)
            except Exception as exc:
                report.warnings.append(f"village-details failed for {v!r}: {exc}")
                continue
            for m in details:
                # The village search is statewide: keep only rows for THIS 2003 AC and part.
                same_ac = (m.ac2003_number == legacy.ac_number) if (legacy and m.ac2003_number) \
                    else (m.ac2003_name or "").strip() == ac_name_hi.strip()
                if m.part2003_number != part_no or not same_ac:
                    continue
                key = (m.village, m.part2025_number)
                if key in seen:
                    continue
                seen.add(key)
                n_part += 1
                repo.upsert_part_mapping(
                    db, from_edition=EDITION_2003,
                    from_ac_number=m.ac2003_number or 0,
                    from_ac_name=m.ac2003_name,
                    from_part_number=m.part2003_number or 0,
                    from_part_name=m.part2003_name,
                    to_edition="ROLL-2025", to_ac_number=m.ac2025_number,
                    to_ac_name=m.ac2025_name, to_part_number=m.part2025_number,
                    to_part_name=m.part2025_name, area_name=m.village,
                    mapping_method="official_ceo_uk_village_mapping",
                    confidence=0.95, source=SOURCE_LEGACY_2003, state_id=booth_state_id(db),
                    source_url=f"{client.__class__.__name__}/village-details")
                stored += 1
        # Record the attempt so `serve` does not re-query a part the source has no mapping for.
        repo.record_fetch(db, source=SOURCE_LEGACY_2003, url="uklegacydata/village-details",
                          ok=True, note=mapping_attempt_note(ac_name_hi, part_no, n_part))
        if not n_part:
            unmapped.append(part_no)
    db.commit()
    report.add("part-mapping", True,
               f"{stored} official 2003->2025 village-level mappings stored "
               f"(one 2003 part can map to several 2025 parts)", stored)
    if unmapped:
        report.warnings.append(f"no official village mapping found for 2003 part(s) {unmapped}")
    return stored


# --------------------------------------------------------------------------
# Form 20 historical results
# --------------------------------------------------------------------------
FORM20_MIN_BOOTHS = 10
FORM20_MIN_SUM_CHECK = 0.9     # share of booths whose candidate votes sum to total valid votes
FORM20_MIN_COVERAGE = 0.9      # booths found / highest serial


def form20_note_prefix(year: int, ac_number: int) -> str:
    return f"form20 parser={F20_PARSER}/{F20_PARSER_VERSION} year={year} ac={ac_number} "


def ingest_form20(db: Session, http: HttpClient, report: PipelineReport | None,
                  *, year: int, ac_number: int, resume: bool = True,
                  entries: list | None = None) -> dict:
    """Booth results for one constituency-year from its Form 20.

    Returns {"status": "stored" | "blocked" | "failed", "detail", "booths"}. Results are
    replaced only when the parse passes the quality gate (>= 10 booths, >= 90% passing
    the document's own vote-sum check, >= 90% serial coverage). Otherwise the
    constituency-year is recorded as blocked and previously stored rows are untouched.
    Outcomes carry the parser version, so an improved parser re-processes them.
    """
    url: str | None = None

    def outcome(status: str, detail: str, booths: int = 0, note_status: str | None = None) -> dict:
        if note_status:
            repo.record_fetch(db, source=SOURCE_FORM20, url=url or f"form20/{year}", ok=status == "stored",
                              note=form20_note_prefix(year, ac_number) + f"status={note_status}")
        db.commit()
        if report is not None and status != "stored":
            report.warnings.append(f"form20 {year} AC{ac_number}: {detail}")
        return {"status": status, "detail": detail, "booths": booths}

    if entries is None:
        entries = Form20Client(http).list_acs(year)
    entry = next((e for e in entries if e.ac_number == ac_number), None)
    if entry is None:
        return outcome("blocked", f"not listed in the {year} Form 20 index", note_status="blocked:not-listed")
    url = entry.url
    dest = settings.raw_dir / "form20" / f"{year}_AC{ac_number}.pdf"
    try:
        stored = download_pdf(lambda: http.get_pdf(entry.url), dest, entry.url, resume=resume)
    except Exception as exc:
        return outcome("failed", f"download failed: {str(exc)[:160]}")
    repo.record_fetch(db, source=SOURCE_FORM20, url=entry.url, ok=True,
                      bytes_received=stored.size_bytes, checksum=stored.sha256,
                      local_path=str(stored.path))
    if not stored.is_text_pdf:
        return outcome("blocked", f"scanned PDF ({stored.page_count} pages); OCR required",
                       note_status="blocked:scanned")
    if year != 2012:
        return outcome("blocked", "only the 2012 layout has a verified parser "
                                  "(2017's embedded OCR text layer is corrupted)",
                       note_status="blocked:no-parser")

    parsed = parse_form20_2012(stored.path, ac_number)
    n = len(parsed.booths)
    if (n < FORM20_MIN_BOOTHS or parsed.sum_check_rate < FORM20_MIN_SUM_CHECK
            or parsed.serial_coverage < FORM20_MIN_COVERAGE):
        return outcome("blocked",
                       f"layout not recognised reliably: {n} booths, {parsed.sum_check_rate:.0%} pass "
                       f"the vote-sum check, {parsed.serial_coverage:.0%} serial coverage; nothing stored",
                       note_status="blocked:layout")

    election = repo.upsert_election(db, election_year=year, election_type="VIDHAN_SABHA",
                                    state=BOOTH_STATE, source_url=entry.url)
    ac_row = form20_ac(db, ac_number)
    rows = [dict(
        ac_id=ac_row.id if ac_row else None,
        part_number=b.part_number, polling_station_name=b.polling_station_name,
        candidate_name=None, party=None, votes=None,
        candidate_votes_json=json.dumps(b.candidate_votes),
        vote_sum_matches=b.sum_matches_total,
        total_valid_votes=b.total_valid_votes, rejected_votes=b.rejected_votes,
        tendered_votes=b.tendered_votes, total_votes=b.total_votes,
        source=SOURCE_FORM20, source_file=stored.path.name,
        source_url=entry.url, source_page=b.source_page,
        parser_version=f"{F20_PARSER}/{F20_PARSER_VERSION}",
        extraction_confidence=b.confidence) for b in parsed.booths]
    repo.replace_results(db, election=election, ac_number=ac_number, rows=rows)
    return outcome("stored", f"{n} booth results, {parsed.booths_with_matching_sum} pass the vote-sum check",
                   n, note_status=f"stored booths={n}")


# --------------------------------------------------------------------------
# One-command orchestration: plan what is missing, run only that
# --------------------------------------------------------------------------
@dataclass
class PipelineConfig:
    """Scope of the POC. Defaults reproduce the dataset the dashboard demonstrates."""
    district: str = "Dehradun"
    ac_number: int = 19                       # current delimitation
    legacy_district_hi: str = "देहरादून"
    legacy_ac_hi: str = "राजपुर"               # AC 15 in the 2003 delimitation
    legacy_parts: list[int] | None = field(default_factory=lambda: [6, 7, 8, 9, 10])
    legacy_limit: int = 5                     # used only when legacy_parts is None
    form20_years: tuple[int, ...] = (2012,)   # 2017/2022 are attempted but blocked (corrupt / scan)
    station_acs: tuple[int, ...] | None = None  # current ACs to load polling stations for (None = all)
    form20_acs: tuple[int, ...] | None = None   # current ACs to load Form 20 results for (None = all)
    states: tuple[str, ...] = STATE_NAMES     # states whose districts and ACs are discovered from ECI
    all_districts: bool = True                # discover every district's ACs
    include_mapping: bool = True
    include_form20: bool = True
    resume: bool = True                       # reuse downloaded PDFs
    review_dir: Path | None = None            # default: data/processed/review


@dataclass
class StagePlan:
    name: str
    needed: bool
    reason: str
    items: list[int] = field(default_factory=list)
    mode: str = "ingest"                      # ingest | backfill | disabled | always


# Network stages run only when their data is missing; `verify` and `review` are
# offline and idempotent, so they run on every start.
PIPELINE_STAGES = ("hierarchy", "parts", "ps-list", "rolls", "mapping", "ps-list-2024",
                   "form20", "verify", "review")


def _booth_acs(db: Session):
    """Constituencies of the state with booth-level sources. AC numbers repeat across
    states, so every booth-level stage looks constituencies up through this query."""
    from ..database.models import AssemblyConstituency as AC
    from ..database.models import District
    return (db.query(AC).join(District, AC.district_id == District.id)
            .filter(District.state_id == booth_state_id(db)))


def _target_acs(db: Session, numbers: tuple[int, ...] | None) -> list:
    from ..database.models import AssemblyConstituency as AC
    q = _booth_acs(db).filter(AC.delimitation == DELIM_CURRENT)
    if numbers:
        q = q.filter(AC.ac_number.in_(numbers))
    return q.order_by(AC.ac_number).all()


def _form20_counts(db: Session, year: int, ac_number: int) -> tuple[int, int]:
    from sqlalchemy import func

    from ..database.models import Election, ElectionResult
    return (db.query(func.count(ElectionResult.id), func.count(ElectionResult.candidate_votes_json))
            .join(Election, ElectionResult.election_id == Election.id)
            .filter(Election.election_year == year, Election.election_type == "VIDHAN_SABHA",
                    Election.state == BOOTH_STATE, ElectionResult.ac_number == ac_number).one())


def _current_ac(db: Session, ac_number: int):
    from ..database.models import AssemblyConstituency as AC
    return (_booth_acs(db).filter(AC.ac_number == ac_number, AC.delimitation == DELIM_CURRENT)
            .order_by(AC.id).first())


def _legacy_ac(db: Session, ac_name_hi: str):
    from ..database.models import AssemblyConstituency as AC
    return (_booth_acs(db).filter(AC.delimitation == DELIM_2003, AC.ac_name == ac_name_hi.strip())
            .order_by(AC.id).first())


def hierarchy_gaps(db: Session, cfg: PipelineConfig) -> dict[str, str]:
    """Configured states whose ECI hierarchy is missing or incomplete, with the reason."""
    from ..database.models import AssemblyConstituency as AC
    from ..database.models import District, State

    gaps: dict[str, str] = {}
    for name in cfg.states:
        st = db.query(State).filter(State.state_name == name).first()
        districts = [] if st is None else [
            d for d in db.query(District).filter(District.state_id == st.id)
            if not (d.district_code or "").startswith("UK2003-")]
        if not districts:
            gaps[name] = "state and districts not yet discovered"
            continue
        with_acs = {d for (d,) in db.query(AC.district_id)
                    .filter(AC.delimitation == DELIM_CURRENT,
                            AC.district_id.in_([d.id for d in districts])).distinct()}
        booth = name == BOOTH_STATE
        missing = sum(1 for d in districts if d.id not in with_acs)
        if missing and (cfg.all_districts or not booth):
            gaps[name] = f"{missing} district(s) have no constituencies yet"
        elif booth and _current_ac(db, cfg.ac_number) is None:
            gaps[name] = f"AC {cfg.ac_number} not yet discovered"
    return gaps


def plan_pipeline(db: Session, cfg: PipelineConfig) -> list[StagePlan]:
    """Inspect the database and decide which stages still have work to do.

    Pure read: no network, no writes. A stage is `needed` when its data is
    absent or incomplete, so a fully populated database plans nothing.
    """
    from sqlalchemy import func

    from ..database.models import AssemblyConstituency as AC
    from ..database.models import (
        District, Election, ElectionResult, Elector, ElectoralRoll, PartMapping,
        PollingStation, SourceFetch, State,
    )

    plans: list[StagePlan] = []

    # 1. state -> districts -> constituencies, for every configured state
    gaps = hierarchy_gaps(db, cfg)
    if gaps:
        plans.append(StagePlan("hierarchy", True, "; ".join(f"{n}: {r}" for n, r in gaps.items())))
    else:
        stored = []
        for name in cfg.states:
            st = db.query(State).filter(State.state_name == name).first()
            n_d = db.query(func.count(District.id)).filter(
                District.state_id == st.id, ~District.district_code.like("UK2003-%")).scalar()
            n_a = (db.query(func.count(AC.id)).join(District, AC.district_id == District.id)
                   .filter(District.state_id == st.id, AC.delimitation == DELIM_CURRENT).scalar())
            stored.append(f"{name}: {n_d} districts, {n_a} constituencies")
        plans.append(StagePlan("hierarchy", False, "; ".join(stored)))

    # 2-3. current polling stations and their PS-list enrichment, per constituency
    targets = _target_acs(db, cfg.station_acs)
    counts = dict(db.query(PollingStation.ac_id, func.count(PollingStation.id))
                  .filter(PollingStation.edition == EDITION_CURRENT)
                  .group_by(PollingStation.ac_id).all())
    no_parts: set[int] = set()
    for (note,) in db.query(SourceFetch.note).filter(SourceFetch.note.like("sir-parts ac=% parts=0")):
        m = re.search(r"ac=(\d+) ", note or "")
        if m:
            no_parts.add(int(m.group(1)))
    missing_parts = [a.ac_number for a in targets if not counts.get(a.id) and a.ac_number not in no_parts]
    with_stations = [a for a in targets if counts.get(a.id)]
    if not targets:
        plans.append(StagePlan("parts", True, "waits for constituency discovery"))
    else:
        plans.append(StagePlan("parts", bool(missing_parts),
                               f"{len(missing_parts)} constituencies without polling stations" if missing_parts
                               else f"polling stations stored for {len(with_stations)} of {len(targets)} "
                                    "constituencies", missing_parts))
    ps_outcome: dict[int, str] = {}
    for (note,) in db.query(SourceFetch.note).filter(
            SourceFetch.note.like(f"ps-list parser={PS_PARSER}/{PS_PARSER_VERSION} ac=%")):
        m = re.search(r" ac=(\d+) (stations|blocked)=", note or "")
        if m and ps_outcome.get(int(m.group(1))) != "stations":
            ps_outcome[int(m.group(1))] = m.group(2)
    todo_ps = [a.ac_number for a in with_stations if a.ac_number not in ps_outcome]
    if not with_stations:
        plans.append(StagePlan("ps-list", True, "waits for polling stations"))
    else:
        n_named = sum(1 for a in with_stations if ps_outcome.get(a.ac_number) == "stations")
        n_blocked = sum(1 for a in with_stations if ps_outcome.get(a.ac_number) == "blocked")
        plans.append(StagePlan("ps-list", bool(todo_ps),
                               f"{len(todo_ps)} constituencies to parse with {PS_PARSER}/{PS_PARSER_VERSION}"
                               if todo_ps else
                               f"names and areas for {n_named} constituencies; {n_blocked} blocked "
                               "(scanned, unpublished or unrecognised layout)", todo_ps))

    # 4. 2003 electoral rolls: a part is complete when every extracted row is stored
    legacy = _legacy_ac(db, cfg.legacy_ac_hi)
    complete: set[int] = set()
    if legacy is not None:
        rows = (db.query(PollingStation.part_number, ElectoralRoll.extracted_elector_count,
                         func.count(Elector.id))
                .join(ElectoralRoll, ElectoralRoll.polling_station_id == PollingStation.id)
                .outerjoin(Elector, Elector.electoral_roll_id == ElectoralRoll.id)
                .filter(PollingStation.ac_id == legacy.id, PollingStation.edition == EDITION_2003,
                        ElectoralRoll.extraction_status == "extracted")
                .group_by(PollingStation.part_number, ElectoralRoll.extracted_elector_count).all())
        complete = {part for part, expected, n in rows if expected and n == expected}
    # Parts whose PDF is a scan are recorded as blocked; they are retried on --refresh only.
    blocked = set()
    if legacy is not None:
        blocked = {p for (p,) in db.query(PollingStation.part_number)
                   .join(ElectoralRoll, ElectoralRoll.polling_station_id == PollingStation.id)
                   .filter(PollingStation.ac_id == legacy.id,
                           ElectoralRoll.extraction_status.like("blocked%"))}
    blocked_note = f"; parts {sorted(blocked)} blocked (scanned PDF, OCR required)" if blocked else ""
    if cfg.legacy_parts:
        missing = [p for p in cfg.legacy_parts if p not in complete and p not in blocked]
        plans.append(StagePlan("rolls", bool(missing),
                               (f"parts {missing} not yet extracted" if missing
                                else f"parts {sorted(complete)} extracted and validated") + blocked_note,
                               missing))
    else:
        plans.append(StagePlan("rolls", len(complete) + len(blocked) < cfg.legacy_limit,
                               f"{len(complete)} of {cfg.legacy_limit} parts extracted" + blocked_note))

    # 5. official 2003 -> current part mapping: every extracted part must have been
    #    looked up (a part the source has no mapping for is recorded as attempted)
    if not cfg.include_mapping:
        plans.append(StagePlan("mapping", False, "disabled", mode="disabled"))
    elif legacy is None or not complete:
        plans.append(StagePlan("mapping", True, "official part mapping not yet ingested"))
    else:
        mapped = {p for (p,) in db.query(PartMapping.from_part_number).filter(
            PartMapping.from_edition == EDITION_2003,
            PartMapping.from_ac_number == legacy.ac_number).distinct()}
        attempted = set()
        prefix = mapping_attempt_note(cfg.legacy_ac_hi, 0, 0).split(" part=")[0] + " part="
        for (note,) in db.query(SourceFetch.note).filter(SourceFetch.note.like(prefix + "%")):
            m = re.search(r" part=(\d+) ", note or "")
            if m:
                attempted.add(int(m.group(1)))
        missing = sorted(p for p in complete if p not in mapped and p not in attempted)
        n_map = db.query(func.count(PartMapping.id)).filter(
            PartMapping.from_edition == EDITION_2003,
            PartMapping.from_ac_number == legacy.ac_number).scalar() or 0
        plans.append(StagePlan("mapping", bool(missing),
                               f"parts {missing} not yet looked up" if missing
                               else f"{n_map} official mapping rows stored", missing))

    # 5b. mapping rows still in review: read the scanned 2024 list of their constituency
    if not cfg.include_mapping:
        plans.append(StagePlan("ps-list-2024", False, "disabled", mode="disabled"))
    else:
        from ..analytics.intelligence import evaluate_mappings
        review_acs = sorted({m.to_ac_number for m in evaluate_mappings(db)
                             if m.status == "review" and m.to_ac_number is not None})
        done_2024: set[int] = set()
        for (note,) in db.query(SourceFetch.note).filter(
                SourceFetch.note.like(ps_list_2024_note(0, "").split(" ac=")[0] + " ac=%")):
            m = re.search(r" ac=(\d+) status=", note or "")
            if m:
                done_2024.add(int(m.group(1)))
        todo_2024 = [a for a in review_acs if a not in done_2024]
        if db.query(PartMapping.id).first() is None:
            plans.append(StagePlan("ps-list-2024", True, "waits for the official part mapping"))
        else:
            plans.append(StagePlan("ps-list-2024", bool(todo_2024),
                                   f"OCR the scanned 2024 list for ACs {todo_2024} (mapping rows in review)"
                                   if todo_2024 else
                                   f"2024 list read for ACs {sorted(done_2024)}" if done_2024 else
                                   "no mapping row needs the 2024 list", todo_2024))

    # 6. Form 20 results per constituency and year. Items are encoded year*1000 + AC.
    if not cfg.include_form20:
        plans.append(StagePlan("form20", False, "disabled", mode="disabled"))
    else:
        f20_targets = _target_acs(db, cfg.form20_acs)
        outcome: dict[tuple[int, int], str] = {}
        for (note,) in db.query(SourceFetch.note).filter(
                SourceFetch.note.like(f"form20 parser={F20_PARSER}/{F20_PARSER_VERSION} year=%")):
            m = re.search(r" year=(\d+) ac=(\d+) status=(\w+)", note or "")
            if m:
                key = (int(m.group(1)), int(m.group(2)))
                if outcome.get(key) != "stored":
                    outcome[key] = m.group(3)
        todo = [y * 1000 + a.ac_number for y in cfg.form20_years for a in f20_targets
                if (y, a.ac_number) not in outcome]
        if not f20_targets:
            plans.append(StagePlan("form20", True, "waits for constituency discovery"))
        else:
            n_stored = sum(1 for v in outcome.values() if v == "stored")
            n_blocked = sum(1 for v in outcome.values() if v == "blocked")
            plans.append(StagePlan("form20", bool(todo),
                                   f"{len(todo)} constituency-year(s) to process with "
                                   f"{F20_PARSER}/{F20_PARSER_VERSION}" if todo else
                                   f"{n_stored} constituency-year(s) stored, {n_blocked} blocked", todo))

    # 7-8. offline verification and review outputs
    plans.append(StagePlan("verify", True, "link duplicates and results, check stored counts",
                           mode="always"))
    plans.append(StagePlan("review", True, "write verification review and quality report",
                           mode="always"))
    return plans


def run_pipeline(db: Session, report: PipelineReport, cfg: PipelineConfig, *,
                 http_factory, refresh: bool = False) -> HttpClient | None:
    """Bring the database up to date in one call: discovery, polling stations,
    roll download + extraction + validation, part mapping and Form 20.

    refresh=False (used by `serve`): only stages whose data is missing or
    incomplete run, so a restart on a complete database makes no network request.
    refresh=True (used by `pipeline`): every enabled stage runs; downloaded PDFs
    are still reused when `cfg.resume` is on.

    Stages are isolated — a failure is reported and later stages still run on the
    data that exists. The HTTP client is created only if a stage needs it and is
    returned (closed) so callers can report its statistics.
    """
    http: HttpClient | None = None

    def net() -> HttpClient:
        nonlocal http
        if http is None:
            http = http_factory()
        return http

    def hierarchy(plan: StagePlan) -> None:
        wanted = list(cfg.states) if refresh else list(hierarchy_gaps(db, cfg))
        failed = []
        for name in wanted:
            booth = name == BOOTH_STATE
            try:
                discover_hierarchy(db, net(), report, state_name=name,
                                   district_name=None if (cfg.all_districts or not booth) else cfg.district)
            except Exception as exc:
                db.rollback()
                failed.append(f"{name} ({type(exc).__name__})")
                report.warnings.append(f"hierarchy {name}: {str(exc)[:160]}")
        if failed:
            raise LookupError(f"discovery failed for {', '.join(failed)}")

    def parts(plan: StagePlan) -> None:
        acs = _target_acs(db, cfg.station_acs)
        if not acs:
            raise LookupError("no constituencies discovered yet")
        wanted = {a.ac_number for a in acs} if refresh else set(plan.items)
        stations, done, failures = 0, 0, []
        for a in acs:
            if a.ac_number not in wanted:
                continue
            try:
                stations += len(discover_parts(db, net(), None, a))
                done += 1
            except Exception as exc:
                db.rollback()
                failures.append(a.ac_number)
                report.warnings.append(f"parts AC{a.ac_number}: {str(exc)[:160]}")
        report.add("parts", not failures,
                   f"{stations} polling stations for {done} constituencies"
                   + (f"; failed for ACs {failures}" if failures else ""), stations)

    def ps_list(plan: StagePlan) -> None:
        from ..database.models import PollingStation
        with_st = {i for (i,) in db.query(PollingStation.ac_id)
                   .filter(PollingStation.edition == EDITION_CURRENT).distinct()}
        acs = [a for a in _target_acs(db, cfg.station_acs) if a.id in with_st]
        if not acs:
            raise LookupError("no constituency has polling stations yet")
        wanted = {a.ac_number for a in acs} if refresh else set(plan.items)
        tally: Counter = Counter()
        stations = 0
        for a in acs:
            if a.ac_number not in wanted:
                continue
            try:
                res = enrich_from_ps_list(db, net(), report, a, resume=cfg.resume)
            except Exception as exc:
                db.rollback()
                res = {"status": "failed", "stations": 0}
                report.warnings.append(f"ps-list AC{a.ac_number}: {str(exc)[:160]}")
            tally[res["status"]] += 1
            stations += res["stations"]
        report.add("ps-list", tally["failed"] == 0,
                   f"{tally['stored']} constituencies enriched ({stations} stations), "
                   f"{tally['blocked']} blocked, {tally['failed']} failed", stations)

    def rolls(plan: StagePlan) -> None:
        wanted = plan.items if (plan.items and not refresh) else cfg.legacy_parts
        ingest_legacy_rolls(db, net(), report, district_name_hi=cfg.legacy_district_hi,
                            ac_name_hi=cfg.legacy_ac_hi, parts=wanted,
                            limit=cfg.legacy_limit, resume=cfg.resume)

    def mapping(plan: StagePlan) -> None:
        from ..database.models import ElectoralRoll, PollingStation
        part_list = plan.items if (plan.items and not refresh) else cfg.legacy_parts
        if not part_list:
            legacy = _legacy_ac(db, cfg.legacy_ac_hi)
            part_list = sorted(p for (p,) in db.query(PollingStation.part_number)
                               .join(ElectoralRoll, ElectoralRoll.polling_station_id == PollingStation.id)
                               .filter(PollingStation.ac_id == legacy.id)) if legacy else []
        if not part_list:
            raise LookupError("no extracted 2003 roll parts to map")
        ingest_part_mapping(db, net(), report, ac_name_hi=cfg.legacy_ac_hi, parts=part_list)

    def ps_list_2024(plan: StagePlan) -> None:
        wanted = plan.items
        if refresh and not wanted:
            from ..analytics.intelligence import evaluate_mappings
            wanted = sorted({m.to_ac_number for m in evaluate_mappings(db)
                             if m.status == "review" and m.to_ac_number is not None})
        if not wanted:
            if not plan.needed:
                report.add("ps-list-2024", True, f"up to date — {plan.reason}")
                return
            raise LookupError("no official part mapping stored yet")
        tally: Counter = Counter()
        rows = 0
        for number in wanted:
            try:
                res = ocr_ps_list_2024(db, http, report, number, resume=cfg.resume, http_factory=net)
            except Exception as exc:
                db.rollback()
                res = {"status": "failed", "rows": 0}
                report.warnings.append(f"ps-list-2024 AC{number}: {str(exc)[:160]}")
            tally[res["status"]] += 1
            rows += res["rows"]
        report.add("ps-list-2024", tally["failed"] == 0,
                   f"{tally['stored']} constituency list(s) read by OCR ({rows} rows), "
                   f"{tally['blocked']} blocked, {tally['failed']} failed", rows)

    def form20(plan: StagePlan) -> None:
        acs = {a.ac_number for a in _target_acs(db, cfg.form20_acs)}
        if not acs:
            raise LookupError("no constituencies discovered yet")
        jobs = ([(y, n) for y in cfg.form20_years for n in acs] if refresh
                else [divmod(item, 1000) for item in plan.items])
        by_year: dict[int, list[int]] = {}
        for year, number in jobs:
            by_year.setdefault(year, []).append(number)
        tally: Counter = Counter()
        booths = 0
        for year, numbers in sorted(by_year.items()):
            try:
                entries = Form20Client(net()).list_acs(year)
            except LookupError as exc:          # no index configured for this year
                for number in numbers:
                    repo.record_fetch(db, source=SOURCE_FORM20, url=f"form20/{year}", ok=False,
                                      note=form20_note_prefix(year, number) + "status=blocked:no-index")
                db.commit()
                tally["blocked"] += len(numbers)
                report.warnings.append(f"form20 {year}: {exc}")
                continue
            except Exception as exc:
                tally["failed"] += len(numbers)
                report.warnings.append(f"form20 {year} index: {str(exc)[:160]}")
                continue
            for number in sorted(numbers):
                try:
                    res = ingest_form20(db, net(), report, year=year, ac_number=number,
                                        resume=cfg.resume, entries=entries)
                except Exception as exc:
                    db.rollback()
                    res = {"status": "failed", "booths": 0}
                    report.warnings.append(f"form20 {year} AC{number}: {str(exc)[:160]}")
                tally[res["status"]] += 1
                booths += res["booths"]
        report.add("form20", tally["failed"] == 0,
                   f"{tally['stored']} constituency-year(s) stored ({booths} booth results), "
                   f"{tally['blocked']} blocked, {tally['failed']} failed", booths)

    def verify(plan: StagePlan) -> None:
        from .verification import verify_all
        v = verify_all(db)
        report.add("verify", not v.roll_count_mismatches, v.summary(),
                   v.duplicates_linked + v.results_linked)
        report.warnings += [f"roll count mismatch — {m}" for m in v.roll_count_mismatches]

    def review(plan: StagePlan) -> None:
        from .review import write_review
        out_dir = cfg.review_dir or settings.processed_dir / "review"
        res = write_review(db, out_dir, backend=db.get_bind().dialect.name)
        q = res["queue"]
        report.add("review", True,
                   f"review queue: {q['mapping_rows']} mapping rows, {q['records']} records, "
                   f"{q['results']} results — files in {out_dir}",
                   q["mapping_rows"] + q["records"] + q["results"])

    runners = {"hierarchy": hierarchy, "parts": parts, "ps-list": ps_list,
               "rolls": rolls, "mapping": mapping, "ps-list-2024": ps_list_2024, "form20": form20,
               "verify": verify, "review": review}
    try:
        for name in PIPELINE_STAGES:
            # Re-plan before each stage so it sees what earlier stages just stored.
            plan = next(p for p in plan_pipeline(db, cfg) if p.name == name)
            if plan.mode == "disabled" or (not refresh and not plan.needed):
                report.add(name, True, f"up to date — {plan.reason}")
                continue
            try:
                runners[name](plan)
            except Exception as exc:
                db.rollback()
                log.warning("stage %s failed: %s", name, exc, exc_info=True)
                report.add(name, False, f"{type(exc).__name__}: {exc}"[:300])
    finally:
        if http is not None:
            http.close()
    return http


def form20_ac(db: Session, ac_number: int):
    """The AC a 2008-delimitation Form 20 belongs to (audit finding F-5).

    Every Vidhan Sabha election from 2012 onward uses the current delimitation,
    so the result joins to the `current` AC with that number — never the 2003
    AC that happens to share a number. Returns None if absent or ambiguous.
    """
    from ..database.models import AssemblyConstituency
    rows = _booth_acs(db).filter(
        AssemblyConstituency.ac_number == ac_number,
        AssemblyConstituency.delimitation == DELIM_CURRENT).all()
    return rows[0] if len(rows) == 1 else None


def backfill_form20_from_local(db: Session, *, year: int, ac_number: int,
                               pdf_path: Path) -> dict:
    """Add candidate vote columns + ac_id to already-stored Form 20 rows.

    Non-destructive: re-parses the locally stored, checksummed PDF and UPDATEs
    existing rows matched on (election, ac_number, part_number). A row is only
    touched when its stored totals equal the re-parsed totals, so a different
    document can never overwrite a result.
    """
    import hashlib

    from sqlalchemy import select

    from ..database.models import Election, ElectionResult, SourceFetch

    election = db.scalar(select(Election).where(
        Election.election_year == year, Election.election_type == "VIDHAN_SABHA",
        Election.state == BOOTH_STATE))
    if election is None:
        raise LookupError(f"no stored Vidhan Sabha {year} election — run the pipeline first")

    sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    fetched = db.scalars(select(SourceFetch.checksum_sha256).where(
        SourceFetch.url == election.source_url,
        SourceFetch.checksum_sha256.is_not(None))).all()
    checksum_ok = sha in fetched if fetched else None

    parsed = parse_form20_2012(pdf_path, ac_number)
    ac_row = form20_ac(db, ac_number)
    stored = {r.part_number: r for r in db.scalars(select(ElectionResult).where(
        ElectionResult.election_id == election.id,
        ElectionResult.ac_number == ac_number))}
    updated, mismatched, missing = 0, [], []
    for b in parsed.booths:
        row = stored.get(b.part_number)
        if row is None:
            missing.append(b.part_number)
            continue
        if (row.total_valid_votes, row.total_votes) != (b.total_valid_votes, b.total_votes):
            mismatched.append(b.part_number)
            continue
        row.candidate_votes_json = json.dumps(b.candidate_votes)
        row.vote_sum_matches = b.sum_matches_total
        if ac_row is not None:
            row.ac_id = ac_row.id
        updated += 1
    db.commit()
    return {"updated": updated, "mismatched": mismatched, "missing": missing,
            "checksum_matches_fetch_log": checksum_ok, "sha256": sha,
            "ac_id": ac_row.id if ac_row else None, "warnings": parsed.warnings}
