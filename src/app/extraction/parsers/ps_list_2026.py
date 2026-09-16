"""Parser for the CEO Uttarakhand *Polling Station List 2026* PDFs.

Source: `https://election.uk.gov.in/PSSIR2026/{acNumber}.pdf`

These are Excel sheets printed to PDF, so the reading order of the raw text is
scrambled and the Hindi is in legacy Kruti Dev encoding. Both problems are solved
the same way as for the roll: bucket words geometrically, then transliterate.

Five columns (per the printed header row "1 2 3 4 5"):

    1  part / polling-station number
    2  part name (village / locality)
    3  polling station building name
    4  areas covered
    5  who the station serves ("सभी के लिये" = for everyone)

Column x-ranges are discovered per page rather than hard-coded, because the
printed column widths differ between ACs.
"""
from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .krutidev import convert_with_confidence, looks_like_krutidev

log = logging.getLogger(__name__)

PARSER_NAME = "ps_list_2026_geometric"
# 2.0.0: column boundaries derived from the body (1.x mis-split columns). Stations
# enriched by an older version are re-parsed from the stored PDF by the pipeline.
PARSER_VERSION = "2.1.0"      # 2.1.0: columns from the drawn grid, fallback to 2.0 heuristic

HEADER_MARKERS = {"1", "2", "3", "4", "5"}
ROW_TOLERANCE = 6.0


@dataclass
class PollingStationRow:
    part_number: int
    part_name: str | None
    part_name_raw: str | None
    station_name: str | None
    station_name_raw: str | None
    areas: str | None
    areas_raw: str | None
    serves: str | None
    source_page: int
    confidence: float

    @property
    def station_full(self) -> str | None:
        """part_name + station_name joined.

        The source is an Excel sheet printed to PDF with merged and wrapped
        cells, so the boundary between "locality" and "building" is not reliably
        recoverable from geometry alone. Callers that need a single human-readable
        label should use this rather than trusting the split.
        """
        parts = [p for p in (self.part_name, self.station_name) if p]
        return " ".join(parts) or None


@dataclass
class PsListResult:
    ac_number: int | None
    stations: list[PollingStationRow] = field(default_factory=list)
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)


def _find_column_edges(words: list) -> tuple[list[float], float]:
    """Infer column left-edges AND the y of the printed "1 2 3 4 5" header row.

    The header y matters: the digits 1-5 also occur as part numbers in the body,
    so the body start must come from the header row itself, not from the last
    word that happens to equal one of those digits.
    """
    # Printed digits on one line can differ by a point or two in y, and some lists
    # omit the "1": accept "1 2 3 4 5" or "2 3 4 5" within a 3pt band (None for a missing "1").
    # Some lists continue the column numbering from another table ("9 10 11 12 13").
    # A run of exactly five consecutive numbers is the header; a longer run means a
    # different table layout (e.g. an extra distance column), which is not guessed.
    digits = sorted((w for w in words if w[4].strip().isdigit() and len(w[4].strip()) <= 2),
                    key=lambda w: w[1])
    lines: list[list] = []
    for w in digits:
        if lines and abs(w[1] - lines[-1][0][1]) < 3.0:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        items = sorted(line, key=lambda w: w[0])
        nums = [int(w[4]) for w in items]
        y = min(w[1] for w in items)
        runs: list[list[int]] = []
        for i, n in enumerate(nums):
            if runs and n == nums[runs[-1][-1]] + 1:
                runs[-1].append(i)
            else:
                runs.append([i])
        for run in runs:
            if len(run) == 5:
                return [items[i][0] for i in run], y
            if len(run) == 4 and nums[run[0]] == 2:
                return [None] + [items[i][0] for i in run], y
    return [], 0.0


COLUMN_NAMES = ["part_number", "part_name", "station_name", "areas", "serves"]


