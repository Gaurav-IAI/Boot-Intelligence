"""Uttar Pradesh Form 20: discovery, both workbook layouts, ingestion and display rules.

Fixtures are real CEO UP files (fetched 2026-09-25): the Form 20 page at each
postback step, the 2017 AC 86 workbook (English S.No./Party/Votes layout) and the
2022 AC 90 workbook (ECI Hindi layout with rejected / NOTA columns).
"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analytics import intelligence as bi
from app.config import settings
from app.database import repositories as repo
from app.database.models import Base, ElectionResult, SourceFetch
from app.extraction.parsers.form20_up_xls import parse_form20_up
from app.services import state_results as sr
from app.sources.ceo_uttar_pradesh.client import (
    DISTRICT_FIELD, YEAR_FIELD, link_matches_year, parse_ac_links, parse_districts, parse_grid_pages,
    parse_years,
)

FX = Path(__file__).parent / "fixtures"
HINDI = FX / "form20_up_2022_ac90_hindi.xls"
TRIPLETS = FX / "form20_up_2017_ac86_triplets.xls"


def _html(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


class TestDiscovery:
    def test_years_and_districts_come_from_the_page(self):
        assert parse_years(_html("up_form20_start.html")) == [2022, 2019, 2017, 2014, 2012, 2009, 2007]
        districts = parse_districts(_html("up_form20_year2022.html"))
        assert "Agra" in districts and "Select District Name" not in districts

    def test_pager_row_is_not_a_constituency(self):
        html = _html("up_form20_2022_agra.html").replace(
            "</table>", "<tr><td><a href=\"javascript:__doPostBack('ctl00$ContentPlaceHolder1$GridView3',"
                        "'Page$2')\">2</a></td><td>1</td><td>2</td></tr></table>", 1)
        assert [e.ac_number for e in parse_ac_links(html)] == list(range(86, 95))
        assert parse_grid_pages(html) == ("ctl00$ContentPlaceHolder1$GridView3", [2])

    def test_links_from_another_years_folder_are_dropped(self):
        # the site's grid pager serves 2012 files on page 2 of a 2022 grid
        assert link_matches_year("https://ceouttarpradesh.nic.in/rollpdf/Form20_22/AC254.xlsx", 2022)
        assert not link_matches_year("https://ceouttarpradesh.nic.in/rollpdf/Form20_12/264.xls", 2022)
        assert link_matches_year("https://ceouttarpradesh.nic.in/rollpdf/Form20_12/264.xls", 2012)

    def test_constituency_links_are_read_from_the_grid(self):
        links = parse_ac_links(_html("up_form20_2022_agra.html"))
        assert [e.ac_number for e in links] == list(range(86, 95))
        assert links[0].url == "https://ceouttarpradesh.nic.in/rollpdf/Form20_22/AC86.xls"
        assert links[4].ac_label == "90-Agra Rural"


class TestTripletLayout:
    r = parse_form20_up(TRIPLETS, 86, 2017)

    def test_every_booth_reconciles(self):
        assert self.r.layout["kind"] == "english-triplets"
        assert len(self.r.booths) == 416 and self.r.booths_with_matching_sum == 416
        assert self.r.serial_coverage == 1.0

    def test_candidates_are_named_with_party(self):
        assert len(self.r.candidates) == 15
        assert self.r.candidates[0].name.upper().startswith("DR. DHARMPAL SINGH")
        assert self.r.candidates[0].party == "BSP"

    def test_first_booth(self):
        b = self.r.booths[0]
        assert (b.part_number, b.total_votes, b.total_valid_votes) == (1, 838, 831)
        assert b.polling_station_name.startswith("PRA.VI.GIJAULI")
        assert b.candidate_votes[:4] == [401, 20, 36, 335]


class TestHindiLayout:
    r = parse_form20_up(HINDI, 90, 2022)

    def test_rows_and_sheet_total(self):
        assert self.r.layout["kind"] == "hindi-eci"
        assert len(self.r.booths) == 472 and self.r.booths_with_matching_sum == 468
        assert self.r.grand_total_valid == 258444 and not self.r.warnings

    def test_round_subtotals_are_not_booths(self):
        parts = [b.part_number for b in self.r.booths]
        assert parts == sorted(parts) and max(parts) < 1000

    def test_names_converted_and_nota_rejected_kept(self):
        assert self.r.candidates[0].name == "उपेन्द्र सिंह" and self.r.candidates[0].party is None
        b = self.r.booths[0]
        assert (b.part_number, b.total_valid_votes, b.nota_votes, b.rejected_votes, b.total_votes) == \
            (1, 454, 6, 0, 460)

    def test_wrong_ac_is_not_read(self):
        r = parse_form20_up(HINDI, 91, 2022)
        assert r.booths == [] and r.warnings


class TestStructuralReader:
    """Hand-made variants: S.No. columns hold the booth serial, NOTA after the candidates."""

    def test_serial_sno_columns_are_not_candidates(self):
        r = parse_form20_up(FX / "form20_up_2017_ac168_structural.xls", 168, 2017)
        assert r.layout["kind"] == "structural" and r.layout["total_includes_nota"]
        assert len(r.booths) == 375 and r.booths_with_matching_sum == 375
        assert (r.candidates[0].name, r.candidates[0].party) == ("Jagdish Rawat", "RLD")
        b = r.booths[0]
        assert b.candidate_votes == [13, 550, 86, 130, 7, 4, 3, 4]
        assert (b.total_valid_votes, b.nota_votes, b.total_votes) == (797, 9, 806)

    def test_nota_printed_after_the_total(self):
        r = parse_form20_up(FX / "form20_up_2022_ac328_nota_after_total.xls", 328, 2022)
        assert r.booths_with_matching_sum == len(r.booths) == 511
        b = r.booths[0]
        # printed total 449 includes NOTA 12; the candidates reconcile with 449 - 12
        assert (sum(b.candidate_votes), b.total_valid_votes, b.nota_votes, b.total_votes) == (437, 437, 12, 449)
        assert r.candidates[0].name == "Candidate 1"      # the sheet prints no names: positional

    def test_a_sheet_of_another_constituency_is_refused(self):
        r = parse_form20_up(FX / "form20_up_2017_ac168_structural.xls", 169, 2017)
        assert r.booths == []


class TestBoothSheetCheck:
    """A candidate-by-round table can reconcile arithmetically; its 'booth numbers' repeat."""

    @staticmethod
    def _result(parts_and_names):
        from app.extraction.parsers.form20_2012 import BoothResult, Form20Result
        return Form20Result(booths=[BoothResult(part_number=p, polling_station_name=n,
                                                polling_station_name_raw=None, candidate_votes=[1],
                                                total_valid_votes=1, tendered_votes=None, total_votes=1,
                                                sum_matches_total=True, source_page=1, confidence=1.0)
                                    for p, n in parts_and_names])

    def test_repeating_numbers_are_not_a_booth_sheet(self):
        r = self._result([(p % 18 + 1, "x") for p in range(400)])
        assert r.serial_coverage > 1 and r.distinct_share < 0.1       # coverage alone was fooled

    def test_auxiliary_stations_count_as_their_own_booths(self):
        r = self._result([(p, "School") for p in range(1, 101)] + [(p, f"[{p}A] School") for p in range(1, 60)])
        assert r.distinct_share == 1.0


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
class FakeHttp:
    """Serves the saved pages; only AC 90's workbook is available."""

    def __init__(self):
        self.xls_requests = []

    def get_text(self, url, **kw):
        return _html("up_form20_start.html")

    def post_form(self, url, data, **kw):
        if data["__EVENTTARGET"] == YEAR_FIELD:
            return _html("up_form20_year2022.html")
        assert data[DISTRICT_FIELD] == "Agra"
        return _html("up_form20_2022_agra.html")

    def get_xls(self, url, **kw):
        self.xls_requests.append(url)
        if url.endswith("AC90.xls"):
            return HINDI.read_bytes()
        return TRIPLETS.read_bytes()          # a workbook without the requested AC


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "raw_dir", tmp_path)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for code, name, ext in (("S24", "Uttar Pradesh", 23), ("S28", "Uttarakhand", 27)):
            st = repo.upsert_state(s, state_code=code, state_name=name, state_name_local=None,
                                   state_type="ST", external_id=ext, source="eci_gateway_api", source_url="u")
            d = repo.upsert_district(s, state=st, district_code=f"{code}01", district_number=1,
                                     district_name="Agra" if code == "S24" else "Dehradun",
                                     district_name_local=None, source="eci_gateway_api", source_url="u")
            for n in (90, 91):
                repo.upsert_ac(s, district=d, ac_number=n, ac_name=f"AC {n}", source="eci_gateway_api")
        s.commit()
        yield s


