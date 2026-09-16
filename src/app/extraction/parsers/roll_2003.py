"""Parser for CEO Uttarakhand Legacy Electoral Roll 2003 PDFs.

Layout (verified against real pages — see docs/UTTARAKHAND_ROLL_FORMAT.md):

  header block, repeated on every page
      निर्वाचक नामावली-2003   S28 उत्तराखंड
      पुनरीक्षण का वर्ष-2003 / अर्हता तिथि :- 01.01.2003
      भाग संख्या -6
      विधान सभा निर्वाचन क्षेत्र की संख्या व नाम :- 15 - राजपुर
      मतदेय स्थल की संख्या तथा नाम :- 6 प्राथमिक स्कूल अस्थल     (page 1 only)
      मतदेय स्थल में सम्मिलित ग्राम/गली/मुहल्ला :- 1 रैनी वाला, ...  (page 1 only)

  eight fixed columns
      (1) क्रम संख्या   (2) मकान संख्या  (3) निर्वाचक का नाम  (4) सम्बन्ध
      (5) सम्बन्धी का नाम (6) लिंग        (7) आयु             (8) फोटो पहचान पत्र संख्या

Parsing is geometric, not line-based: words are bucketed into columns by their x
position and into rows by the y position of the serial number. That survives the
multi-line name cells that appear in the larger parts, which a naive
line-splitting parser silently mangles.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .devanagari_fix import clean, clean_with_confidence

log = logging.getLogger(__name__)

PARSER_NAME = "roll_2003_geometric"

# Column boundaries in PDF points, measured from real pages. A word belongs to a
# column when its left edge falls inside the interval.
COLUMNS: list[tuple[str, float, float]] = [
    ("serial",   40.0, 88.0),
    ("house",    88.0, 125.0),
    ("name",     125.0, 226.0),
    ("relation", 226.0, 272.0),
    ("relative", 272.0, 356.0),
    ("gender",   356.0, 396.0),
    ("age",      396.0, 428.0),
    ("epic",     428.0, 560.0),
]

# The table body is bounded per page, not by fixed coordinates: continuation
# pages carry a shorter header block, and the footnote ("कॉलम-4 सम्बन्ध कोड- ...")
# sits in the same x range as the serial column, so it must be cut off or it is
# read as part of the last row.
BODY_TOP_FALLBACK = 235.0
BODY_BOTTOM_FALLBACK = 800.0
ROW_TOLERANCE = 9.0
# The footnote block starts at x=42.5 — left of the serial column's x=53.7 —
# so it is identified by position AND text. Matching on text alone wrongly
# truncated pages where an elector name happened to share a marker's prefix.
FOOTNOTE_X_MAX = 50.0
FOOTNOTE_MARKER = "कॉलम"
COLUMN_NUMBER_ROW = {"(1)", "(2)", "(3)", "(4)", "(5)", "(6)", "(7)", "(8)"}

GENDER_MAP = {
    "पुरुष": "M", "पुरूष": "M", "पु": "M", "पुŜष": "M",
    "महिला": "F", "मिहला": "F", "म": "F", "स्त्री": "F",
    "अन्य": "O", "तृतीय": "O",
}

RELATION_MAP = {
    "पिता": "FATHER", "िपता": "FATHER", "पि": "FATHER",
    "माता": "MOTHER", "मा": "MOTHER",
    "पति": "HUSBAND", "पित": "HUSBAND", "प": "HUSBAND",
    "अन्य": "OTHER", "अɊ": "OTHER",
}

_NULLISH = {"", "null", "none", "-", "--"}


@dataclass
class RollHeader:
    roll_year: int | None = None
    qualifying_date: str | None = None
    part_number: int | None = None
    ac_number: int | None = None
    ac_name: str | None = None
    state_code: str | None = None
    polling_station_number: int | None = None
    polling_station_name: str | None = None
    areas: str | None = None
    revision_type: str | None = None


@dataclass
class ElectorRow:
    serial_number: int | None
    house_number: str | None
    elector_name: str | None
    elector_name_raw: str | None
    relative_name: str | None
    relative_name_raw: str | None
    relative_type: str | None
    gender: str | None
    age: int | None
    epic_number: str | None
    source_page: int
    raw_text: str
    confidence: float


@dataclass
class RollParseResult:
    header: RollHeader
    electors: list[ElectorRow] = field(default_factory=list)
    page_count: int = 0
    pages_with_rows: int = 0
    warnings: list[str] = field(default_factory=list)


def _norm(v: str | None) -> str | None:
    if v is None:
        return None
    v = clean(v)
    return None if v.strip().lower() in _NULLISH else v.strip()


def _to_int(v: str | None) -> int | None:
    if not v:
        return None
    m = re.search(r"\d+", v.replace(",", ""))
    return int(m.group(0)) if m else None


def _map_gender(v: str | None) -> str | None:
    if not v:
        return None
    c = clean(v).strip().rstrip(".")
    if c in GENDER_MAP:
        return GENDER_MAP[c]
    for k, g in GENDER_MAP.items():
        if c.startswith(k):
            return g
    return "UNKNOWN"


def _map_relation(v: str | None) -> str | None:
    if not v:
        return None
    c = clean(v).strip().rstrip(".")
    if c in RELATION_MAP:
        return RELATION_MAP[c]
    for k, r in RELATION_MAP.items():
        if c.startswith(k):
            return r
    return "OTHER"


def parse_header(page_text: str) -> RollHeader:
    t = clean(page_text)
    h = RollHeader()

    m = re.search(r"निर्वाचक\s+नामावली-(\d{4})", t)
    if m:
        h.roll_year = int(m.group(1))
    m = re.search(r"([A-Z]\d{2})\s", t)
    if m:
        h.state_code = m.group(1)
    m = re.search(r"अर्हता\s*तिथि\s*:?-?\s*([\d.\-/]+)", t)
    if m:
        h.qualifying_date = m.group(1)
    m = re.search(r"भाग\s*संख्या\s*:?-?\s*(\d+)", t)
    if m:
        h.part_number = int(m.group(1))
    m = re.search(r"क्षेत्र\s*की\s*संख्या\s*व\s*नाम\s*:?-?\s*(\d+)\s*-\s*([^\n]+?)\s*(?:पुनरीक्षण|मतदेय|क्रम|$)", t)
    if m:
        h.ac_number = int(m.group(1))
        h.ac_name = m.group(2).strip()
    m = re.search(r"पुनरीक्षण\s*का\s*स्वरूप\s*:?-?\s*([^\n]+?)\s*(?:मतदेय|क्रम|$)", t)
    if m:
        h.revision_type = m.group(1).strip()
    m = re.search(r"मतदेय\s*स्थल\s*की\s*संख्या\s*तथा\s*नाम[:ः\s-]*(\d+)\s*(.+?)\s*(?:मतदेय|क्रम|$)", t)
    if m:
        h.polling_station_number = int(m.group(1))
        h.polling_station_name = m.group(2).strip()
    m = re.search(r"आदि\s*का\s*नाम\s*:?-?\s*(.+?)\s*(?:नि\.क्र|क्रम\s*संख्या|मकान\s*संख्या|$)", t)
    if m:
        h.areas = m.group(1).strip()
    return h


# Serials sometimes carry a stray combining mark from the embedded font
# ("62̻"), so match a leading run of digits instead of testing isdigit().
_SERIAL_RE = re.compile(r"^\d+")


def _column_of(x: float) -> str | None:
    for name, lo, hi in COLUMNS:
        if lo <= x < hi:
            return name
    return None


def _page_bounds(words: list) -> tuple[float, float]:
    """Find where this page's table body starts and stops.

    Top: just below the "(1) (2) ... (8)" column-number row.
    Bottom: just above the footnote, which reuses the serial column's x range.
    """
    # Continuation pages carry a shorter header, so their column-number row sits
    # ~74pt higher than page 1's. Use whatever the page actually shows; the
    # constant is only a fallback for a page where the marker is missing.
    marker_ys = [w[1] for w in words if w[4].strip() in COLUMN_NUMBER_ROW]
    top = (max(marker_ys) + 12.0) if marker_ys else BODY_TOP_FALLBACK

    bottom = BODY_BOTTOM_FALLBACK
    for x0, y0, x1, y1, text, *_ in words:
        if y0 > top and x0 < FOOTNOTE_X_MAX and text.startswith(FOOTNOTE_MARKER):
            bottom = min(bottom, y0 - 4.0)
    return top, bottom


def _page_rows(page: pymupdf.Page) -> list[dict[str, str]]:
    """Bucket a page's words into table rows, anchored on the serial column."""
    all_words = page.get_text("words")
    top, bottom = _page_bounds(all_words)
    words = [w for w in all_words if top <= w[1] <= bottom]
    if not words:
        return []

    # Anchors: numeric tokens in the serial column define one row each.
    anchors = sorted(
        (w[1], w[4]) for w in words
        if _column_of(w[0]) == "serial" and _SERIAL_RE.match(w[4].strip())
    )
    if not anchors:
        return []

    buckets: list[dict[str, list[tuple[float, str]]]] = [{} for _ in anchors]
    anchor_ys = [a[0] for a in anchors]

    for x0, y0, x1, y1, text, *_ in words:
        col = _column_of(x0)
        if col is None or not text.strip():
            continue
        # Assign to the nearest anchor at or above this word, within tolerance of
        # the next anchor — this keeps wrapped second lines with their own row.
        idx = None
        for i, ay in enumerate(anchor_ys):
            if y0 >= ay - ROW_TOLERANCE:
                idx = i
            else:
                break
        if idx is None:
            continue
        buckets[idx].setdefault(col, []).append((x0, text))

    rows = []
    for b in buckets:
        row = {}
        for col, items in b.items():
            row[col] = " ".join(t for _, t in sorted(items))
        rows.append(row)
    return rows


