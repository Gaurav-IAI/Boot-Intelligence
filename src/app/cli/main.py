"""Command line interface for the election data POC (Uttarakhand, Uttar Pradesh, Telangana)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from ..analytics.booth import booth_stats, format_booth_stats, quality_report
from ..config import settings
from ..database.models import (
    AssemblyConstituency, District, ElectoralRoll, PollingStation, State,
)
from ..database.session import active_backend, get_session, init_db
from ..extraction.ocr.engine import ocr_available, ocr_pdf
from ..extraction.parsers.roll_2003 import parse_roll_pdf
from ..extraction.pdf.downloader import inspect_pdf
from ..http_client import HttpClient
from ..services import pipeline as pl
from ..states import BOOTH_STATE, DEFAULT_STATE, STATE_NAMES

app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)
console = Console()

# Defaults for the POC scope: one district, one AC, a handful of parts.
DEFAULT_DISTRICT = "Dehradun"
DEFAULT_AC = 19                    # Raipur (current delimitation)
DEFAULT_LEGACY_DISTRICT_HI = "देहरादून"
DEFAULT_LEGACY_AC_HI = "राजपुर"    # AC 15 in the 2003 delimitation -> AC 19 today
DEFAULT_LEGACY_PARTS = "6,7,8,9,10"


def _config(*, district: str = DEFAULT_DISTRICT, ac: int = DEFAULT_AC,
            legacy_district: str = DEFAULT_LEGACY_DISTRICT_HI,
            legacy_ac: str = DEFAULT_LEGACY_AC_HI, parts: str | None = DEFAULT_LEGACY_PARTS,
            limit: int = 5, resume: bool = True, all_districts: bool = True,
            skip_mapping: bool = False, skip_form20: bool = False,
            form20_years: str = "2012", acs: str = "all",
            states: str = "all") -> "pl.PipelineConfig":
    part_list = [int(p) for p in parts.split(",") if p.strip()] if parts else None
    years = tuple(int(y) for y in form20_years.split(",") if y.strip())
    numbers = None if acs.strip().lower() == "all" else tuple(int(a) for a in acs.split(",") if a.strip())
    return pl.PipelineConfig(
        states=_state_list(states),
        district=district, ac_number=ac, legacy_district_hi=legacy_district,
        legacy_ac_hi=legacy_ac, legacy_parts=part_list or None, legacy_limit=limit,
        all_districts=all_districts, include_mapping=not skip_mapping,
        include_form20=not skip_form20, resume=resume, form20_years=years or (2012,),
        station_acs=numbers, form20_acs=numbers)


def _state_list(states: str) -> tuple[str, ...]:
    if states.strip().lower() == "all":
        return STATE_NAMES
    by_key = {n.casefold(): n for n in STATE_NAMES}
    chosen = []
    for raw in states.split(","):
        name = by_key.get(raw.strip().casefold())
        if name is None:
            raise typer.BadParameter(f"unknown state {raw.strip()!r}; choose from {', '.join(STATE_NAMES)}")
        chosen.append(name)
    return tuple(chosen)


STATES_HELP = f"states to discover from ECI: 'all' or a comma-separated list of {', '.join(STATE_NAMES)}"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _http(concurrency: int) -> HttpClient:
    # Concurrency is exposed for future parallel fetching; today it also drives
    # the politeness delay so a higher value never means a faster hammering.
    delay = max(settings.http_delay_seconds, 1.2 / max(concurrency, 1))
    return HttpClient(delay=delay)


def _print_steps(report: pl.PipelineReport) -> None:
    t = Table(title="Pipeline steps", show_lines=False)
    t.add_column("step"); t.add_column("ok"); t.add_column("n", justify="right")
    t.add_column("detail", overflow="fold")
    for s in report.steps:
        t.add_row(s.name, "[green]yes[/]" if s.ok else "[red]NO[/]",
                  str(s.count or ""), s.detail)
    console.print(t)
    if report.warnings:
        console.print("\n[yellow]Warnings[/]")
        for w in report.warnings:
            console.print(f"  - {w}")


# --------------------------------------------------------------------------
@app.command("init-db")
def cmd_init_db(drop: bool = typer.Option(False, help="Drop and recreate tables")):
    """Create the database schema."""
    backend = init_db(drop=drop)
    console.print(f"schema ready on [bold]{backend}[/]")


@app.command("discover-state")
def cmd_discover_state(
    state: str = typer.Option(DEFAULT_STATE, "--state"),
    concurrency: int = typer.Option(3, "--concurrency"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Resolve a state and its official code from the ECI gateway."""
    _setup_logging(verbose)
    from ..sources.eci_api.client import EciApiClient
    with _http(concurrency) as http:
        s = EciApiClient(http).find_state(state)
    console.print(f"[bold]{s.state_name}[/] -> code [bold cyan]{s.state_code}[/] "
                  f"(ECI stateId {s.external_id}, type {s.state_type})")


