"""Telangana 2018 Form 20 by OCR: the checks that decide what may be stored (no OCR needed)."""
from types import SimpleNamespace

from app.extraction.parsers import form20_tg_ocr as t
from app.services import telangana_results as tg
from app.sources.ceo_telangana.client import parse_index


# ------------------------------------------------------------------ the row check
def test_reconcile_accepts_the_one_reading_that_fits_both_printed_checks():
    # 5 + 232 + 28 = 265 valid; 265 + 0 rejected + 10 NOTA = 275
    assert t.reconcile([["5"], ["232"], ["28"], ["265"], ["0"], ["10"], ["275"]]) == (5, 232, 28, 265, 0, 10, 275)
    # a second opinion that is wrong is harmless while only one combination fits
    assert t.reconcile([["5", "8"], ["232"], ["28"], ["265"], ["0"], ["10"], ["275"]]) == (5, 232, 28, 265, 0, 10, 275)


def test_reconcile_refuses_ambiguity_and_gaps():
    # 5 + 28 and 6 + 27 both make 265 with 232: two readings fit, so none is taken
    assert t.reconcile([["5", "6"], ["232"], ["28", "27"], ["265"], ["0"], ["10"], ["275"]]) is None
    assert t.reconcile([["5"], ["232"], ["28"], ["264"], ["0"], ["10"], ["275"]]) is None   # sums disagree
    assert t.reconcile([["5"], [], ["28"], ["265"], ["0"], ["10"], ["275"]]) is None       # unread cell
    assert t.reconcile([]) is None


def test_trimmed_drops_a_stray_edge_digit_only_for_long_readings():
    assert t.trimmed(["15771"]) == ["15771", "5771", "1577"]
    assert t.trimmed(["12"]) == ["12"]


# ------------------------------------------------------------------ placing booths
def _booth(ps, votes=(1, 2), serial=None, page=1):
    r = t.TgRow(page=page, kind="booth", serial=serial, ps_label=ps)
    if votes is not None:
        r.votes = list(votes)
        r.total_valid, r.rejected, r.nota, r.total = sum(votes), 0, 0, sum(votes)
    return r


def _sheet(rows, columns=2):
    return t.assemble([t.PageRead(page=1, columns=columns, rows=rows)])


def test_a_ps_number_needs_a_neighbour_that_agrees():
    s = _sheet([_booth("1"), _booth("2"), _booth("47"), _booth("4"), _booth("5")])
    assert [r.ps_label for r in s.booths_read] == ["1", "2", "4", "5"]      # 47 fits no neighbour
    assert s.booths_expected == 5


def test_auxiliary_station_follows_its_base_number():
    s = _sheet([_booth("11"), _booth("12"), _booth("12A"), _booth("13")])
    assert [r.ps_label for r in s.booths_read] == ["11", "12", "12A", "13"]
    assert s.booths_expected == 14                                          # 13 + one auxiliary


def test_a_printed_serial_can_confirm_an_isolated_row():
    s = _sheet([_booth("1"), _booth("2"), _booth("9", serial=9), _booth("20")])
    assert [r.ps_label for r in s.booths_read] == ["1", "2", "9"]


def test_duplicate_labels_are_not_placed():
    s = _sheet([_booth("1"), _booth("2"), _booth("3"), _booth("2"), _booth("3")])
    assert [r.ps_label for r in s.booths_read] == ["1"]


def test_unreconciled_rows_are_counted_but_not_read():
    s = _sheet([_booth("1"), _booth("2", votes=None), _booth("3")])
    assert [r.ps_label for r in s.booths_read] == ["1", "3"] and s.booths_expected == 3


def test_pages_with_another_column_count_are_skipped():
    pages = [t.PageRead(1, 2, [_booth("1"), _booth("2")]), t.PageRead(2, 3, [_booth("3"), _booth("4")]),
             t.PageRead(3, 2, [_booth("5"), _booth("6")])]
    s = t.assemble(pages)
    assert s.candidate_columns == 2 and [r.ps_label for r in s.booths_read] == ["1", "2", "5", "6"]
    assert "page 2" in s.warnings[0]


# ------------------------------------------------------------------ naming and the gate
def _eci(name, party, general, total):
    return SimpleNamespace(candidate_name=name, party=party, general_votes=general, total_votes=total)


def _label(kind, options):
    return t.TgRow(page=9, kind=kind, options=options)


def test_columns_are_named_only_by_two_exact_matches():
    eci = [_eci("KONERU", "TRS", 82957, 83088), _eci("PALVAI", "INC", 58813, 59052)]
    rows = [_booth("1"), _booth("2"),
            _label("total", [["58813"], ["82957", "8295"]]),          # EVM totals, sheet order
            _label("postal", [["239"], ["131"]]),
            _label("total", [["59052"], ["83088"]])]                  # grand totals
    sheet = _sheet(rows)
    assert tg.name_columns(sheet, eci) == [{"name": "PALVAI", "party": "INC"},
                                           {"name": "KONERU", "party": "TRS"}]
    # a grand total that does not match leaves the sheet unnamed, never half-named
    rows[-1] = _label("total", [["59052"], ["83000"]])
    assert tg.name_columns(_sheet(rows), eci) is None


def test_gate_requires_the_ecis_candidate_count():
    sheet = _sheet([_booth(str(i)) for i in range(1, 12)])
    assert "3 candidates" in tg.gate(sheet, [_eci("A", "X", 99, 99)] * 3)
    assert "run `results --state Telangana` first" in tg.gate(sheet, [])


def test_gate_caps_named_columns_by_the_ecis_evm_votes():
    # 11 booths read (110 and 11 votes), booth 12 not read
    sheet = _sheet([_booth(str(i), votes=(10, 1)) for i in range(1, 12)] + [_booth("12", votes=None)])
    eci = [_eci("A", "X", 100, 101), _eci("B", "Y", 50, 50)]
    names = [{"name": "A", "party": "X"}, {"name": "B", "party": "Y"}]
    assert "more than the ECI report's EVM votes for A" in tg.gate(sheet, eci, names)
    eci[0] = _eci("A", "X", 200, 201)
    assert tg.gate(sheet, eci, names) is None                    # partial: sums only capped
    complete = _sheet([_booth(str(i), votes=(10, 1)) for i in range(1, 12)])
    assert "do not add up to the ECI report's EVM votes" in tg.gate(complete, eci, names)
    eci = [_eci("A", "X", 110, 111), _eci("B", "Y", 11, 11)]
    assert tg.gate(complete, eci, names) is None                 # complete: sums must be equal
    few = _sheet([_booth(str(i)) for i in range(1, 5)])
    assert "only 4 booths" in tg.gate(few, eci)


# ------------------------------------------------------------------ the index page
def test_index_links_are_read_not_guessed():
    html = ('<a href="001-Sirpur.pdf">1</a> <a href="005-Asifabad .pdf">5</a> '
            '<a href="INDEX.html">x</a> <a href="002-Chennur.pdf">2</a>')
    entries = parse_index(html, "https://ceotelangana.nic.in/GE_2018/Form-20/INDEX.html")
    assert [e.ac_number for e in entries] == [1, 2, 5]
    assert entries[2].url == "https://ceotelangana.nic.in/GE_2018/Form-20/005-Asifabad%20.pdf"
    assert entries[0].ac_label == "Sirpur"
