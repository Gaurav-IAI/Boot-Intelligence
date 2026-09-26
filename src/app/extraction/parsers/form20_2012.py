"""Parser for Form 20 (Final Result Sheet), Vidhan Sabha 2012, Uttarakhand.

Source: `https://election.uk.gov.in/Vidhan_sabha2012/form20_2012_PDF/{nn}-{Name}.pdf`

Of the three Vidhan Sabha Form 20 sets published by CEO Uttarakhand this is the
only one with a usable text layer — 2022 is a pure scan and 2017's embedded OCR
layer is corrupted. The text is Kruti Dev.

Every returning officer produced the sheet differently, so no column position is
hard-coded. Layouts differ in page rotation, in overprinted "bold" text (a word
drawn four times, so "18" can arrive as "18181818"), in serials glued to the
station name ("18-jktdh;"), and in the summary columns after the candidates
(AC 19 prints valid, rejected, total, tendered). The parser therefore:

  1. reads words in reading orientation, drops overprinted duplicates, undoes 4x
     merged digits and splits serial-prefixed tokens;
  2. takes the left-most numeric column as the booth serial (reusing it on pages
     with too few booths to detect it);
  3. decides the vote columns for the WHOLE document: numeric columns present in
     at least 30% of booths. Digits inside station names ("क0नं0 1") never line
     up that consistently, so they stay part of the name;
  4. locates TOTAL VALID VOTES arithmetically — the column whose value equals the
     sum of the cells to its left in most booths;
  5. names the other summary columns only from their printed headers (rejected /
     tendered / total), one header per column; a sparse column (tendered votes are
     often blank) counts only when its header sits directly above it. "total" is
     kept only when it equals valid + rejected in most booths.

Candidate names are printed as rotated headers detached from their columns, so
they are NOT attributed. Every booth carries the document's own arithmetic check.
"""
from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .krutidev import convert_with_confidence

log = logging.getLogger(__name__)

PARSER_NAME = "form20_2012_geometric"
PARSER_VERSION = "2.0.0"        # 2.0.0: layout detected per document (1.x assumed AC 19's)

ROW_TOLERANCE = 10.0
COLUMN_TOLERANCE = 6.0
DENSE_SHARE = 0.3
# Kruti Dev spellings of the summary-column headers.
HEADER_KEYS = {
    "rejected": ("izfr{ksfir",),                     # प्रतिक्षेपित
    "tendered": ("fufonRr", "fufofnr", "fufonr"),   # निविदत्त
    "total": (";ksx",),                             # योग
}

Word = tuple[float, float, float, float, str]


@dataclass
class BoothResult:
    part_number: int
    polling_station_name: str | None
    polling_station_name_raw: str | None
    candidate_votes: list[int]
    total_valid_votes: int | None
    tendered_votes: int | None
    total_votes: int | None
    sum_matches_total: bool
    source_page: int
    confidence: float
    rejected_votes: int | None = None
    nota_votes: int | None = None


@dataclass
class Form20Result:
    ac_number: int | None = None
    ac_name: str | None = None
    election_year: int = 2012
    total_electors: int | None = None
    candidate_column_count: int | None = None
    booths: list[BoothResult] = field(default_factory=list)
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)
    layout: dict = field(default_factory=dict)

    @property
    def booths_with_matching_sum(self) -> int:
        return sum(1 for b in self.booths if b.sum_matches_total)

    @property
    def sum_check_rate(self) -> float:
        return self.booths_with_matching_sum / len(self.booths) if self.booths else 0.0

    @property
    def serial_coverage(self) -> float:
        highest = max((b.part_number for b in self.booths), default=0)
        return len(self.booths) / highest if highest > 0 else 0.0

    @property
    def distinct_share(self) -> float:
        """Distinct booths per row. An auxiliary station keeps its base number with its label
        ("[12A] ...") in the name, so it counts as its own booth; a table whose 'booth
        numbers' repeat heavily (a candidate-by-round table) is not a booth sheet, even
        when its arithmetic happens to reconcile."""
        def key(b: BoothResult) -> tuple:
            m = re.match(r"\[(\w+)\]", b.polling_station_name or "")
            return b.part_number, m.group(1) if m else ""
        return len({key(b) for b in self.booths}) / len(self.booths) if self.booths else 0.0


