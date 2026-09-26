"""Constituency-level results from the ECI statistical reports ("Detailed Results").

For states whose booth-level Form 20 cannot be read (Telangana: scanned sheets), the ECI's
own statistical report gives every candidate's votes per constituency as typed text: an
Excel file (2023) or a PDF with a text layer (2018). These rows go to `ac_results`, never
to the booth-level `election_results`.

Each constituency is checked against the report's own arithmetic (general + postal = total
for every candidate; candidates add up to the printed constituency total where one is
printed) and the check is recomputed on every read.

Sources are declared per state and year below. The 2018 report sits on the ECI's old site,
which serves files only to a browser session, so it is fetched with Playwright when it is
not already in data/raw.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..database import repositories as repo
from ..database.models import AcResult
from ..extraction.parsers.eci_detailed_results import (
    PARSER_NAME, PARSER_VERSION, check_block, parse_detailed_results,
)
from ..extraction.pdf.downloader import sha256_bytes
from ..http_client import HttpClient
from .state_results import state_ac

log = logging.getLogger(__name__)
SOURCE_ECI_REPORT = "eci_statistical_report"

# state -> year -> how to get that year's "Detailed Results"
AC_RESULT_SOURCES: dict[str, dict[int, dict[str, str]]] = {
    "Telangana": {
        2023: {"url": "https://www.eci.gov.in/eci-backend/public/all_files/full-statistical-reports/"
                      "telangana/2023/Detailed_Results.xlsx",
               "page": "https://www.eci.gov.in/telangana-legislative-election-2023-statistical-report",
               "file": "2023_Detailed_Results.xlsx"},
        2018: {"page": "https://old.eci.gov.in/files/file/"
                       "9691-telangana-general-legislative-election-2018-statistical-report/",
               "browser_file": "Detailed Results",           # picked from the site's file list
               "file": "2018_Detailed_Results.pdf"},
    },
}


@dataclass
class AcResultsRun:
    state: str
    stored: dict[int, int] = field(default_factory=dict)       # year -> constituencies
    candidates: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        years = ", ".join(f"{y}: {n} ACs" for y, n in sorted(self.stored.items())) or "nothing"
        return f"{self.state} constituency results (ECI): {years}; {self.candidates:,} candidate rows"


def _slug(state: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", state.lower()).strip("-")


def fetch_with_browser(page_url: str, file_label: str, dest: Path) -> None:
    """Download one file from the old ECI site's file list in a real browser session."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.goto(page_url.rstrip("/") + "/?do=download", wait_until="networkidle", timeout=90000)
        links = page.eval_on_selector_all("a[href*='do=download'][href*='r=']", "els => els.map(e => e.href)")
        for href in links:
            with page.expect_download(timeout=120000) as dl:
                page.evaluate("u => { window.location.href = u; }", href)
            d = dl.value
            if file_label.lower() in d.suggested_filename.lower():
                dest.parent.mkdir(parents=True, exist_ok=True)
                d.save_as(dest)
                browser.close()
                return
        browser.close()
    raise LookupError(f"'{file_label}' not found among the files of {page_url}")


def _obtain(http: HttpClient, state: str, year: int, spec: dict, resume: bool) -> tuple[Path, str]:
    dest = settings.raw_dir / "ac_results" / _slug(state) / spec["file"]
    if not (resume and dest.exists() and dest.stat().st_size > 0):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if "url" in spec:
            dest.write_bytes(http.request("GET", spec["url"]).content)
        else:
            fetch_with_browser(spec["page"], spec["browser_file"], dest)
    return dest, spec.get("url") or spec["page"]


def load_ac_results(db: Session, http: HttpClient, state: str, *, years: tuple[int, ...] | None = None,
                    resume: bool = True) -> AcResultsRun:
    sources = AC_RESULT_SOURCES.get(state)
    if not sources:
        raise LookupError(f"no ECI statistical-report source declared for {state}")
    code = repo_state_code(db, state)
    run = AcResultsRun(state)
    for year in sorted(years or sources):
        spec = sources.get(year)
        if spec is None:
            run.warnings.append(f"{year}: no source declared")
            continue
        try:
            path, url = _obtain(http, state, year, spec, resume)
        except Exception as exc:
            run.warnings.append(f"{year}: could not obtain the report: {str(exc)[:200]}")
            continue
        data = path.read_bytes()
        repo.record_fetch(db, source=SOURCE_ECI_REPORT, url=url, ok=True, bytes_received=len(data),
                          checksum=sha256_bytes(data), local_path=str(path))
        blocks = parse_detailed_results(path)
        election = repo.upsert_election(db, election_year=year, election_type="VIDHAN_SABHA",
                                        state=state, source_url=url)
        db.query(AcResult).filter(AcResult.election_id == election.id).delete()
        n_cand = 0
        for b in blocks:
            ac = state_ac(db, state, b.ac_number)
            ok, why = check_block(b)
            if not ok:
                run.warnings.append(f"{year} AC{b.ac_number} {b.ac_name}: {why}")
            if ac is None:
                run.warnings.append(f"{year} AC{b.ac_number}: no current AC with that number in {state}")
            db.add_all([AcResult(
                election_id=election.id, ac_id=ac.id if ac else None, ac_number=b.ac_number, ac_name=b.ac_name,
                serial=c.serial, candidate_name=c.name, sex=c.sex, age=c.age, category=c.category,
                party=c.party, symbol=c.symbol, general_votes=c.general, postal_votes=c.postal,
                total_votes=c.total, vote_pct=c.pct, total_electors=b.total_electors,
                printed_ac_total=b.printed_total, source=SOURCE_ECI_REPORT, source_url=url,
                source_file=path.name, parser_version=f"{PARSER_NAME}/{PARSER_VERSION}")
                for c in b.candidates])
            n_cand += len(b.candidates)
        repo.record_fetch(db, source=SOURCE_ECI_REPORT, url=url, ok=True,
                          note=f"ac-results parser={PARSER_NAME}/{PARSER_VERSION} state={code} year={year} "
                               f"status=stored acs={len(blocks)} candidates={n_cand}")
        db.commit()
        run.stored[year] = len(blocks)
        run.candidates += n_cand
    return run


def repo_state_code(db: Session, state: str) -> str:
    from ..database.models import State
    st = db.query(State).filter(State.state_name == state).first()
    if st is None:
        raise LookupError(f"{state} is not discovered yet — run `python -m app pipeline --missing-only` first")
    return st.state_code
