"""CEO Uttar Pradesh Form 20 (polling-station-wise result) workbooks.

UP publishes Form 20 as legacy Excel, so no text extraction or OCR is involved:
every number is read from a typed cell. Two layouts occur, sometimes within one
year; the second (the ECI Hindi form) is described above `_parse_hindi`. Layout 1:

    AC No. | Polling Station No. | Name | Total electors | Turnout M F O Total |
    EPIC-identified | Tendered | <candidate: S.No. | Party | Votes Secured> x N |
    Total Votes Secured

The header rows are located by their text, not by fixed row numbers (2012 has an
extra title row). Candidate names sit in the row above the S.No./Party/Votes
triplets. The sheets carry no NOTA or rejected-vote columns, so "Total Votes
Secured" (the candidates' sum as printed) is stored as total valid votes and the
turnout total as total votes.

Station names are Kruti Dev in some years (e.g. 2022) and romanised in others; the
encoding is decided per sheet (Kruti Dev text has very few Latin vowels).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import krutidev
from .form20_2012 import BoothResult, Form20Result

PARSER_NAME = "form20_up_xls"
PARSER_VERSION = "1.2.0"

MIN_DISTINCT_SHARE = 0.75

KRUTIDEV_MAX_VOWEL_SHARE = 0.31
_PS = re.compile(r"^\s*(\d+)\s*[-‐.]?\s*([A-Za-z]{0,2})\s*$")


@dataclass
class Candidate:
    name: str
    party: str | None


@dataclass
class UpForm20Result(Form20Result):
    candidates: list[Candidate] = field(default_factory=list)
    grand_total_valid: int | None = None     # the sheet's own "Grand Total" row, if printed
    names_encoding: str = "latin"            # krutidev | latin


def _num(v) -> int | None:
    """An integer cell value; None for blanks or text."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else None
    s = str(v).strip().replace(",", "")
    return int(s) if s.isdigit() else None