def _ac(db, state, n):
    return sr.state_ac(db, state, n)


class TestIngestion:
    def test_stores_links_within_state_and_blocks_unreadable(self, db):
        http = FakeHttp()
        run = sr.load_state_results(db, http, "Uttar Pradesh", years=(2022,), districts=["Agra"], acs=(90, 91))
        assert run.tally["stored"] == 1 and run.tally["blocked"] == 1
        up90, uk90 = _ac(db, "Uttar Pradesh", 90), _ac(db, "Uttarakhand", 90)
        rows = db.query(ElectionResult).all()
        assert len(rows) == 472 and {r.ac_id for r in rows} == {up90.id}
        assert bi.results_for_ac(db, uk90) == []            # AC numbers repeat across states
        views = bi.results_for_ac(db, up90)
        q = bi.form20_quality_summary(views)
        assert (q["rows"], q["verified"], q["review"]) == (472, 468, 4)
        assert run.verified_rows == 468

    def test_rerun_skips_processed_and_refresh_reprocesses(self, db):
        sr.load_state_results(db, FakeHttp(), "Uttar Pradesh", years=(2022,), districts=["Agra"], acs=(90,))
        http = FakeHttp()
        run = sr.load_state_results(db, http, "Uttar Pradesh", years=(2022,), districts=["Agra"], acs=(90,))
        assert run.tally["skipped"] == 1 and http.xls_requests == []
        run = sr.load_state_results(db, FakeHttp(), "Uttar Pradesh", years=(2022,), districts=["Agra"], acs=(90,), refresh=True)
        assert run.tally["stored"] == 1 and db.query(ElectionResult).count() == 472   # replaced, not doubled

    def test_notes_do_not_collide_with_uttarakhand_planner(self, db):
        sr.load_state_results(db, FakeHttp(), "Uttar Pradesh", years=(2022,), districts=["Agra"], acs=(90,))
        notes = [n for (n,) in db.query(SourceFetch.note).filter(SourceFetch.note.is_not(None))]
        assert notes and all("state=S24" in n and "form20_2012_geometric" not in n for n in notes)

    def test_unsupported_year_is_refused(self, db):
        with pytest.raises(LookupError, match="pre-2008"):
            sr.load_state_results(db, FakeHttp(), "Uttar Pradesh", years=(2007,))


