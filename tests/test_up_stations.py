"""Uttar Pradesh district polling-station lists (SIR 2026).

Fixtures are real Ghaziabad documents (fetched 2026-09-26): the district page, the
first three pages of 55 Sahibabad (Annexure-1, mis-mapped visual-order font, an edit
patch over station 39) and all of 58 Dholana (Annexure-3: header on page 1 only, a
different font, a partial constituency, and a printed elector total).
"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.database import repositories as repo
from app.database.models import Base, PollingStation, SourceFetch
from app.extraction.parsers.ps_list_up import decode, detect_font, fix_visual_unicode, parse_ps_list_up
from app.services import state_stations as ss
from app.sources.ceo_uttar_pradesh.ps_lists import CEO_INDEX, parse_ceo_index, parse_district_page

FX = Path(__file__).parent / "fixtures"
AC55 = FX / "ps_list_up_ac55_p1-3.pdf"
AC58 = FX / "ps_list_up_ac58_annexure3.pdf"


class TestDecoding:
    @pytest.mark.parametrize("raw, text", [
        ("कàपोिजटͪ वघालय", "कम्पोजिटविद्यालय"),
        ("भोपुरालालगेटमिÛदरवालȣ", "भोपुरालालगेटमन्दिरवाली"),
        ("ĒेटवेपिÞलकèकूल", "ग्रेटवेपब्लिकस्कूल"),
        ("शिÈतखÖड-3 इिÛदरापुरम", "शक्तिखण्ड-3 इन्दिरापुरम"),
        ("हष[ ǒबहार", "हर्ष बिहार"),
        ("साǑहबाबाद", "साहिबाबाद"),
    ])
    def test_visual_order_font(self, raw, text):
        assert fix_visual_unicode(raw) == text

    def test_kruti_dev_words_and_punctuation(self):
        assert decode("uxj fuxe xkft;kckn")[0] == "नगर निगम गाजियाबाद"
        assert decode("[k.M")[0] == "खण्ड"                  # "[" is Kruti ख, not the reph glyph
        assert decode("gkml]")[0] == "हाउस,"

    def test_font_b_is_repaired_with_the_shared_table(self):
        assert decode("दयावती मोदी कɊा जू०हा० ˋूल", "B")[0] == "दयावती मोदी कन्या जू०हा० स्कूल"
        assert detect_font("कƗ-3 ˋूल पाकŊ") == "B" and detect_font("कàपोिजटͪ क¢") == "A"


class TestAnnexure1:
    r = parse_ps_list_up(AC55, 55)

    def test_rows(self):
        assert self.r.font == "A"
        assert [x.number for x in self.r.rows] == list(range(1, 51))
        assert all(x.electors for x in self.r.rows)

    def test_first_station(self):
        x = self.r.rows[0]
        assert (x.number, x.electors) == (1, 1082)
        assert x.locality == "भौपुरा नगर निगम गाजियाबाद"
        assert x.building == "कम्पोजिटविद्यालयभौपुराकक्ष सं0 1"

    def test_edit_patch_is_one_station(self):
        x = next(y for y in self.r.rows if y.number == 39)
        assert x.electors == 600 and sum(1 for y in self.r.rows if y.number == 39) == 1


class TestAnnexure3:
    r = parse_ps_list_up(AC58, 58)

    def test_header_on_first_page_only_and_partial_constituency(self):
        numbers = [x.number for x in self.r.rows]
        assert self.r.font == "B" and numbers == list(range(335, 480))
        assert self.r.serial_coverage == 1.0                  # over its own range, not 1..479
        assert not [w for w in self.r.warnings if "header" in w]

    def test_station_electors_add_up_to_the_printed_total(self):
        assert self.r.printed_total == 123985 == sum(x.electors for x in self.r.rows)
        x = self.r.rows[0]
        assert (x.number, x.electors, x.building, x.area) == (335, 1119, "प्रा0 वि0 कनौजा कक्ष-1", "कनौजा भाग-1")


def test_district_page_lists_one_pdf_per_constituency():
    entries = parse_district_page((FX / "up_ps_list_ghaziabad_2025.html").read_text(encoding="utf-8"), "Ghaziabad")
    assert sorted(e.ac_number for e in entries) == [53, 54, 55, 56, 57, 58]
    assert next(e for e in entries if e.ac_number == 55).url.endswith("/2025/11/17627801114818.pdf")


# --------------------------------------------------------------------------
class FakeHttp:
    def get_text(self, url, **kw):
        name = "up_ceo_ps_list_index.html" if url == CEO_INDEX else "up_ps_list_ghaziabad_2025.html"
        return (FX / name).read_text(encoding="utf-8")

    def get_pdf(self, url, **kw):
        return AC58.read_bytes() if url.endswith("17627801729503.pdf") else AC55.read_bytes()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "raw_dir", tmp_path)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for code, name, ext in (("S24", "Uttar Pradesh", 23), ("S28", "Uttarakhand", 27)):
            st = repo.upsert_state(s, state_code=code, state_name=name, state_name_local=None, state_type="ST",
                                   external_id=ext, source="eci_gateway_api", source_url="u")
            d = repo.upsert_district(s, state=st, district_code=f"{code}01", district_number=1,
                                     district_name="Ghaziabad" if code == "S24" else "Dehradun",
                                     district_name_local=None, source="eci_gateway_api", source_url="u")
            repo.upsert_ac(s, district=d, ac_number=58, ac_name=f"AC 58 {code}", source="eci_gateway_api")
        s.commit()
        yield s


def test_ingestion_stores_within_the_state_and_is_resumable(db):
    run = ss.load_up_polling_stations(db, FakeHttp(), districts=["Ghaziabad"], acs=(58,))
    assert run.tally["stored"] == 1 and run.stations == 145 and run.electors == 123985
    up58 = ss.state_ac(db, "Uttar Pradesh", 58)
    uk58 = ss.state_ac(db, "Uttarakhand", 58)
    assert db.query(PollingStation).filter(PollingStation.ac_id == up58.id).count() == 145
    assert db.query(PollingStation).filter(PollingStation.ac_id == uk58.id).count() == 0
    assert not [w for w in run.warnings if "prints a total" in w]
    again = ss.load_up_polling_stations(db, FakeHttp(), districts=["Ghaziabad"], acs=(58,))
    assert again.tally["skipped"] == 1
    assert db.query(SourceFetch).filter(SourceFetch.note.like("ps-list-up %status=stored%")).count() == 1


def test_a_list_for_a_constituency_not_in_the_state_is_not_stored(db):
    # this database has no AC 55 in Uttar Pradesh: the list is refused, never attached
    # to an AC with that number elsewhere
    run = ss.load_up_polling_stations(db, FakeHttp(), districts=["Ghaziabad"], acs=(55,))
    assert run.tally["blocked"] == 1 and db.query(PollingStation).count() == 0


def test_unknown_district_is_refused(db):
    with pytest.raises(LookupError, match="no polling-station page for Atlantis"):
        ss.load_up_polling_stations(db, FakeHttp(), districts=["Atlantis"])


def test_ceo_index_covers_all_75_districts_in_eci_spelling():
    index = parse_ceo_index((FX / "up_ceo_ps_list_index.html").read_text(encoding="utf-8"))
    assert len(index) == 75 and {"Bulandshahr", "Prayagraj", "Lucknow", "Varanasi"} <= set(index)


def test_links_are_attributed_only_to_the_districts_acs():
    html = ('<table><tr><td>5</td><td>90- Agra Rural</td><td><a href="a.pdf">Details</a></td></tr>'
            '<tr><td>6</td><td>58-धौलाना (आंशिक)</td><td><a href="b.pdf">View</a></td></tr>'
            '<tr><td>7</td><td>Notice</td><td><a href="c.pdf">View</a></td></tr></table>')
    acs = {5: ["Nakur"], 58: ["Dholana", "धौलाना"], 86: ["Etmadpur"], 90: ["Agra Rural"], 207: ["Rasulabad"]}
    entries = parse_district_page(html + '<p>207-क्षेत्र पंचायत सादाबाद <a href="d.pdf">x</a></p>', "Agra",
                                  {86, 90}, page_url="https://x/", state_acs=acs)
    # "5" is a serial (and AC 5 is another district's); "58-धौलाना" is a split constituency named in
    # its label; "207-" is a station number, not AC 207 (whose name is absent)
    assert [(e.ac_number, e.url) for e in entries] == [(90, "https://x/a.pdf"), (58, "https://x/b.pdf")]


class TestHeaderLabels:
    def test_missing_labels_on_the_standard_grid_take_their_own_column(self):
        from app.extraction.parsers.ps_list_up import _fill_labels
        # Deoria: labels 8 and 10 not found; the rest sit in their own columns
        found = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 9: 9}
        assert _fill_labels(found, 10) == {n: n for n in range(1, 11)}

    def test_an_extra_serial_column_shifts_labels_not_positions(self):
        from app.extraction.parsers.ps_list_up import _fill_labels
        # Muradnagar: an unnumbered serial column first, label 8 missing
        found = {1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8, 9: 10, 10: 11}
        assert _fill_labels(found, 11)[8] == 9
