"""Constituency-level results from the ECI statistical report ("Detailed Results")."""
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.analytics import intelligence as bi
from app.database import repositories as repo
from app.database.models import AcResult, AssemblyConstituency, Base, Election
from app.extraction.parsers.eci_detailed_results import check_block, parse_detailed_results
from app.services import ac_results
from app.services.state_export import export_states, import_states

FIX = Path(__file__).parent / "fixtures"
PDF_2018 = FIX / "eci_detailed_2018_sample.pdf"        # pages 1-2 and the last two of the 2018 report
XLSX_2023 = FIX / "eci_detailed_2023_sample.xlsx"      # ACs 1-2 of the 2023 report


# ------------------------------------------------------------------ parsers
def test_pdf_reads_candidates_by_column():
    blocks = {b.ac_number: b for b in parse_detailed_results(PDF_2018)}
    sirpur = blocks[1]
    assert sirpur.ac_name == "Sirpur" and sirpur.total_electors == 191131
    assert len(sirpur.candidates) == 13
    top = sirpur.candidates[0]
    assert (top.name, top.sex, top.age, top.category, top.party, top.symbol) == (
        "KONERU KONAPPA", "M", 63, "GEN", "TRS", "Car")
    assert (top.general, top.postal, top.total, top.pct) == (82957, 131, 83088, 50.57)
    assert sirpur.printed_total == sirpur.total_votes == 164288


def test_pdf_statewide_total_does_not_replace_the_last_constituency():
    last = {b.ac_number: b for b in parse_detailed_results(PDF_2018)}[119]
    assert last.printed_total == last.total_votes == 110445


def test_pdf_every_complete_constituency_reconciles():
    blocks = {b.ac_number: b for b in parse_detailed_results(PDF_2018)}
    for n in (1, 2, 3, 118, 119):
        assert check_block(blocks[n])[0], n
    # AC 4 continues past the fixture's page break: it must not pass as complete
    ok, why = check_block(blocks[4])
    assert not ok and "the report prints 173,421" in why


def test_xlsx_reads_serials_names_and_parties():
    blocks = parse_detailed_results(XLSX_2023)
    assert [b.ac_number for b in blocks] == [1, 2]
    sirpur = blocks[0]
    assert sirpur.ac_name == "Sirpur" and sirpur.total_electors == 227164 and sirpur.printed_total is None
    top = sirpur.candidates[0]
    assert (top.serial, top.name, top.party, top.total) == (1, "Dr.palvai Harish Babu", "BJP", 63702)
    assert all(c.general + c.postal == c.total for c in sirpur.candidates)
    assert check_block(sirpur) == (True, "general + postal = total for every candidate")


def test_check_block_flags_general_plus_postal_mismatch():
    b = parse_detailed_results(XLSX_2023)[0]
    b.candidates[1].total += 1
    ok, why = check_block(b)
    assert not ok and "general + postal differs" in why


# ------------------------------------------------------------------ loading and views
@pytest.fixture
def tg(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'tg.sqlite3').as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    s = Session(engine)
    st = repo.upsert_state(s, state_code="S29", state_name="Telangana", state_name_local=None, state_type="ST",
                           external_id=29, source="eci_gateway_api", source_url="u")
    d = repo.upsert_district(s, state=st, district_code="S2901", district_number=1, district_name="Adilabad",
                             district_name_local=None, source="eci_gateway_api", source_url="u")
    for n, name in ((1, "Sirpur"), (2, "Chennur")):
        repo.upsert_ac(s, district=d, ac_number=n, ac_name=name, source="eci_gateway_api")
    s.commit()
    # the reports come from the fixtures instead of the network
    monkeypatch.setattr(ac_results, "AC_RESULT_SOURCES", {"Telangana": {
        2018: {"page": "https://example/2018", "file": PDF_2018.name},
        2023: {"url": "https://example/2023.xlsx", "file": XLSX_2023.name}}})
    monkeypatch.setattr(ac_results, "_obtain", lambda http, state, year, spec, resume: (
        FIX / spec["file"], spec.get("url") or spec["page"]))
    yield s, url, tmp_path
    s.close()


def test_load_stores_both_years_and_links_current_acs(tg):
    s, _, _ = tg
    run = ac_results.load_ac_results(s, http=None, state="Telangana")
    assert run.stored == {2018: 6, 2023: 2}
    # 2018 ACs 3, 4, 118, 119 have no AC in this small state; AC 4 is also incomplete
    assert any("AC4" in w and "report prints" in w for w in run.warnings)
    sirpur = s.scalar(select(AssemblyConstituency).where(AssemblyConstituency.ac_number == 1))
    assert s.scalar(select(func.count()).select_from(AcResult).where(AcResult.ac_id == sirpur.id)) == 13 + 14

    # a re-run replaces, never duplicates
    ac_results.load_ac_results(s, http=None, state="Telangana")
    assert s.scalar(select(func.count()).select_from(AcResult).where(AcResult.ac_id == sirpur.id)) == 27


def test_ac_level_results_view(tg):
    s, _, _ = tg
    ac_results.load_ac_results(s, http=None, state="Telangana")
    sirpur = s.scalar(select(AssemblyConstituency).where(AssemblyConstituency.ac_number == 1))
    views = {v.year: v for v in bi.ac_level_results(s, sirpur)}
    assert set(views) == {2018, 2023}
    v = views[2018]
    assert v.winner.candidate_name == "KONERU KONAPPA" and v.runner_up.party == "INC"
    assert v.margin == 83088 - 59052 and v.total == 164288
    assert v.turnout_pct == pytest.approx(164288 / 191131 * 100, abs=0.01)
    assert v.verified
    assert views[2023].winner.candidate_name == "Dr.palvai Harish Babu" and views[2023].verified

    summary = bi.state_summary(s, state_id=sirpur.district.state_id)
    assert summary["acs_with_ac_results"] == 2 and summary["ac_result_years"] == [2018, 2023]


def test_export_and_import_carry_ac_results(tg):
    s, url, tmp = tg
    ac_results.load_ac_results(s, http=None, state="Telangana")
    n = s.scalar(select(func.count()).select_from(AcResult))
    s.close()
    report = export_states(url, tmp / "tg.sqlite3.gz", ["Telangana"])
    assert report.rows["ac_results"] == n

    prod = f"sqlite:///{(tmp / 'prod.sqlite3').as_posix()}"
    Base.metadata.create_all(create_engine(prod))
    import_states(tmp / "tg.sqlite3.gz", prod)
    with Session(create_engine(prod)) as p:
        assert p.scalar(select(func.count()).select_from(AcResult)) == n
        sirpur = p.scalar(select(AssemblyConstituency).where(AssemblyConstituency.ac_number == 1))
        rows = p.scalars(select(AcResult).where(AcResult.ac_id == sirpur.id)).all()
        assert len(rows) == 27
        years = {p.get(Election, r.election_id).election_year for r in rows}
        assert years == {2018, 2023}