class TestCandidateAttribution:
    def _view(self, cols, total, cands):
        import json
        r = ElectionResult(id=1, ac_number=90, part_number=1, candidate_votes_json=json.dumps(cols),
                           total_valid_votes=total, source="t",
                           candidates_json=json.dumps(cands) if cands else None)
        from app.database.models import Election
        return bi._result_view(r, Election(election_year=2022, election_type="VIDHAN_SABHA", state="x"), len(cols))

    def test_leader_and_runner_up_named(self):
        v = self._view([5, 30, 10], 45, [{"name": "A", "party": "P"}, {"name": "B", "party": None},
                                          {"name": "C", "party": None}])
        assert v.leader["name"] == "B" and v.runner_up["name"] == "C"

    def test_tie_withholds_names(self):
        v = self._view([30, 30, 10], 70, [{"name": "A"}, {"name": "B"}, {"name": "C"}])
        assert v.verified and v.leader is None and v.runner_up is None

    def test_review_row_withholds_names(self):
        v = self._view([5, 30, 10], 99, [{"name": "A"}, {"name": "B"}, {"name": "C"}])
        assert not v.verified and v.named_columns is None and v.leader is None

    def test_no_names_when_counts_differ(self):
        v = self._view([5, 30, 10], 45, [{"name": "A"}, {"name": "B"}])
        assert v.named_columns is None
