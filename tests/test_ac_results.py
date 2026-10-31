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
UK_XLSX_2017 = FIX / "eci_uk_2017_sample.xlsx"         # Uttarakhand 2017, ACs 1-2
UK_PDF_2012 = FIX / "eci_uk_2012_sample.pdf"           # Uttarakhand 2012 report, first "Detailed Results" page


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


def test_xlsx_2017_spelled_out_columns_and_printed_total():
    blocks = parse_detailed_results(UK_XLSX_2017)
    assert [b.ac_number for b in blocks] == [1, 2]
    purola = blocks[0]
    assert purola.ac_name == "Purola" and purola.total_electors == 67496
    top = purola.candidates[0]
    assert (top.serial, top.name, top.sex, top.age, top.category, top.party, top.symbol) == (
        None, "RAJKUMAR", "M", 51, "SC", "INC", None)
    assert (top.general, top.postal, top.total) == (17548, 250, 17798)
    assert "None of the Above" in [c.name for c in purola.candidates]
    # "Total valid votes polled +NOTA" is the printed constituency total, NOTA included
    assert purola.printed_total == purola.total_votes == 49532
    assert check_block(purola) == (True, "candidate votes reconcile with the printed constituency total")


def test_pdf_2012_without_symbol_column_keeps_the_party():
    blocks = {b.ac_number: b for b in parse_detailed_results(UK_PDF_2012)}
    top = blocks[1].candidates[0]
    assert (top.name, top.category, top.party, top.symbol) == ("MALCHAND", "SC", "BJP", None)
    assert (top.general, top.postal, top.total) == (18004, 94, 18098)
    assert blocks[2].candidates[0].party == "UKDP" and blocks[3].candidates[0].party == "INC"
    for n in (1, 2, 3):
        assert blocks[n].printed_total == blocks[n].total_votes and check_block(blocks[n])[0], n


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


@pytest.fixture
def uk(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'uk.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    s = Session(engine)
    st = repo.upsert_state(s, state_code="S28", state_name="Uttarakhand", state_name_local=None, state_type="ST",
                           external_id=28, source="eci_gateway_api", source_url="u")
    d = repo.upsert_district(s, state=st, district_code="S2801", district_number=1, district_name="Uttarkashi",
                             district_name_local=None, source="eci_gateway_api", source_url="u")
    for n, name in ((1, "Purola"), (2, "Yamunotri"), (3, "Gangotri")):
        repo.upsert_ac(s, district=d, ac_number=n, ac_name=name, source="eci_gateway_api")
    # the 2012 election already exists, created by the booth-level Form 20 loader
    repo.upsert_election(s, election_year=2012, election_type="VIDHAN_SABHA", state="Uttarakhand",
                         source_url="https://election.uk.gov.in/form20/1-Purola.pdf")
    s.commit()
    monkeypatch.setattr(ac_results, "AC_RESULT_SOURCES", {"Uttarakhand": {
        2012: {"page": "https://example/2012", "file": UK_PDF_2012.name},
        2017: {"page": "https://example/2017", "file": UK_XLSX_2017.name}}})
    monkeypatch.setattr(ac_results, "_obtain", lambda http, state, year, spec, resume: (
        FIX / spec["file"], spec["page"]))
    yield s
    s.close()


def test_uttarakhand_years_load_and_keep_the_form20_election_source(uk):
    s = uk
    run = ac_results.load_ac_results(s, http=None, state="Uttarakhand")
    assert run.stored == {2012: 4, 2017: 2}
    e2012 = s.scalar(select(Election).where(Election.election_year == 2012))
    assert e2012.source_url == "https://election.uk.gov.in/form20/1-Purola.pdf"
    purola = s.scalar(select(AssemblyConstituency).where(AssemblyConstituency.ac_number == 1))
    assert {r.source_url for r in s.scalars(select(AcResult).where(AcResult.ac_id == purola.id))} == {
        "https://example/2012", "https://example/2017"}

    av = bi.all_availability(s, [purola.id])[0]
    assert av.ac_result_years == [2012, 2017] and av.election_years == [2012, 2017]
    views = bi.ac_level_results(s, purola)
    obs = bi.ac_observations(av, [], None, views)
    assert obs[0] == "Winning party: 2012 BJP · 2017 INC."
    assert obs[1] == f"In 2017 RAJKUMAR won by {17798 - 16785:,} votes ({views[1].margin_pct:.1f}% of votes polled)."


def test_state_results_overview_counts_verified_winners(uk):
    s = uk
    ac_results.load_ac_results(s, http=None, state="Uttarakhand")
    sr = bi.state_results_overview(s)
    assert sr.years == sr.ac_years == [2012, 2017]
    assert [r.av.ac.ac_number for r in sr.rows] == [1, 2, 3]
    assert sr.unlinked == 1                                   # 2012 AC 4 has no current AC here
    tallies = {t.year: t for t in sr.tallies}
    assert dict(tallies[2012].seats) == {"BJP": 1, "UKDP": 1, "INC": 1} and tallies[2012].verified == 3
    assert dict(tallies[2017].seats) == {"INC": 1, "BJP": 1} and tallies[2017].verified == 2
    assert sr.rows[0].latest.year == 2017 and sr.rows[0].latest.winner.candidate_name == "RAJKUMAR"

    # a result that fails the report's arithmetic is left out of the seat count
    gangotri = s.scalars(select(AcResult).where(AcResult.ac_number == 3)).first()
    gangotri.total_votes += 1
    s.commit()
    t2012 = {t.year: t for t in bi.state_results_overview(s).tallies}[2012]
    assert (t2012.verified, t2012.unverified) == (2, 1) and "INC" not in dict(t2012.seats)


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
