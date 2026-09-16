"""Text-encoding tests.

Both conversions are the highest-risk part of the pipeline: a silent regression
here corrupts every name in the database without failing anything else. The
expectations below were read off real Uttarakhand PDFs.
"""
from app.extraction.parsers.devanagari_fix import clean, clean_with_confidence
from app.extraction.parsers.krutidev import (
    convert, convert_with_confidence, looks_like_krutidev,
)


class TestKrutiDev:
    """Polling Station List 2026 and Form 20 2012 are in Kruti Dev."""

    def test_common_roll_vocabulary(self):
        cases = {
            "jktdh; izkFkfed fo|ky;": "राजकीय प्राथमिक विद्यालय",
            "lHkh ds fy;s": "सभी के लिये",
            "iapk;r?kj": "पंचायतघर",
            "jk;iqj": "रायपुर",
            "ukykikuh": "नालापानी",
            "fo/kku lHkk": "विधान सभा",
        }
        for src, expected in cases.items():
            assert convert(src) == expected, src

    def test_reph_is_moved_to_the_front_of_its_cluster(self):
        # Kruti Dev types the reph AFTER the syllable it belongs to.
        assert convert("fuokZpu") == "निर्वाचन"
        assert convert("fuokZpdksa dh dqy la[;k") == "निर्वाचकों की कुल संख्या"

    def test_reph_reorder_is_not_applied_twice(self):
        # A second pass would turn निर्वाचन into र्निवाचन.
        assert not convert("fuokZpu").startswith("र्")

    def test_ra_kaar_and_ra_vowels(self):
        assert convert("izkFkfed") == "प्राथमिक"          # z = ra-kaar
        assert convert("ernku dsUnz") == "मतदान केन्द्र"
        assert convert("izk:i&20") == "प्रारूप-20"         # : = रू
        assert convert("#i;s") == "रुपये"                  # # = रु

    def test_conjuncts(self):
        assert convert("{ks=") == "क्षेत्र"
        assert convert("f=osUnz flag jkor") == "त्रिवेन्द्र सिंह रावत"

    def test_detection_heuristic(self):
        assert looks_like_krutidev("jktdh; izkFkfed fo|ky; d-u- 1 lHkh ds fy;s")
        assert not looks_like_krutidev("राजकीय प्राथमिक विद्यालय संख्या एक सभी के लिये")

    def test_confidence_drops_when_characters_survive_unmapped(self):
        _, good = convert_with_confidence("jktdh; izkFkfed fo|ky;")
        assert good == 1.0
        _, poor = convert_with_confidence("@@@@@@@@@@ ^^^^^^^^^^")
        assert poor < 0.5


class TestDevanagariRepair:
    """The Legacy Roll 2003 PDFs map conjuncts into Latin-Extended codepoints."""

    def test_column_vocabulary(self):
        cases = {
            "पुŜष": "पुरुष",
            "मिहला": "महिला",
            "िपता": "पिता",
            "पित": "पति",
            "संƥा": "संख्या",
            "िनवाŊचक": "निर्वाचक",
        }
        for src, expected in cases.items():
            assert clean(src) == expected, src

    def test_names_seen_in_real_parts(self):
        cases = {
            "कृˁकुमार": "कृष्णकुमार",
            "अरिवȽ": "अरविन्द",
            "ʴामिसंह": "श्यामसिंह",
            "लƘी": "लक्ष्मी",
            "चȾŮकाश": "चन्द्रप्रकाश",
            "सįरता": "सरिता",
            "मुɄी वमाŊ": "मुन्नी वर्मा",
        }
        for src, expected in cases.items():
            assert clean(src) == expected, src

    def test_literal_NULL_export_artifact_is_removed(self):
        assert clean("भारती उिनयाल NULL") == "भारती उनियाल"

    def test_stray_combining_marks_are_dropped(self):
        assert clean("पित̻") == "पति"

    def test_confidence_reflects_unmapped_glyphs(self):
        _, good = clean_with_confidence("पुŜष")
        assert good == 1.0
        # An unmapped Latin-Extended glyph must lower confidence, not be hidden.
        cleaned, poor = clean_with_confidence("अǀǁǂब")
        assert poor < 1.0

    def test_ascii_epic_numbers_pass_through_untouched(self):
        assert clean("MYC0239293") == "MYC0239293"
        assert clean("UP/1/423/0153455") == "UP/1/423/0153455"