@app.command("discover-districts")
def cmd_discover_districts(
    state: str = typer.Option(DEFAULT_STATE, "--state"),
    concurrency: int = typer.Option(3, "--concurrency"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """List districts for a state from the ECI gateway."""
    _setup_logging(verbose)
    from ..sources.eci_api.client import EciApiClient
    with _http(concurrency) as http:
        api = EciApiClient(http)
        s = api.find_state(state)
        ds = api.list_districts(s.state_code)
    t = Table(title=f"Districts — {s.state_name} ({s.state_code})")
    t.add_column("code"); t.add_column("no", justify="right"); t.add_column("name")
    for d in ds:
        t.add_row(d.district_code, str(d.district_number or ""), d.district_name)
    console.print(t)


@app.command("discover-acs")
def cmd_discover_acs(
    state: str = typer.Option(DEFAULT_STATE, "--state"),
    district: str = typer.Option(DEFAULT_DISTRICT, "--district"),
    concurrency: int = typer.Option(3, "--concurrency"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """List assembly constituencies in a district."""
    _setup_logging(verbose)
    from ..sources.eci_api.client import EciApiClient
    with _http(concurrency) as http:
        api = EciApiClient(http)
        s = api.find_state(state)
        ds = api.list_districts(s.state_code)
        match = next((d for d in ds if d.district_name.casefold() == district.casefold()), None)
        if match is None:
            raise typer.BadParameter(
                f"district {district!r} not found; available: "
                f"{', '.join(d.district_name for d in ds)}")
        acs = api.list_acs(match.district_code)
    t = Table(title=f"ACs — {match.district_name} ({match.district_code})")
    t.add_column("no", justify="right"); t.add_column("name")
    t.add_column("cat"); t.add_column("PC", justify="right"); t.add_column("acId")
    for a in acs:
        t.add_row(str(a.ac_number), a.ac_name, a.category or "",
                  str(a.pc_number or ""), a.official_code or "")
    console.print(t)


@app.command("discover-parts")
def cmd_discover_parts(
    ac: int = typer.Option(DEFAULT_AC, "--ac", help="AC number (current delimitation)"),
    limit: int = typer.Option(20, "--limit"),
    concurrency: int = typer.Option(3, "--concurrency"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """List polling stations / parts for an AC (CEO Uttarakhand, SIR 2026)."""
    _setup_logging(verbose)
    from ..sources.ceo_uttarakhand.client import SirPartsClient
    with _http(concurrency) as http:
        parts = SirPartsClient(http).list_parts(ac)
    t = Table(title=f"Parts — AC {ac} (SIR 2026); {len(parts)} total, showing {min(limit, len(parts))}")
    t.add_column("part", justify="right"); t.add_column("name")
    for p in parts[:limit]:
        t.add_row(str(p.part_number), p.part_name)
    console.print(t)


@app.command("inspect-roll")
def cmd_inspect_roll(
    roll: Path = typer.Option(..., "--roll", exists=True, help="PDF to inspect"),
):
    """Report whether a PDF is text-based or a scan, and the OCR engine status."""
    pages, is_text, chars = inspect_pdf(roll)
    console.print(f"[bold]{roll}[/]")
    console.print(f"  pages:          {pages}")
    console.print(f"  text layer:     {'yes' if is_text else 'NO — scanned'}")
    console.print(f"  chars sampled:  {chars}")
    console.print(f"  route:          {'direct text extraction' if is_text else 'OCR required'}")
    ok, reason = ocr_available()
    console.print(f"  OCR engine:     {'available' if ok else 'UNAVAILABLE'} — {reason}")


@app.command("extract-roll")
def cmd_extract_roll(
    roll: Path = typer.Option(..., "--roll", exists=True),
    limit: int = typer.Option(10, "--limit", help="rows to display"),
    out: Path = typer.Option(None, "--out", help="write all rows to JSON"),
):
    """Parse a roll PDF and print the structured records (no database write)."""
    pages, is_text, _ = inspect_pdf(roll)
    if not is_text:
        ok, reason = ocr_available()
        console.print(f"[yellow]{roll.name} is a scan.[/] OCR "
                      f"{'available' if ok else 'unavailable'}: {reason}")
        if not ok:
            raise typer.Exit(2)
        res = ocr_pdf(roll)
        console.print(f"OCR mean confidence {res.mean_confidence * 100:.1f}% "
                      f"over {len(res.pages)} pages")
        raise typer.Exit(0)

    parsed = parse_roll_pdf(roll)
    h = parsed.header
    console.print(f"[bold]{roll.name}[/] — AC {h.ac_number} {h.ac_name}, "
                  f"part {h.part_number}, roll {h.roll_year}")
    console.print(f"  polling station: {h.polling_station_number} {h.polling_station_name}")
    console.print(f"  areas: {h.areas}")
    console.print(f"  {len(parsed.electors)} electors over {parsed.page_count} pages")
    t = Table(show_lines=False)
    for c in ("serial", "name", "rel", "relative", "gender", "age", "epic", "pg", "conf"):
        t.add_column(c)
    for e in parsed.electors[:limit]:
        t.add_row(str(e.serial_number), e.elector_name or "", e.relative_type or "",
                  e.relative_name or "", e.gender or "", str(e.age or ""),
                  e.epic_number or "", str(e.source_page), f"{e.confidence:.2f}")
    console.print(t)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            [e.__dict__ for e in parsed.electors], ensure_ascii=False, indent=1),
            encoding="utf-8")
        console.print(f"wrote {len(parsed.electors)} rows to {out}")
    for w in parsed.warnings:
        console.print(f"[yellow]warning:[/] {w}")


@app.command("analyze-booth")
def cmd_analyze_booth(
    part: int = typer.Option(..., "--part", help="Part number"),
    ac: int = typer.Option(None, "--ac", help="AC number (disambiguates)"),
    edition: str = typer.Option(None, "--edition", help="e.g. ROLL-2003"),
):
    """Print booth-level statistics for an ingested part."""
    with get_session() as db:
        stmt = select(ElectoralRoll).join(PollingStation).where(
            PollingStation.part_number == part)
        if edition:
            stmt = stmt.where(PollingStation.edition == edition)
        if ac is not None:
            stmt = stmt.join(AssemblyConstituency).where(
                AssemblyConstituency.ac_number == ac)
        rolls = db.scalars(stmt).all()
        if not rolls:
            console.print(f"[red]no ingested roll for part {part}[/] — run "
                          f"`python -m app pipeline` first")
            raise typer.Exit(1)
        for r in rolls:
            console.print(format_booth_stats(booth_stats(db, r)))
            console.print("")


@app.command("quality")
def cmd_quality():
    """Data-quality dashboard (text form)."""
    with get_session() as db:
        q = quality_report(db, active_backend())
    console.print(f"[bold]Database backend:[/] {q.backend}\n")
    t = Table(title="Coverage")
    t.add_column("entity"); t.add_column("count", justify="right")
    for label, val in [
        ("states", q.states), ("districts", q.districts),
        ("ACs (current)", q.acs_current), ("ACs (2003)", q.acs_2003),
        ("polling stations", q.polling_stations), ("electoral rolls", q.rolls),
        ("electors", q.electors), ("part mappings", q.part_mappings),
        ("election results", q.election_results),
    ]:
        t.add_row(label, f"{val:,}")
    console.print(t)

    t2 = Table(title="Quality")
    t2.add_column("metric"); t2.add_column("value", justify="right")
    t2.add_row("HTTP fetch success rate", f"{q.fetch_success_rate:.1f}%")
    t2.add_row("rolls downloaded", f"{q.rolls_downloaded}/{q.rolls}")
    t2.add_row("rolls extracted", f"{q.rolls_extracted}/{q.rolls}")
    t2.add_row("official electors (sum)", f"{q.official_total:,}")
    t2.add_row("extracted electors (sum)", f"{q.extracted_total:,}")
    t2.add_row("EXTRACTION RATE", f"{q.extraction_rate:.2f}%")
    t2.add_row("records passing validation", f"{q.validity_rate:.2f}%")
    t2.add_row("duplicates flagged", f"{q.electors_duplicate:,}")
    t2.add_row("records with EPIC", f"{q.electors_with_epic:,}")
    t2.add_row("mean extraction confidence", f"{q.mean_confidence * 100:.2f}%")
    console.print(t2)


def _prepare_data(cfg: pl.PipelineConfig, *, concurrency: int, refresh: bool) -> bool:
    """Run the orchestrated pipeline and print its report. Returns True if every stage succeeded."""
    backend = init_db()
    report = pl.PipelineReport()
    with get_session() as db:
        http = pl.run_pipeline(db, report, cfg, http_factory=lambda: _http(concurrency),
                               refresh=refresh)
    _print_steps(report)
    if http is not None:
        console.print(f"\nHTTP: {http.stats.ok}/{http.stats.attempted} requests ok "
                      f"({http.stats.success_rate:.1f}%), {http.stats.retries} retries")
    else:
        console.print("\nHTTP: no requests needed — all data already present")
    console.print(f"Database backend: [bold]{backend}[/]")
    ok = all(s.ok for s in report.steps)
    if not ok:
        console.print("[yellow]Some stages failed (see above); the stored data is still usable "
                      "and the next run retries only what is missing.[/]")
    return ok


@app.command("pipeline")
def cmd_pipeline(
    district: str = typer.Option(DEFAULT_DISTRICT, "--district"),
    ac: int = typer.Option(DEFAULT_AC, "--ac"),
    legacy_district: str = typer.Option(DEFAULT_LEGACY_DISTRICT_HI, "--legacy-district"),
    legacy_ac: str = typer.Option(DEFAULT_LEGACY_AC_HI, "--legacy-ac"),
    limit: int = typer.Option(5, "--limit", help="number of parts to ingest when --parts is empty"),
    parts: str = typer.Option(DEFAULT_LEGACY_PARTS, "--parts", help="explicit part numbers, e.g. 6,7,8"),
    concurrency: int = typer.Option(3, "--concurrency"),
    resume: bool = typer.Option(True, "--resume/--no-resume"),
    all_districts: bool = typer.Option(True, "--all-districts/--district-only",
                                       help="discover constituencies for every district"),
    missing_only: bool = typer.Option(False, "--missing-only",
                                      help="run only stages whose data is missing (what `serve` does)"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    skip_form20: bool = typer.Option(False, "--skip-form20"),
    skip_mapping: bool = typer.Option(False, "--skip-mapping"),
    form20_years: str = typer.Option("2012", "--form20-years",
                                     help="comma-separated, e.g. 2012,2017,2022 (2017/2022 are recorded as blocked)"),
    acs: str = typer.Option("all", "--acs",
                            help="constituencies to load polling stations and Form 20 for: 'all' or e.g. 19,20"),
    states: str = typer.Option("all", "--states", help=STATES_HELP),
    verbose: bool = typer.Option(True, "--verbose/--quiet", "-v"),
):
    """Run the whole flow in one command: discovery, rolls + validation, mapping,
    Form 20, verification and the review/quality outputs."""
    _setup_logging(verbose)
    cfg = _config(district=district, ac=ac, legacy_district=legacy_district,
                  legacy_ac=legacy_ac, parts=parts, limit=limit, resume=resume,
                  all_districts=all_districts, skip_mapping=skip_mapping,
                  skip_form20=skip_form20, form20_years=form20_years, acs=acs, states=states)

    if dry_run:
        console.print("[bold]DRY RUN[/] — the plan, no requests, no writes:")
        for line in [
            f"1. ECI /common/states           -> resolve {', '.join(cfg.states)} to their codes",
            f"2. ECI /common/districts/<code> -> all districts of each state",
            f"3. ECI /common/acs/<districtCd> -> ACs for {'every district' if all_districts else district}; "
            f"target AC {ac} ({BOOTH_STATE})",
            f"   steps 4-9 use CEO {BOOTH_STATE} sources and run for {BOOTH_STATE} only",
            f"4. CEO UK SearchAdsEpic/Parts   -> SIR 2026 polling stations for ACs: {acs}",
            f"5. CEO UK PSSIR2026/<ac>.pdf      -> station names + areas (quality-gated; scans blocked)",
            f"6. CEO UK Roll 2003 PDFs        -> {legacy_ac} parts "
            f"{cfg.legacy_parts or f'(first {limit})'} -> electors, validation, duplicates",
            f"7. CEO UK village-details       -> official 2003->2025 part mapping",
            f"8. CEO UK PS-2024LS/<ac>.pdf    -> OCR of the scanned 2024 list (mapping rows in review)",
            f"9. CEO UK Form 20 {form20_years}, ACs: {acs} -> booth results (quality-gated)",
            f"10. verify (offline)            -> link duplicates + results, check stored counts",
            f"11. review (offline)            -> {settings.processed_dir / 'review'}",
        ]:
            console.print("  " + line)
        with get_session() as db:
            init_db()
            console.print("\n[bold]Current state[/] (what `serve` would run):")
            for p in pl.plan_pipeline(db, cfg):
                flag = "[yellow]run[/]" if p.needed else "[green]up to date[/]"
                console.print(f"  {p.name:<10} {flag}  {p.reason}")
        console.print("\nNo CAPTCHA-gated or authenticated endpoint is contacted.")
        raise typer.Exit(0)

    ok = _prepare_data(cfg, concurrency=concurrency, refresh=not missing_only)
    console.print("\nNext: [bold]python -m app serve[/]")
    if not ok:
        raise typer.Exit(1)


@app.command("backfill-form20")
def cmd_backfill_form20(
    year: int = typer.Option(2012, "--year"),
    ac: int = typer.Option(DEFAULT_AC, "--ac"),
    pdf: Path = typer.Option(None, "--pdf", help="defaults to data/raw/form20/{year}_AC{ac}.pdf"),
):
    """Add per-booth candidate vote columns and ac_id to stored Form 20 rows (no network, no deletes)."""
    path = pdf or settings.raw_dir / "form20" / f"{year}_AC{ac}.pdf"
    if not path.exists():
        console.print(f"[red]{path} not found[/] — run `python -m app pipeline` first")
        raise typer.Exit(1)
    with get_session() as db:
        res = pl.backfill_form20_from_local(db, year=year, ac_number=ac, pdf_path=path)
    console.print(f"updated {res['updated']} booth rows (ac_id={res['ac_id']})")
    console.print(f"  sha256 {res['sha256'][:16]}…  matches fetch log: "
                  f"{res['checksum_matches_fetch_log']}")
    if res["mismatched"]:
        console.print(f"[yellow]skipped (totals differ from stored): {res['mismatched']}[/]")
    if res["missing"]:
        console.print(f"[yellow]parsed but not stored: {res['missing']}[/]")
    for w in res["warnings"]:
        console.print(f"[yellow]warning:[/] {w}")


@app.command("results")
def cmd_results(
    state: str = typer.Option(..., "--state", help="Uttar Pradesh or Telangana"),
    years: str = typer.Option("all", "--years", help="'all' (the state's supported years) or e.g. 2022,2017"),
    district: str = typer.Option("all", "--district",
                                 help="UP only: 'all' or comma-separated district names, e.g. Agra"),
    acs: str = typer.Option("all", "--acs", help="'all' or comma-separated AC numbers"),
    refresh: bool = typer.Option(False, "--refresh",
                                 help="re-process constituency-years already stored or blocked"),
    resume: bool = typer.Option(True, "--resume/--no-resume", help="reuse downloaded files"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Load Form 20 booth results for Uttar Pradesh or Telangana (Uttarakhand's come from `pipeline`).

    Not run by `serve`: these states have ~1,400 result files, so they load only on
    request. Resumable — already processed constituency-years are skipped.
    """
    from ..services.state_results import load_state_results

    _setup_logging(verbose)
    (name,) = _state_list(state)
    if name == BOOTH_STATE:
        console.print(f"[yellow]{BOOTH_STATE} results are loaded by `python -m app pipeline`.[/]")
        raise typer.Exit(1)
    year_t = None if years.strip().lower() == "all" else tuple(int(y) for y in years.split(",") if y.strip())
    ac_t = None if acs.strip().lower() == "all" else tuple(int(a) for a in acs.split(",") if a.strip())
    dists = None if district.strip().lower() == "all" else [d.strip() for d in district.split(",") if d.strip()]
    init_db()
    with get_session() as db, _http(3) as http:
        try:
            run = load_state_results(db, http, name, years=year_t, districts=dists, acs=ac_t,
                                     resume=resume, refresh=refresh)
        except LookupError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1)
    for w in run.warnings[:40]:
        console.print(f"[yellow]warning:[/] {w}")
    if run.warnings:
        review = settings.processed_dir / "review"
        review.mkdir(parents=True, exist_ok=True)
        log_path = review / f"results_{name.replace(' ', '_').lower()}_warnings.txt"
        log_path.write_text("\n".join(run.warnings) + "\n", encoding="utf-8")
        more = f"{len(run.warnings) - 40} more; " if len(run.warnings) > 40 else ""
        console.print(f"[yellow]… {more}all {len(run.warnings)} warnings in {log_path}[/]")
    console.print(f"\n[bold]{run.summary()}[/]")
    console.print(f"HTTP: {http.stats.ok}/{http.stats.attempted} requests ok, {http.stats.retries} retries")
    if run.tally["failed"]:
        raise typer.Exit(1)


@app.command("stations")
def cmd_stations(
    district: str = typer.Option("all", "--district",
                                 help="'all' checked districts, or comma-separated names, e.g. Ghaziabad"),
    acs: str = typer.Option("all", "--acs", help="'all' or comma-separated AC numbers"),
    refresh: bool = typer.Option(False, "--refresh", help="re-process lists already stored or blocked"),
    resume: bool = typer.Option(True, "--resume/--no-resume", help="reuse downloaded PDFs"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Load current (SIR 2026) polling stations for Uttar Pradesh from district polling-station lists.

    Only districts whose list page has been checked are available (see
    sources/ceo_uttar_pradesh/ps_lists.py). Uttarakhand's come from `pipeline`.
    """
    from ..services.state_stations import load_up_polling_stations

    _setup_logging(verbose)
    ac_t = None if acs.strip().lower() == "all" else tuple(int(a) for a in acs.split(",") if a.strip())
    dists = None if district.strip().lower() == "all" else [d.strip() for d in district.split(",") if d.strip()]
    init_db()
    with get_session() as db, _http(3) as http:
        try:
            run = load_up_polling_stations(db, http, districts=dists, acs=ac_t, resume=resume, refresh=refresh)
        except LookupError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1)
    for w in run.warnings[:40]:
        console.print(f"[yellow]warning:[/] {w}")
    console.print(f"\n[bold]{run.summary()}[/]")
    if run.tally["failed"]:
        raise typer.Exit(1)


@app.command("migrate-db")
def cmd_migrate_db(
    source: str = typer.Option(settings.database_url_fallback, "--from",
                               help="source database URL (default: the local SQLite file)"),
    target: str = typer.Option(settings.database_url, "--to", help="target database URL (default: DATABASE_URL)"),
    replace: bool = typer.Option(False, "--replace", help="empty a non-empty target first"),
):
    """Copy all loaded data from one database into another — e.g. this machine's SQLite file into
    the server's PostgreSQL — so a new server needs no downloads and no re-parsing."""
    from ..services.db_copy import copy_database

    console.print(f"copying [bold]{source.split('@')[-1]}[/] -> [bold]{target.split('@')[-1]}[/]")
    try:
        report = copy_database(source, target, replace=replace)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    for name, n in report.rows.items():
        console.print(f"  {name:<24} {n:>10,}")
    console.print(f"[bold green]{report.total:,} rows copied[/]")


@app.command("serve")
def cmd_serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    skip_pipeline: bool = typer.Option(False, "--skip-pipeline",
                                       help="start on existing data without checking it"),
    refresh: bool = typer.Option(False, "--refresh",
                                 help="re-run every pipeline stage, not only missing ones"),
    concurrency: int = typer.Option(3, "--concurrency"),
    states: str = typer.Option("all", "--states", help=STATES_HELP),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Prepare all data (discovery, rolls, validation, mapping, Form 20), then run the dashboard.

    Only stages whose data is missing or incomplete run, so restarts are fast and
    work offline once the data exists.
    """
    import uvicorn

    _setup_logging(verbose)
    if not skip_pipeline:
        console.print("[bold]Preparing data[/] — checking what is missing…\n")
        _prepare_data(_config(states=states), concurrency=concurrency, refresh=refresh)
    console.print(f"\n[bold green]Dashboard:[/] http://{host}:{port}   (Ctrl+C to stop)")
    uvicorn.run("app.web.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
