"""Geometry and sequence rules of the scanned Polling Station List 2024 reader (no OCR engine needed)."""
import numpy as np

from app.extraction.ocr import ps_list_scan as scan


def test_template_fit_tolerates_a_page_shift_and_missing_rules():
    found = [x + 22 for x in (380, 722, 1257, 2283)]          # two rules not detected
    cols, score = scan.fit_template(found)
    assert score == 4
    assert cols == [402, 482, 744, 1279, 1982, 2305]


def test_template_fit_needs_three_agreeing_rules():
    cols, score = scan.fit_template([100, 900])
    assert cols is None and score < 3


def test_row_bands_come_from_rules_crossing_the_serial_column():
    dark = np.zeros((400, 600), dtype=bool)
    for y in (50, 150, 300):
        dark[y:y + 3, 80:560] = True                          # full-width row rules
    dark[220:222, 300:560] = True                              # inner rule of the areas column only
    assert scan.row_bands(dark, 80, 160) == [(51, 151), (151, 301)]


def test_serial_digits_are_read_in_either_script():
    assert scan.read_serial("१८") == 18
    assert scan.read_serial("17.") == 17
    assert scan.read_serial("") is None
    assert scan.read_serial("12345") is None


def test_repeated_column_captions_are_not_stations():
    assert scan.is_header_row("मतदान केन्द्र का परिक्ष", "भवन, ।जसम यह स्थित होगा", "मतदान क्षेत्र")
    assert not scan.is_header_row("नालापानी", "रायपुर ब्लाक सभागार", "1. नालापानी रोड 2. देवलोक कालौनी")
    rows = [dict(serial_text=t, locality=l, building=b, areas=a) for t, l, b, a in (
        ("4", "मतदान केन्द्र का परिक्ष", "भवन, जिसमें यह स्थित होगा", "मतदान क्षेत्र"),
        ("13", "नालापानी", "रायपुर ब्लाक सभागार", "1. नालापानी रोड"),
        ("14", "नालापानी", "राजकीय इण्टर कालेज क.न. 2", "1. सपेरा बस्ती"))]
    assert [r["serial"] for r in scan.clean_rows(rows)] == [13, 14]


def test_isolated_misread_serials_are_dropped():
    # "18" misread as "48" between 17 and 19; a lone value with no neighbour is dropped too
    assert scan.assign_serials([16, 17, 48, 19, None, 21]) == [16, 17, None, 19, None, 21]
    assert scan.assign_serials([None, 7, None]) == [None, None, None]