def _visual_words(page: pymupdf.Page) -> list[Word]:
    """Words in reading orientation: overprints removed, merged digits and serials split."""
    matrix = page.rotation_matrix
    seen: set[tuple] = set()
    out: list[Word] = []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        t = text.strip()
        if not t:
            continue
        r = pymupdf.Rect(x0, y0, x1, y1) * matrix
        key = (t, round(r.x0 / 3), round(r.y0 / 3))
        if key in seen:
            continue
        seen.add(key)
        m = re.fullmatch(r"(\d{1,3})\1{3}", t)          # "18181818" -> "18"
        if m:
            t = m.group(1)
        m = re.match(r"^(\d{1,3})[-&.](.*)$", t)          # "18-jktdh;" -> "18", "jktdh;"
        if m:
            cut = r.x0 + (r.x1 - r.x0) * (len(m.group(1)) + 1) / len(t)
            out.append((r.x0, r.y0, cut, r.y1, m.group(1)))
            if m.group(2).strip():
                out.append((cut, r.y0, r.x1, r.y1, m.group(2)))
            continue
        out.append((r.x0, r.y0, r.x1, r.y1, t))
    return out


def _clusters(values: list[float], tol: float) -> list[list[float]]:
    groups: list[list[float]] = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return groups


@dataclass
class _Row:
    page: int
    serial: int
    y: float
    tokens: list[Word] = field(default_factory=list)
    name_words: list[tuple[float, float, str]] = field(default_factory=list)
    cells: list[tuple[float, float, int]] = field(default_factory=list)     # (x0, x1, value)


def _page_rows(words: list[Word], page_no: int, page_width: float,
               serial_hint: tuple[float, float] | None) -> tuple[list[_Row], tuple[float, float] | None]:
    """Rows anchored on booth serials; every other token on the row is kept for later.

    With a `serial_hint` (the document's serial column) that range is used as-is:
    a page holding only a few booths plus a totals row must not redefine it.
    """
    left = [w for w in words if w[4].isdigit() and w[0] < page_width * 0.3]
    if serial_hint is not None:
        s_range = serial_hint
    else:
        group = next((g for g in _clusters([w[2] for w in left], COLUMN_TOLERANCE) if len(g) >= 3), None)
        if group is None:
            return [], None
        s_range = (group[0] - COLUMN_TOLERANCE, group[-1] + COLUMN_TOLERANCE)
    s_lo, s_hi = s_range
    serial_words = [w for w in left if s_lo <= w[2] <= s_hi]
    if not serial_words:
        return [], s_range
    rows = [_Row(page_no, int(w[4]), w[1]) for w in sorted(serial_words, key=lambda w: w[1])]
    ys = [r.y for r in rows]
    for w in words:
        if w in serial_words or w[0] <= s_hi:
            continue
        i = min(range(len(ys)), key=lambda k: abs(w[1] - ys[k]))
        if abs(w[1] - ys[i]) <= ROW_TOLERANCE * 2:
            rows[i].tokens.append(w)
    return rows, s_range


def _header_positions(words: list[Word], first_row_y: float) -> dict[str, list[float]]:
    found: dict[str, list[float]] = {}
    for x0, y0, x1, y1, t in words:
        if y0 >= first_row_y:
            continue
        for key, spellings in HEADER_KEYS.items():
            if any(s in t for s in spellings):
                found.setdefault(key, []).append((x0 + x1) / 2)
    return found