def _text(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return " ".join(str(v).split())


def _vowel_share(names: list[str]) -> float:
    letters = [c for n in names for c in n if c.isascii() and c.isalpha()]
    return sum(c.lower() in "aeiou" for c in letters) / max(len(letters), 1)


def _find_sheet(book, ac_number: int):
    """The sheet named after the AC, else the first sheet whose rows carry that AC number."""
    for sh in book.sheets():
        if sh.name.strip() == str(ac_number):
            return sh
    for sh in book.sheets():
        for r in range(min(sh.nrows, 40)):
            if _num(sh.cell_value(r, 0)) == ac_number and _PS.match(_text(sh.cell_value(r, 1)) or "x"):
                return sh
    return None


class _XlsxSheet:
    """An openpyxl worksheet behind the small xlrd interface the readers use."""

    def __init__(self, ws) -> None:
        self.name = ws.title
        rows = [["" if v is None else v for v in r] for r in ws.iter_rows(values_only=True)]
        self.ncols = max((len(r) for r in rows), default=0)
        self._rows = [r + [""] * (self.ncols - len(r)) for r in rows]
        self.nrows = len(self._rows)

    def row_values(self, r: int) -> list:
        return self._rows[r]

    def cell_value(self, r: int, c: int):
        return self._rows[r][c]


class _XlsxBook:
    def __init__(self, path: Path) -> None:
        import openpyxl
        # data_only: a formula cell yields the value Excel saved with it (None if never
        # calculated -> blank -> the row cannot reconcile and needs review)
        # opened from a handle: some .xlsx files are published with a ".xls" name, and
        # openpyxl refuses a path by its extension
        with open(path, "rb") as fh:
            wb = openpyxl.load_workbook(fh, read_only=True, data_only=True)
            self._sheets = [_XlsxSheet(ws) for ws in wb.worksheets]
            wb.close()

    def sheets(self) -> list[_XlsxSheet]:
        return self._sheets

    def sheet_names(self) -> list[str]:
        return [s.name for s in self._sheets]

    def sheet_by_name(self, name: str) -> _XlsxSheet:
        return next(s for s in self._sheets if s.name == name)


def _open_book(path: Path):
    """Legacy .xls through xlrd, .xlsx through openpyxl — decided by content, not extension."""
    with open(path, "rb") as fh:
        head = fh.read(4)
    if head == b"PK\x03\x04":
        return _XlsxBook(path)
    import xlrd
    return xlrd.open_workbook(str(path), on_demand=True)


def parse_form20_up(path: Path, ac_number: int, year: int | None = None) -> UpForm20Result:
    """Parse one AC's results from a UP Form 20 workbook.

    Every applicable reader is tried — the labelled English layout, the ECI Hindi
    layout, and a structural reader for the many hand-made variants — and the parse
    in which the most booth rows reconcile (candidate votes = printed total) wins.
    Reconciliation, not the reader, decides what is later shown as verified.
    """
    book = _open_book(path)

    def fresh(kind: str, sheet) -> UpForm20Result:
        r = UpForm20Result(ac_number=ac_number, election_year=year or 0, page_count=1)
        r.layout.update(sheet=sheet.name, kind=kind)
        return r

    tries: list[UpForm20Result] = []
    sh = _find_sheet(book, ac_number)
    if sh is not None:
        tries.append(_parse_triplets(sh, ac_number, fresh("english-triplets", sh)))
    hindi = _find_hindi_sheet(book, ac_number)
    if hindi is not None:
        tries.append(_parse_hindi(hindi[0], hindi[1], fresh("hindi-eci", hindi[0])))
    for sheet in _generic_sheets(book, ac_number):
        tries.append(_parse_generic(sheet, ac_number, fresh("structural", sheet)))
    # a reading whose booth numbers repeat heavily is some other table (candidates x rounds)
    tries = [r for r in tries if r.distinct_share >= MIN_DISTINCT_SHARE]
    best = max(tries, key=lambda r: (r.booths_with_matching_sum, len(r.booths)), default=None)
    if best is None or not best.booths:
        res = UpForm20Result(ac_number=ac_number, election_year=year or 0)
        res.warnings.append(f"no readable sheet for AC {ac_number} in {path.name} "
                            f"(sheets: {book.sheet_names()})")
        return res
    return best


def _parse_triplets(sh, ac_number: int, res: UpForm20Result) -> UpForm20Result:

    # --- headers -----------------------------------------------------------
    sub = next((r for r in range(min(sh.nrows, 15))
                if any("votes secured" in _text(v).lower() and "total" not in _text(v).lower()
                       for v in sh.row_values(r))), None)
    if sub is None or sub == 0:
        res.warnings.append("header row with 'Votes Secured' not found")
        return res
    top_row = [_text(v) for v in sh.row_values(sub - 1)]
    sub_row = [_text(v) for v in sh.row_values(sub)]
    vote_cols = [i for i, v in enumerate(sub_row)
                 if "votes secured" in v.lower() and "total" not in v.lower()]
    total_col = next((i for i, v in enumerate(top_row + sub_row)
                      if "total votes" in v.lower().replace("-", "")), None)
    if total_col is not None:
        total_col %= len(top_row)
    turnout_total = next((i for i, v in enumerate(sub_row) if v.lower() == "total"), None)
    tendered = next((i for i, v in enumerate(top_row) if "tendered" in v.lower().replace("-", "")), None)
    electors = 3 if "elector" in top_row[3].lower() or "voter" in top_row[3].lower() else None
    if not vote_cols or total_col is None:
        res.warnings.append("candidate vote columns or 'Total Votes Secured' column not found")
        return res
    # A candidate's name heads its S.No. column, two to the left of Votes Secured.
    names = [top_row[c - 2] if c >= 2 else "" for c in vote_cols]
    res.candidate_column_count = len(vote_cols)
    res.layout.update(header_row=sub, vote_cols=vote_cols, total_col=total_col,
                      turnout_total_col=turnout_total, tendered_col=tendered)

    # --- rows --------------------------------------------------------------
    raw_rows = []
    for r in range(sub + 1, sh.nrows):
        row = sh.row_values(r)
        first = _text(row[0]).lower()
        if first.startswith("grand total") or first == "total":
            res.grand_total_valid = _num(row[total_col])
            break
        # Some sheets leave the AC No. cell blank on a run of rows; once this AC's
        # rows have started, a blank there still belongs to it.
        if _num(row[0]) != ac_number and not (raw_rows and _text(row[0]) == ""):
            continue
        m = _PS.match(_text(row[1]))
        if not m or not _text(row[2]):
            continue
        raw_rows.append((r, int(m.group(1)), m.group(2).upper(), row))
    if not raw_rows:
        res.warnings.append("no polling-station rows found")
        return res

    station_names = [_text(row[2]) for *_, row in raw_rows]
    kruti = _vowel_share(station_names) < KRUTIDEV_MAX_VOWEL_SHARE
    res.names_encoding = "krutidev" if kruti else "latin"
    parties = [_text(raw_rows[0][3][c - 1]) or None for c in vote_cols]
    res.candidates = _finish_candidates([Candidate(name=_clean_name(n) if _is_name_label(n) else "", party=p)
                                         for n, p in zip(names, parties)])

    for (r, part, suffix, row), raw_name in zip(raw_rows, station_names):
        if kruti:
            name, conf = krutidev.convert_with_confidence(raw_name)
        else:
            name, conf = raw_name, 1.0
        if suffix:        # auxiliary station (e.g. 159A): keeps its base part number
            name = f"[{part}{suffix}] {name}".strip()
        cells = [_num(row[c]) for c in vote_cols]
        votes = [v for v in cells if v is not None]       # a blank cell shortens the row -> review
        total_valid = _num(row[total_col])
        res.booths.append(BoothResult(
            part_number=part, polling_station_name=name or None,
            polling_station_name_raw=raw_name or None, candidate_votes=votes,
            total_valid_votes=total_valid,
            tendered_votes=_num(row[tendered]) if tendered is not None else None,
            total_votes=_num(row[turnout_total]) if turnout_total is not None else None,
            sum_matches_total=(len(votes) == len(cells) and total_valid is not None
                               and sum(votes) == total_valid),
            source_page=r + 1, confidence=conf))
    if electors is not None:
        res.total_electors = sum(_num(row[electors]) or 0 for *_, row in raw_rows)

    booth_sum = sum(b.total_valid_votes or 0 for b in res.booths)
    if res.grand_total_valid is not None and res.grand_total_valid != booth_sum:
        res.warnings.append(f"booth totals sum to {booth_sum:,} but the sheet's Grand Total is "
                            f"{res.grand_total_valid:,}")
    aux = sum(1 for *_, s, _ in raw_rows if s)
    if aux:
        res.warnings.append(f"{aux} auxiliary polling station(s) stored under their base part number")
    return res


# --------------------------------------------------------------------------
# Layout 2: the ECI Hindi Form 20 ("pswise" sheet)
#   मतदान स्थल की संख्या | 1&<name> ... N&<name> | वैद्य मतों की कुल | निरस्त मतों |
#   नोटा | योग | निविदत्त
# Rows are grouped by counting round, with subtotal / running-total rows between
# groups (text in the first column); stations are not in numeric order. The
# "मतदेय स्थलों पर अभिलिखित मतों" row is the total over polling stations (postal
# ballots follow it and are not booth rows).
# --------------------------------------------------------------------------
_VALID = "वैद्य"
_REJECTED = "निरस्त"
_NOTA = "नोटा"
_TOTAL = "योग"
_TENDERED = "निविदत्त"
_BOOTH_TOTAL = "मतदेय स्थलों पर"
_AC_TITLE = re.compile(r"^\s*(\d+)\s*-")


def _find_hindi_sheet(book, ac_number: int):
    """(sheet, header row) for an ECI Hindi layout sheet of this AC, else None."""
    for sh in book.sheets():
        # the station-number label is sometimes Unicode, sometimes Kruti Dev: key on the
        # valid-votes column and the numbered "1&<name>" candidate headers instead
        header = next((r for r in range(min(sh.nrows, 15))
                       if any(_VALID in _text(v) for v in sh.row_values(r))
                       and any(re.match(r"^\s*1\s*&", _text(v)) for v in sh.row_values(r))), None)
        if header is None:
            continue
        titles = [_text(sh.cell_value(r, 0)) for r in range(header)]
        numbers = {int(m.group(1)) for t in titles if (m := _AC_TITLE.match(t))}
        numbers |= {int(n) for t in titles for n in re.findall(r"क्रमांक[^\d]*(\d+)", t)}
        if ac_number in numbers:
            return sh, header
    return None


def _parse_hindi(sh, header: int, res: UpForm20Result) -> UpForm20Result:
    hdr = [_text(v) for v in sh.row_values(header)]
    col = {key: next((i for i, v in enumerate(hdr) if key in v), None)
           for key in (_VALID, _REJECTED, _NOTA, _TENDERED)}
    total = next((i for i, v in enumerate(hdr) if v.strip() == _TOTAL), None)
    valid = col[_VALID]
    if valid is None or total is None:
        res.warnings.append("valid-votes or total column not found in the Hindi layout")
        return res
    vote_cols = [i for i in range(1, valid) if re.match(r"^\s*\d+\s*&", hdr[i])]
    if not vote_cols:
        res.warnings.append("no candidate columns found in the Hindi layout")
        return res
    res.candidate_column_count = len(vote_cols)
    res.names_encoding = "krutidev"
    res.candidates = [Candidate(name=_kruti_name(re.sub(r"^\s*\d+\s*&\s*", "", hdr[c]))
                                or f"Candidate {k + 1}", party=None)
                      for k, c in enumerate(vote_cols)]
    res.layout.update(header_row=header, vote_cols=vote_cols, valid_col=valid, total_col=total)

    for r in range(header + 1, sh.nrows):
        row = sh.row_values(r)
        first = _text(row[0])
        if first.startswith(_BOOTH_TOTAL):
            res.grand_total_valid = _num(row[valid])
            break
        m = _PS.match(first)
        if not m:
            continue            # round header, round subtotal or running total
        cells = [_num(row[c]) for c in vote_cols]
        votes = [v for v in cells if v is not None]
        tv = _num(row[valid])
        part, suffix = int(m.group(1)), m.group(2).upper()
        res.booths.append(BoothResult(
            part_number=part, polling_station_name=f"[{part}{suffix}]" if suffix else None,
            polling_station_name_raw=None, candidate_votes=votes, total_valid_votes=tv,
            tendered_votes=_num(row[col[_TENDERED]]) if col[_TENDERED] is not None else None,
            total_votes=_num(row[total]),
            sum_matches_total=len(votes) == len(cells) and tv is not None and sum(votes) == tv,
            source_page=r + 1, confidence=1.0,
            rejected_votes=_num(row[col[_REJECTED]]) if col[_REJECTED] is not None else None,
            nota_votes=_num(row[col[_NOTA]]) if col[_NOTA] is not None else None))
    res.booths.sort(key=lambda b: b.part_number)
    booth_sum = sum(b.total_valid_votes or 0 for b in res.booths)
    if res.grand_total_valid is not None and res.grand_total_valid != booth_sum:
        res.warnings.append(f"booth totals sum to {booth_sum:,} but the sheet's polling-station total is "
                            f"{res.grand_total_valid:,}")
    return res


# --------------------------------------------------------------------------
# Layout 3: structural reader for hand-made variants of layout 1
# Many ROs retyped the English form: the "Votes Secured" label is missing, the
# first column holds a serial or "389 VARANASI", party columns are absent, a NOTA
# or rejected column is appended. The columns are recovered from the data:
#   station number = a column that counts, followed by a text name;
#   candidate votes = numeric columns that vary between rows (S.No. columns are
#   constant, party columns are text), after the electors/turnout/tendered block;
#   total = the column the candidate columns actually sum to.
# --------------------------------------------------------------------------
_PRE_KEYS = ("elector", "voter", "turn", "cast", "male", "female", "men", "women", "other",
             "epic", "identif", "tender", "total no")
# lower-cased; the Kruti Dev spellings of प्रतिक्षेपित / निविदत्त / नोटा are included
_SKIP_KEYS = ("reject", "postal", "tender", "निरस्त", "निविदत्त", "izfr{ksfir", "fufonrr", "fufofnr")
_NOTA_KEYS = ("nota", "नोटा", "none of", "uksvk")
_SNO = re.compile(r"\bs\.?\s?no\b|\bsl\.?\s?no\b")
_GENERIC_HEADERS = {"s.no", "s no", "s. no", "party affiliation", "party affili",
                    "votes secured", "votes", "party", "no"}
_EMPTY = {"", "-", "&", "--", "nil"}


# a label that is a party, not a person ("Samajwadi Party", "Indian National Congress", "IND")
_PARTYISH = re.compile(r"(?i)\b(party|congress|dal|independent|ind|bsp|bjp|sp|inc)\b|पार्टी|कांग्रेस|दल\b")


def _is_name_label(t: str) -> bool:
    """A header cell that can be a candidate's name (not a column label or placeholder)."""
    low = t.lower().strip(" .:")
    return bool(t) and _num(t) is None and low not in _GENERIC_HEADERS and not _SNO.search(low) \
        and not low.startswith(("candidate", "candudate", "name of", "party")) and "votes" not in low


def _clean_name(t: str) -> str:
    t = re.sub(r"^\s*\d+\s*[.)&-]\s*", "", t)          # "1.AKHILESH", "3&..." numbering
    return " ".join(t.split()).strip(" ~`,;")


def _kruti_name(t: str) -> str:
    """Kruti Dev name to Unicode. The converter leaves Kruti punctuation as typed:
    ']' is a comma, '^' and '*' are the opening and closing quote marks."""
    out = krutidev.convert(t)
    out = out.replace("]", ",").replace("^^", "“").replace("**", "”").replace("^", "‘").replace("*", "’")
    return _clean_name(out)


def _finish_candidates(cands: list[Candidate]) -> list[Candidate]:
    """Kruti Dev names (decided over the whole list) converted; blanks become positional."""
    if _vowel_share([c.name for c in cands if c.name]) < KRUTIDEV_MAX_VOWEL_SHARE:
        for c in cands:
            c.name = _kruti_name(c.name)
    return [Candidate(name=c.name or f"Candidate {i + 1}", party=c.party) for i, c in enumerate(cands)]


def _generic_sheets(book, ac_number: int):
    """Sheets the structural reader may use. A workbook whose sheets are named after several
    ACs (a whole district) is only read on the sheet named after this AC."""
    numbered = [s.name.strip() for s in book.sheets() if s.name.strip().isdigit()]
    if str(ac_number) in numbered:
        return [s for s in book.sheets() if s.name.strip() == str(ac_number)]
    if len(numbered) > 1:
        return []
    return [s for s in book.sheets() if s.nrows >= 12]


def _is_data_row(row, pc: int, nc: int | None = None) -> bool:
    """A polling-station row: station number in column `pc`, its name in `nc` (the next
    column, or the one after when the column between holds only a blank or 0)."""
    nc = pc + 1 if nc is None else nc
    if nc + 1 >= len(row) or not _PS.match(_text(row[pc])):
        return False
    if nc == pc + 2 and _text(row[pc + 1]) not in ("", "0"):
        return False
    name = _text(row[nc])
    if not name or _num(row[nc]) is not None:
        return False
    return sum(1 for v in row[nc + 1:] if _num(v) is not None) >= 5


def _station_columns(rows) -> tuple[int, int]:
    """(station-number column, name column) matching the most rows; the station number
    must vary between rows (a column of zeros is not one)."""
    def score(pc: int, nc: int) -> int:
        hits = [r for r in rows if _is_data_row(r, pc, nc)]
        distinct = {_text(r[pc]) for r in hits}
        return len(hits) if len(distinct) * 2 > len(hits) else 0
    return max(((pc, pc + off) for pc in range(3) for off in (1, 2)), key=lambda p: (score(*p), -p[1]))


def _parse_generic(sh, ac_number: int, res: UpForm20Result) -> UpForm20Result:
    rows = [sh.row_values(r) for r in range(sh.nrows)]
    pc, nc = _station_columns(rows)
    data = [(i, r) for i, r in enumerate(rows) if _is_data_row(r, pc, nc)]
    if len(data) < 10:
        res.warnings.append("no polling-station rows found")
        return res
    # a first column that repeats one AC number must repeat this one (a varying first
    # column is a row serial, not an AC number)
    if pc > 0:
        firsts = Counter(_num(r[0]) for _, r in data if _num(r[0]) is not None)
        if firsts:
            value, n = firsts.most_common(1)[0]
            if n * 2 >= len(data) and value != ac_number:
                res.warnings.append(f"sheet belongs to AC {value}, not {ac_number}")
                return res
    first = data[0][0]
    header = [" ".join(_text(rows[h][j]) for h in range(max(0, first - 6), first)).lower()
              for j in range(sh.ncols)]

    def col_kind(j: int) -> str:
        vals = [_text(r[j]) if j < len(r) else "" for _, r in data]
        filled = [v for v in vals if v.lower() not in _EMPTY]
        if not filled:
            return "empty"
        nums = [_num(v) for v in filled]
        if sum(n is not None for n in nums) >= 0.8 * len(filled):
            return "const" if len({n for n in nums if n is not None}) == 1 else "var"
        return "text"

    kinds = {j: col_kind(j) for j in range(nc + 1, sh.ncols)}
    window = range(nc + 1, min(nc + 11, sh.ncols))
    # ("Voters Secured" heads candidate columns in some sheets: not part of the turnout block)
    pre = [j for j in window if any(k in header[j] for k in _PRE_KEYS) and "secur" not in header[j]]
    start = (max(pre) + 1) if pre else nc + 1
    words = {j: [w for w in header[j].split() if not w.replace(".", "").isdigit()] for j in window}
    turnout_total = next((j for j in window if words[j][-1:] == ["total"]), None)   # below "Voters Turnout"
    tendered = next((j for j in window if "tender" in header[j].replace("-", "").replace(" ", "")), None)
    electors = nc + 1 if kinds.get(nc + 1) == "var" else None

    ps_numbers = [_num(r[pc]) for _, r in data]

    def repeats_station_number(j: int) -> bool:
        return sum(_num(r[j]) == n for (_, r), n in zip(data, ps_numbers)) >= 0.8 * len(data)

    region = [j for j in range(start, sh.ncols) if kinds.get(j) == "var"
              and not any(k in header[j] for k in _SKIP_KEYS)
              and not _SNO.search(header[j]) and not repeats_station_number(j)]
    # NOTA is sometimes its own column, sometimes a candidate-style block whose label
    # sits on the block's first column: look at the whole block's header
    nota, prev_j = None, start - 1
    for j in region:
        if any(k in " ".join(header[prev_j + 1:j + 1]) for k in _NOTA_KEYS):
            nota = j
            break
        prev_j = j

    def sum_rate(cols: list[int], total: int) -> float:
        ok = 0
        for _, r in data:
            vals = [_num(r[c]) for c in cols]
            t = _num(r[total])
            ok += t is not None and None not in vals and sum(vals) == t
        return ok / len(data)

    # the total is the column the candidates (with or without NOTA, wherever NOTA is
    # printed) actually sum to
    best: tuple[float, int, list[int]] | None = None
    for t in reversed(region):
        if t == nota:
            continue
        cands_left = [c for c in region if c < t and c != nota]
        options = [cands_left] + ([cands_left + [nota]] if nota is not None else [])
        for cols in options:
            if len(cols) < 2:
                continue
            rate = sum_rate(cols, t)
            if best is None or rate > best[0]:
                best = (rate, t, cols)
    if best is None or best[0] < 0.5:
        res.warnings.append("candidate columns do not reconcile with any total column")
        return res
    _, total_col, chosen = best
    total_has_nota = nota is not None and nota in chosen
    vote_cols = [c for c in chosen if c != nota]
    res.candidate_column_count = len(vote_cols)
    res.layout.update(ps_col=pc, vote_cols=vote_cols, total_col=total_col, total_includes_nota=total_has_nota,
                      turnout_total_col=turnout_total, tendered_col=tendered, nota_col=nota)

    # Candidate names: the header row that labels the most candidate blocks (the upper one on
    # a tie — a party row sits below the names); party: the first data row's text in the block.
    blocks, prev = [], start - 1
    for c in vote_cols:
        blocks.append(range(prev + 1, c + 1))
        prev = c
    name_rows = list(range(max(0, first - 6), first))

    def label_in(h: int, block) -> str:
        return next((_clean_name(_text(rows[h][j])) for j in block if _is_name_label(_text(rows[h][j]))), "")

    def row_score(h: int) -> tuple[int, int]:
        labels = [label_in(h, b) for b in blocks]
        partyish = sum(1 for lb in labels if _PARTYISH.search(lb) and "," not in lb)
        return sum(bool(lb) for lb in labels) - partyish, -h

    names_row = max(name_rows, key=row_score, default=None)
    cands = []
    for b in blocks:
        label = label_in(names_row, b) if names_row is not None else ""
        party = next((_text(data[0][1][j]) for j in b if kinds.get(j) == "text"), None)
        cands.append(Candidate(name=label, party=party))
    res.candidates = _finish_candidates(cands)

    station_names = [_text(r[nc]) for _, r in data]
    kruti = _vowel_share(station_names) < KRUTIDEV_MAX_VOWEL_SHARE and not any(
        "ऀ" <= ch <= "ॿ" for n in station_names[:20] for ch in n)
    res.names_encoding = "krutidev" if kruti else "latin"
    aux = 0
    for (i, r), raw_name in zip(data, station_names):
        m = _PS.match(_text(r[pc]))
        part, suffix = int(m.group(1)), m.group(2).upper()
        name, conf = krutidev.convert_with_confidence(raw_name) if kruti else (raw_name, 1.0)
        if suffix:
            aux += 1
            name = f"[{part}{suffix}] {name}".strip()
        cells = [_num(r[c]) for c in vote_cols]
        votes = [v for v in cells if v is not None]
        nota_v = _num(r[nota]) if nota is not None else None
        tv = _num(r[total_col])
        if total_has_nota:
            # the printed total counts NOTA; candidates' valid votes are that total less NOTA
            tv = tv - nota_v if tv is not None and nota_v is not None else None
        res.booths.append(BoothResult(
            part_number=part, polling_station_name=name or None, polling_station_name_raw=raw_name or None,
            candidate_votes=votes, total_valid_votes=tv,
            tendered_votes=_num(r[tendered]) if tendered is not None else None,
            total_votes=_num(r[turnout_total]) if turnout_total is not None else None,
            sum_matches_total=len(votes) == len(cells) and tv is not None and sum(votes) == tv,
            source_page=i + 1, confidence=conf, nota_votes=nota_v))
    if electors is not None:
        res.total_electors = sum(_num(r[electors]) or 0 for _, r in data)
    if aux:
        res.warnings.append(f"{aux} auxiliary polling station(s) stored under their base part number")
    return res
