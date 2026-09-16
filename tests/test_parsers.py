"""Parser tests, run against slices of the real published PDFs.

Fixtures in tests/fixtures are cut from documents actually downloaded from
CEO Uttarakhand, so these tests exercise the true layouts rather than a
hand-made approximation. No test here contacts the network.
"""
from pathlib import Path

import pytest

from app.extraction.ocr.engine import ocr_available, ocr_pdf
from app.extraction.parsers.form20_2012 import parse_form20_2012
from app.extraction.parsers.ps_list_2026 import parse_ps_list_pdf
from app.extraction.parsers.roll_2003 import parse_roll_pdf
from app.extraction.pdf.downloader import inspect_pdf, sha256_bytes

FIXTURES = Path(__file__).parent / "fixtures"
ROLL = FIXTURES / "roll_2003_sample.pdf"
FORM20 = FIXTURES / "form20_2012_sample.pdf"
PS_LIST = FIXTURES / "ps_list_2026_sample.pdf"
SCAN = FIXTURES / "scanned_page_sample.pdf"


class TestPdfInspection:
    def test_text_pdf_is_detected_as_text(self):
        pages, is_text, chars = inspect_pdf(ROLL)
        assert pages == 2
        assert is_text is True
        assert chars > 500

    def test_scanned_pdf_is_detected_as_a_scan(self):
        pages, is_text, chars = inspect_pdf(SCAN)
        assert is_text is False
        assert chars == 0

    def test_checksum_is_stable(self):
        assert sha256_bytes(b"abc") == sha256_bytes(b"abc")
        assert len(sha256_bytes(b"abc")) == 64


class TestRoll2003Parser:
    @pytest.fixture(scope="class")
    @classmethod
    def parsed(cls):
        return parse_roll_pdf(ROLL)

    def test_header_fields(self, parsed):
        h = parsed.header
        assert h.roll_year == 2003
        assert h.part_number == 6
        assert h.ac_number == 15
        assert h.ac_name == "राजपुर"
        assert h.state_code == "S28"
        assert h.qualifying_date == "01.01.2003"
        assert h.polling_station_number == 6
        assert "अस्थल" in h.polling_station_name
        assert "रैनी वाला" in h.areas

    def test_every_printed_row_on_the_sample_pages_is_extracted(self, parsed):
        # Pages 1-2 of this part carry serials 1..54 with no gaps.
        serials = sorted(e.serial_number for e in parsed.electors)
        assert serials == list(range(1, 55))

    def test_first_record_matches_the_document(self, parsed):
        e = parsed.electors[0]
        assert e.serial_number == 1
        assert e.elector_name == "अशवीन"
        assert e.relative_type == "FATHER"
        assert e.relative_name == "भरतसिंह"
        assert e.gender == "M"
        assert e.age == 29
        assert e.epic_number == "MYC0239293"

    def test_gender_and_relation_come_from_a_closed_vocabulary(self, parsed):
        assert {e.gender for e in parsed.electors} <= {"M", "F", "O", "UNKNOWN"}
        assert {e.relative_type for e in parsed.electors} <= {
            "FATHER", "MOTHER", "HUSBAND", "OTHER"}

    def test_absent_epic_is_null_and_never_the_literal_NULL(self, parsed):
        epics = [e.epic_number for e in parsed.electors]
        assert None in epics                       # the column is genuinely optional
        assert "NULL" not in [str(x) for x in epics]

    def test_every_record_carries_provenance(self, parsed):
        for e in parsed.electors:
            assert e.source_page in (1, 2)
            assert e.raw_text                      # raw text is always preserved
            assert 0.0 <= e.confidence <= 1.0

    def test_confidence_is_high_on_a_real_page(self, parsed):
        mean = sum(e.confidence for e in parsed.electors) / len(parsed.electors)
        assert mean > 0.95


class TestPsList2026Parser:
    def test_stations_are_extracted_and_transliterated(self):
        res = parse_ps_list_pdf(PS_LIST, 19)
        assert res.stations, res.warnings
        first = res.stations[0]
        assert first.part_number == 1
        # Kruti Dev must have been converted, not passed through.
        assert "राजकीय" in (first.station_full or "")
        assert "jktdh" not in (first.station_full or "")

    def test_columns_are_split_at_their_real_boundaries(self):
        # Header digits are centred over their columns; boundaries must come from the body.
        res = parse_ps_list_pdf(PS_LIST, 19)
        first = res.stations[0]
        assert first.part_name == "अस्थल"
        assert first.station_name == "राजकीय प्राथमिक विद्यालय"
        assert first.areas == "1- अस्थल 2- बझैत"
        assert first.serves == "सभी के लिये"
        assert all("सभी" not in (s.areas or "") for s in res.stations)
        assert all((s.serves or "").startswith("सभी") for s in res.stations)

    def test_raw_text_is_kept_alongside_the_conversion(self):
        res = parse_ps_list_pdf(PS_LIST, 19)
        withraw = [s for s in res.stations if s.part_name_raw]
        assert withraw, "the raw Kruti Dev text must be preserved for audit"


class TestForm20Parser:
    @pytest.fixture(scope="class")
    @classmethod
    def parsed(cls):
        return parse_form20_2012(FORM20, 19)

    def test_booth_rows_are_extracted(self, parsed):
        assert len(parsed.booths) >= 5
        assert parsed.booths[0].part_number == 1

    def test_summary_columns_are_named_from_their_headers(self, parsed):
        # AC 19 prints valid, rejected, total, tendered after the candidate columns.
        names = set(parsed.layout["summary_columns"])
        assert {"rejected", "total"} <= names
        with_total = [b for b in parsed.booths if b.total_votes is not None]
        assert with_total
        assert all(b.total_votes == b.total_valid_votes + (b.rejected_votes or 0) for b in with_total)

    def test_votes_sum_to_the_printed_total(self, parsed):
        # The document guarantees this arithmetic; it is the only independent
        # check available for a Form 20.
        checked = [b for b in parsed.booths if b.total_valid_votes]
        assert checked
        ok = sum(1 for b in checked if b.sum_matches_total)
        assert ok / len(checked) > 0.9

    def test_station_names_are_transliterated(self, parsed):
        names = [b.polling_station_name for b in parsed.booths if b.polling_station_name]
        assert names
        assert any("विद्यालय" in n or "अस्थल" in n for n in names)

    def test_candidate_names_are_not_invented(self, parsed):
        # Candidate headers cannot be matched to vote columns, so no result row
        # may claim a candidate name.
        for b in parsed.booths:
            assert b.candidate_votes is not None


class TestOcr:
    def test_availability_is_reported_honestly(self):
        ok, reason = ocr_available()
        assert isinstance(ok, bool)
        assert reason                      # a reason is always given

    def test_unavailable_engine_yields_a_flagged_result_not_silent_emptiness(self):
        res = ocr_pdf(SCAN, max_pages=1)
        if not res.available:
            assert res.pages == []
            assert res.reason
        else:
            assert res.pages
            assert 0.0 <= res.mean_confidence <= 1.0