def parse_form20_2012(path: Path, ac_number: int | None = None) -> Form20Result:
    result = Form20Result(ac_number=ac_number)
    rows: list[_Row] = []
    headers: dict[str, list[float]] = {}
    serial_hint: tuple[float, float] | None = None
    with pymupdf.open(path) as doc:
        result.page_count = doc.page_count
        first = doc[0].get_text()
        m = re.search(r"…+\s*(\d+)\s*([^…]+?)…", first)
        if m:
            result.ac_number = result.ac_number or int(m.group(1))
            result.ac_name = convert_with_confidence(m.group(2).strip())[0]
        m = re.search(r"fuokZpdksa dh dqy la\[;k[^\d]*(\d+)", first)
        if m:
            result.total_electors = int(m.group(1))
        pages = [(pno + 1, _visual_words(doc[pno]), doc[pno].rect.width) for pno in range(doc.page_count)]
    # The document's serial column: the range most pages with several booths agree on.
    ranges: Counter = Counter()
    for page_no, words, width in pages:
        page_rows, s_range = _page_rows(words, page_no, width, None)
        if len(page_rows) >= 3 and s_range:
            ranges[(round(s_range[0]), round(s_range[1]))] += 1
    if ranges:
        serial_hint = tuple(float(v) for v in ranges.most_common(1)[0][0])
    for page_no, words, width in pages:
        page_rows, _ = _page_rows(words, page_no, width, serial_hint)
        if page_rows:
            for k, xs in _header_positions(words, min(r.y for r in page_rows)).items():
                headers.setdefault(k, []).extend(xs)
        rows += page_rows

    if not rows:
        result.warnings.append("no booth rows were extracted")
        return result

    # 3. Vote columns for the whole document.
    digit_x1 = [w[2] for r in rows for w in r.tokens if w[4].isdigit()]
    dense = [g for g in _clusters(digit_x1, COLUMN_TOLERANCE) if len(g) >= DENSE_SHARE * len(rows)]
    if not dense:
        result.warnings.append("no numeric column is present in most booths; layout not recognised")
        return result
    first_x1 = dense[0][0] - COLUMN_TOLERANCE
    vote_x0_min = min(w[0] for r in rows for w in r.tokens if w[4].isdigit() and w[2] >= first_x1) - 2.0
    kept_rows = []
    for r in rows:
        r.cells = [(w[0], w[2], int(w[4])) for w in r.tokens if w[4].isdigit() and w[0] >= vote_x0_min]
        r.name_words = [(w[1], w[0], w[4]) for w in r.tokens if w[2] < vote_x0_min]
        values = [v for _, _, v in sorted(r.cells, key=lambda c: c[1])]
        if len(values) >= 3 and values == list(range(values[0], values[0] + len(values))):
            continue                                   # the printed "1 2 3 ..." column-number row
        if values:
            kept_rows.append(r)
    rows = kept_rows
    if not rows:
        result.warnings.append("no booth rows with vote cells")
        return result

    # 4. Total-valid column: where a cell equals the sum of the cells to its left.
    split_x: list[float] = []
    for r in rows:
        running = 0
        for i, (x0, x1, v) in enumerate(sorted(r.cells, key=lambda c: c[1])):
            if i >= 2 and v > 0 and v == running:
                split_x.append(x1)
            running += v
    if not split_x:
        result.warnings.append("no column satisfies candidate votes = total valid votes; layout not recognised")
        return result
    best = max(_clusters(split_x, COLUMN_TOLERANCE), key=len)
    valid_x = statistics.median(best)

    # 5. Summary columns right of total-valid, named from their headers.
    header_x = {k: statistics.median(v) for k, v in headers.items()}
    rest_centres = [(c[0] + c[1]) / 2 for r in rows for c in r.cells if c[1] > valid_x + COLUMN_TOLERANCE]
    columns = []
    for g in _clusters(rest_centres, 10.0):
        cx = statistics.median(g)
        if len(g) >= DENSE_SHARE * len(rows) or any(abs(hx - cx) <= 25.0 for hx in header_x.values()):
            columns.append(cx)
    pairs = sorted((abs(hx - cx), key, ci) for key, hx in header_x.items()
                   for ci, cx in enumerate(columns) if abs(hx - cx) <= 45.0)
    names: dict[int, str] = {}
    for _, key, ci in pairs:
        if ci not in names and key not in names.values():
            names[ci] = key

    booths: list[BoothResult] = []
    for r in rows:
        cells = sorted(r.cells, key=lambda c: c[1])
        valid = [v for _, x1, v in cells if abs(x1 - valid_x) <= COLUMN_TOLERANCE]
        if not valid:
            continue
        cand = [v for _, x1, v in cells if x1 < valid_x - COLUMN_TOLERANCE]
        summary: dict[str, int] = {}
        for x0, x1, v in cells:
            if x1 <= valid_x + COLUMN_TOLERANCE or not columns:
                continue
            ci = min(range(len(columns)), key=lambda k: abs(columns[k] - (x0 + x1) / 2))
            if abs(columns[ci] - (x0 + x1) / 2) <= 12.0 and ci in names:
                summary.setdefault(names[ci], v)
        raw_name = " ".join(t for _, _, t in sorted(r.name_words, key=lambda w: (round(w[0] / 4), w[1]))).strip() or None
        name, conf = convert_with_confidence(raw_name) if raw_name else (None, 0.0)
        booths.append(BoothResult(
            part_number=r.serial, polling_station_name=name, polling_station_name_raw=raw_name,
            candidate_votes=cand, total_valid_votes=valid[0],
            tendered_votes=summary.get("tendered"), total_votes=summary.get("total"),
            rejected_votes=summary.get("rejected"),
            sum_matches_total=bool(cand) and sum(cand) == valid[0],
            source_page=r.page, confidence=round(conf, 4)))

    # A booth printed across a page break can appear twice; keep the richer row.
    kept: dict[int, BoothResult] = {}
    for b in booths:
        cur = kept.get(b.part_number)
        if cur is None or len(b.candidate_votes) > len(cur.candidate_votes):
            kept[b.part_number] = b
    result.booths = [kept[k] for k in sorted(kept)]

    # A grand-totals row sits in the serial column with a vote count as its "serial".
    if result.booths:
        median_serial = statistics.median(b.part_number for b in result.booths)
        limit = max(3 * median_serial, median_serial + 400)
        outliers = [b.part_number for b in result.booths if b.part_number > limit]
        if outliers:
            result.booths = [b for b in result.booths if b.part_number <= limit]
            result.warnings.append(f"ignored {len(outliers)} row(s) with an implausible serial "
                                   f"{outliers[:3]} (e.g. a totals row)")

    # "total" must equal valid + rejected in most booths, or it is not trusted.
    with_total = [b for b in result.booths if b.total_votes is not None]
    if with_total:
        agree = sum(1 for b in with_total
                    if b.total_votes == (b.total_valid_votes or 0) + (b.rejected_votes or 0))
        if agree < 0.8 * len(with_total):
            for b in result.booths:
                b.total_votes = None
            result.warnings.append("column headed 'total' does not equal valid + rejected; total not stored")

    result.layout = {"total_valid_x": round(valid_x, 1), "identity_rows": len(best), "rows": len(rows),
                     "vote_x0_min": round(vote_x0_min, 1),
                     "summary_columns": {names.get(i, f"unassigned@{round(x)}"): round(x, 1)
                                         for i, x in enumerate(columns)}}
    widths = Counter(len(b.candidate_votes) for b in result.booths)
    if len(widths) == 1:
        result.candidate_column_count = next(iter(widths))
    elif widths:
        result.warnings.append(
            f"candidate column count varies across booths: {sorted(widths)} — "
            "rows where a zero-vote column was not printed cannot be aligned")
    bad = [b.part_number for b in result.booths if not b.sum_matches_total]
    if bad:
        result.warnings.append(
            f"{len(bad)} of {len(result.booths)} booths: candidate votes do not sum "
            f"to the printed total valid votes (e.g. parts {bad[:5]})")
    return result