def parse_roll_pdf(path: Path) -> RollParseResult:
    """Parse a 2003 roll PDF into header + elector rows."""
    result = RollParseResult(header=RollHeader())
    with pymupdf.open(path) as doc:
        result.page_count = doc.page_count
        if doc.page_count:
            result.header = parse_header(doc[0].get_text())

        seen_serials: set[int] = set()
        for pno in range(doc.page_count):
            page = doc[pno]
            rows = _page_rows(page)
            if rows:
                result.pages_with_rows += 1
            for row in rows:
                serial = _to_int(row.get("serial"))
                if serial is None:
                    continue
                seen_serials.add(serial)

                raw_name = row.get("name")
                raw_rel = row.get("relative")
                name, name_conf = clean_with_confidence(raw_name or "")
                rel, rel_conf = clean_with_confidence(raw_rel or "")
                raw_text = " | ".join(
                    f"{c}={row.get(c, '')}" for c, _, _ in COLUMNS if row.get(c)
                )
                confs = [c for c in (name_conf, rel_conf) if c > 0]
                result.electors.append(ElectorRow(
                    serial_number=serial,
                    house_number=_norm(row.get("house")),
                    elector_name=name or None,
                    elector_name_raw=(raw_name or None),
                    relative_name=rel or None,
                    relative_name_raw=(raw_rel or None),
                    relative_type=_map_relation(row.get("relation")),
                    gender=_map_gender(row.get("gender")),
                    age=_to_int(row.get("age")),
                    epic_number=_norm(row.get("epic")),
                    source_page=pno + 1,
                    raw_text=raw_text,
                    confidence=round(sum(confs) / len(confs), 4) if confs else 0.0,
                ))

    if not result.electors:
        result.warnings.append("no elector rows were extracted")
    if result.header.part_number is None:
        result.warnings.append("part number could not be read from the header")
    return result