def _grid_ranges(page: pymupdf.Page, header: list[float]) -> list[tuple[str, float, float]] | None:
    """Column ranges from the table's drawn vertical rules, bracketing each header digit.

    Every published list is an Excel sheet printed with cell borders, so the rules are
    the most reliable column boundaries whatever the column widths or header placement.
    Returns None when the page has no usable grid (the body-based fallback is used).
    """
    xs: list[float] = []
    for g in page.get_drawings():
        for it in g["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.x - b.x) < 1.5 and abs(a.y - b.y) > 8:
                    xs.append((a.x + b.x) / 2)
            elif it[0] == "re":
                r = it[1]
                if r.width < 2.5 and r.height > 8:
                    xs.append((r.x0 + r.x1) / 2)
    edges: list[list[float]] = []
    for x in sorted(xs):
        if edges and x - edges[-1][-1] < 3.0:
            edges[-1].append(x)
        else:
            edges.append([x])
    lines = [sum(e) / len(e) for e in edges]
    ranges = []
    for name, h in zip(COLUMN_NAMES, header):
        if h is None:                      # "1" not printed: first column ends at the next column's rule
            ranges.append((name, 0.0, 0.0))
            continue
        left = [x for x in lines if x < h]
        right = [x for x in lines if x > h]
        if not left or not right:
            return None
        ranges.append((name, max(left) - 2.0, min(right) - 2.0))
    if header[0] is None:
        ranges[0] = (ranges[0][0], 0.0, ranges[1][1])
    # Columns must be contiguous and in order, otherwise the grid is not the table's.
    if any(ranges[i][2] > ranges[i + 1][1] + 4.0 for i in range(len(ranges) - 1)):
        return None
    ranges[-1] = (ranges[-1][0], ranges[-1][1], 10_000.0)
    ranges[0] = (ranges[0][0], 0.0, ranges[0][2])
    return ranges


def _column_ranges(words: list, header: list[float], header_y: float) -> list[tuple[str, float, float]]:
    """[lo, hi) x-range of each of the five columns on one page.

    The printed "1 2 3 4 5" header digits are CENTRED over their columns. Using each
    digit's x as the column's left edge shifts every boundary to the right: the
    locality column loses its first word, building names absorb the start of the
    village list, and the rest of the village list lands in the areas column next
    to "सभी के लिये". Boundaries therefore come from the body itself — the part
    numbers end column 1, the numbered village entries ("1-", "2-") start column 4
    — and the remaining edges mirror those around the header centres.
    """
    body = [w for w in words if w[1] > header_y + 4.0]
    digits = [w for w in body if w[0] < (header[0] + header[1]) / 2 and w[4].strip().isdigit()]
    b12 = max(w[2] for w in digits) + 2.0 if digits else (header[0] + header[1]) / 2
    # Village entries are numbered "1-" (Kruti Dev "&" also renders as a hyphen).
    enum = [w[0] for w in body
            if header[2] < w[0] < header[4] and re.match(r"^\d{1,2}[-&]", w[4].strip())]
    b34 = min(enum) - 2.0 if enum else (header[2] + header[3]) / 2
    b23 = 2 * header[2] - b34
    if not (header[1] < b23 < header[2]):
        b23 = (header[1] + header[2]) / 2
    b45 = 2 * header[3] - b34
    if not (header[3] < b45 < header[4]):
        b45 = (header[3] + header[4]) / 2
    edges = [0.0, b12, b23, b34, b45, 10_000.0]
    names = ["part_number", "part_name", "station_name", "areas", "serves"]
    return [(nm, edges[i], edges[i + 1]) for i, nm in enumerate(names)]


def _reading_order(items: list[tuple[float, float, str]]) -> str:
    """Join (y, x, text) words line by line; words within 4pt of y share a line."""
    lines: list[list[tuple[float, float, str]]] = []
    for it in sorted(items):
        if lines and abs(it[0] - lines[-1][0][0]) < 4.0:
            lines[-1].append(it)
        else:
            lines.append([it])
    return " ".join(t for line in lines for _, _, t in sorted(line, key=lambda w: w[1]))


def _convert(raw: str | None) -> tuple[str | None, str | None, float]:
    """Return (converted, raw, confidence); leave already-Unicode text alone."""
    if not raw or not raw.strip():
        return None, None, 1.0
    raw = re.sub(r"\s+", " ", raw).strip()
    has_devanagari = any("ऀ" <= c <= "ॿ" for c in raw)
    has_ascii_alpha = any(c.isascii() and c.isalpha() for c in raw)
    if has_devanagari or not has_ascii_alpha:
        if not looks_like_krutidev(raw):
            return raw, raw, 1.0
    out, conf = convert_with_confidence(raw)
    return out or None, raw, conf


def parse_ps_list_pdf(path: Path, ac_number: int | None = None) -> PsListResult:
    result = PsListResult(ac_number=ac_number)
    with pymupdf.open(path) as doc:
        result.page_count = doc.page_count
        last_edges: list | None = None
        last_ranges: list | None = None
        for pno in range(doc.page_count):
            page = doc[pno]
            words = page.get_text("words")
            edges, header_y = _find_column_edges(words)
            if len(edges) < 5:
                # Continuation pages often do not repeat the header row: reuse the last
                # page's column layout (re-fitted to this page's grid when it has one).
                if last_edges is None:
                    result.warnings.append(f"page {pno + 1}: column header row not found")
                    continue
                edges, header_y = last_edges, 30.0
                ranges = _grid_ranges(page, edges) or last_ranges
            else:
                ranges = _grid_ranges(page, edges)
                if ranges is None:
                    if edges[0] is None:
                        result.warnings.append(f"page {pno + 1}: incomplete header and no usable grid")
                        continue
                    ranges = _column_ranges(words, edges, header_y)
            last_edges, last_ranges = edges, ranges
            body = [w for w in words if w[1] > header_y + 4.0]

            def col_of(x: float) -> str | None:
                for nm, lo, hi in ranges:
                    if lo <= x < hi:
                        return nm
                return None

            anchors = sorted(
                (w[1], int(w[4])) for w in body
                if col_of(w[0]) == "part_number" and w[4].strip().isdigit()
            )
            if not anchors:
                continue
            anchor_ys = [a[0] for a in anchors]
            buckets: list[dict[str, list[tuple[float, float, str]]]] = [{} for _ in anchors]
            # Cells are top-aligned in most lists but vertically centred in some, where a row's
            # text starts above its serial number. Measure that lead instead of assuming zero.
            text_ys = [w[1] for w in body if col_of(w[0]) in ("part_name", "station_name")]
            leads = []
            for ay in anchor_ys:
                above = [ay - y for y in text_ys if 0.0 <= ay - y <= 20.0]
                if above:
                    leads.append(min(above))
            lead = statistics.median(leads) if leads else 0.0

            for x0, y0, x1, y1, text, *_ in body:
                col = col_of(x0)
                if col is None or not text.strip():
                    continue
                idx = None
                for i, ay in enumerate(anchor_ys):
                    if y0 >= ay - ROW_TOLERANCE - lead:
                        idx = i
                    else:
                        break
                if idx is None:
                    continue
                buckets[idx].setdefault(col, []).append((y0, x0, text))

            for (ay, part_no), b in zip(anchors, buckets):
                def cell(name: str) -> str | None:
                    items = b.get(name)
                    if not items:
                        return None
                    return _reading_order(items)

                pname, pname_raw, c1 = _convert(cell("part_name"))
                sname, sname_raw, c2 = _convert(cell("station_name"))
                areas, areas_raw, c3 = _convert(cell("areas"))
                serves, _, c4 = _convert(cell("serves"))
                result.stations.append(PollingStationRow(
                    part_number=part_no,
                    part_name=pname, part_name_raw=pname_raw,
                    station_name=sname, station_name_raw=sname_raw,
                    areas=areas, areas_raw=areas_raw,
                    serves=serves,
                    source_page=pno + 1,
                    confidence=round(min(c1, c2, c3, c4), 4),
                ))

    # Printed sheets repeat a part across page breaks; keep the richest row.
    best: dict[int, PollingStationRow] = {}
    for row in result.stations:
        cur = best.get(row.part_number)
        if cur is None or len(str(row.station_name or "")) > len(str(cur.station_name or "")):
            best[row.part_number] = row
    result.stations = [best[k] for k in sorted(best)]
    if not result.stations:
        result.warnings.append("no polling-station rows were extracted")
    return result
