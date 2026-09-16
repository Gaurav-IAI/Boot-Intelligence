"""Read the scanned Polling Station List 2024 (CEO Uttarakhand, Lok Sabha 2024) by OCR.

The 2024 list is published as page scans only. Each page is a ruled table:

    क्रम सं. | मतदान केन्द्र का परिक्षेत्र | भवन, जिसमें यह स्थित होगा | मतदान क्षेत्र | सभी के लिये

Reading the whole page with Tesseract mixes neighbouring rows and columns, so the
table is read geometrically instead:

  * column rules are found from the vertical projection and fitted to the known
    table template (the scans differ by a shift, and page 1 is slightly skewed, so
    the fit is redone inside every row band);
  * row boundaries are the horizontal rules crossing the narrow serial column
    (the areas column has its own inner rules, which must not split a station);
  * every cell is OCR'd on its own — the serial with a digits-only whitelist.

OCR is imperfect: the output is evidence, not a record. Serials that do not fit the
running sequence are dropped (serial=None), and consumers must cross-check every
row against an independent official source before using it.
"""
from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pymupdf

from .engine import _tesseract_cmd, ocr_available, tesseract_config

PARSER_NAME = "ps_list_2024_scan_ocr"
PARSER_VERSION = "1.0.0"
DPI = 300
# x of the six vertical table rules at 300 dpi (measured on pages 3, 7 and 8 of AC 19)
TEMPLATE = (380, 460, 722, 1257, 1960, 2283)
_DEV = str.maketrans("०१२३४५६७८९", "0123456789")


@dataclass
class ScanRow:
    page: int
    serial: int | None          # None when the OCR'd number does not fit the sequence
    serial_text: str
    locality: str
    building: str
    areas: str
    box: tuple[int, int, int, int]


def _dark(img) -> np.ndarray:
    return np.asarray(img.convert("L")) < 170


def _groups(idx: np.ndarray, gap: int) -> list[int]:
    out: list[list[int]] = []
    for x in idx:
        if out and x - out[-1][-1] <= gap:
            out[-1].append(int(x))
        else:
            out.append([int(x)])
    return [int(np.mean(g)) for g in out]


def vertical_rules(dark: np.ndarray, y0: int, y1: int, threshold: float) -> list[int]:
    share = dark[y0:y1, :].mean(axis=0)
    share = np.convolve(share, np.ones(5) / 5, mode="same")
    return _groups(np.where(share > threshold)[0], 12)


def fit_template(found: list[int], tol: int = 15) -> tuple[list[int] | None, int]:
    """Shift the template onto the detected rules; None unless 3+ rules agree."""
    best_score, best_shift = 0, 0
    for f in found:
        for t in TEMPLATE:
            shift = f - t
            score = sum(1 for tt in TEMPLATE if any(abs(tt + shift - x) <= tol for x in found))
            if score > best_score:
                best_score, best_shift = score, shift
    if best_score < 3:
        return None, best_score
    # snap each template rule onto its detected rule where there is one
    snapped = []
    for t in TEMPLATE:
        near = [x for x in found if abs(t + best_shift - x) <= tol]
        snapped.append(near[0] if near else t + best_shift)
    return snapped, best_score


def row_bands(dark: np.ndarray, x0: int, x1: int, min_height: int = 45) -> list[tuple[int, int]]:
    """Row bands between horizontal rules that cross the serial column."""
    strip = dark[:, x0 + 8:x1 - 8]
    share = strip.mean(axis=1)
    lines = _groups(np.where(share > 0.6)[0], 6)
    return [(a, b) for a, b in zip(lines, lines[1:]) if b - a >= min_height]


def _clean_lines(cell: np.ndarray) -> np.ndarray:
    """Whiten ruled lines inside a cell so they are not read as text."""
    cell = cell.copy()
    dark = cell < 170
    cell[dark.mean(axis=1) > 0.5, :] = 255
    cell[:, dark.mean(axis=0) > 0.5] = 255
    return cell


def _ocr(gray: np.ndarray, lang: str, config: str) -> str:
    import pytesseract
    from PIL import Image
    if gray.size == 0 or (gray < 170).mean() < 0.003:
        return ""
    img = Image.fromarray(np.pad(_clean_lines(gray), 12, constant_values=255))
    text = pytesseract.image_to_string(img, lang=lang, config=(tesseract_config() + " " + config).strip())
    return " ".join(text.split())


_HEADER = re.compile(r"यह|हागा|होगा|क्ष[े]?त्र|परिक्")


def is_header_row(locality: str, building: str, areas: str) -> bool:
    """The column captions repeated on every page ("भवन, जिसमें यह स्थित होगा" ...)."""
    hits = sum(1 for t in (locality, building, areas) if _HEADER.search(t or "") and len(t or "") < 30)
    return hits >= 2


def clean_rows(rows: list[dict]) -> list[dict]:
    """Drop caption rows, then keep only serials a neighbouring row agrees with."""
    kept = [r for r in rows if not is_header_row(r["locality"], r["building"], r["areas"])]
    raw = [read_serial(r["serial_text"]) for r in kept]
    for r, s in zip(kept, assign_serials(raw)):
        r["serial"] = s
    return kept


def read_serial(text: str) -> int | None:
    digits = re.sub(r"\D", "", (text or "").translate(_DEV))
    return int(digits) if digits and len(digits) <= 3 else None


def assign_serials(raw: list[int | None]) -> list[int | None]:
    """Keep a serial only if a neighbouring read number agrees with it.

    Serials rise by 1 per station (a little more where a row band was lost), so a
    value is kept when the previous or next read serial sits 1..3 away in the right
    direction. An isolated misread ("18" read as "48") has no such neighbour and
    becomes None.
    """
    known = [(i, s) for i, s in enumerate(raw) if s is not None]
    out: list[int | None] = [None] * len(raw)
    for k, (i, s) in enumerate(known):
        before = known[k - 1][1] if k > 0 else None
        after = known[k + 1][1] if k + 1 < len(known) else None
        if (before is not None and 1 <= s - before <= 3) or (after is not None and 1 <= after - s <= 3):
            out[i] = s
    return out


def read_scanned_ps_list(path: Path, max_pages: int | None = None) -> dict:
    """OCR a scanned 2024 polling-station list into rows with provenance."""
    ok, reason = ocr_available()
    if not ok:
        raise RuntimeError(reason)
    import pytesseract
    from PIL import Image

    pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd()
    rows: list[ScanRow] = []
    pages_read, pages_skipped = 0, []
    with pymupdf.open(path) as doc:
        n = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
        for pno in range(n):
            img = Image.open(io.BytesIO(doc[pno].get_pixmap(dpi=DPI).tobytes("png")))
            gray = np.asarray(img.convert("L"))
            dark = gray < 170
            h = dark.shape[0]
            cols, _ = fit_template(vertical_rules(dark, int(h * 0.12), int(h * 0.93), 0.18))
            if cols is None:
                pages_skipped.append(pno + 1)
                continue
            pages_read += 1
            for y0, y1 in row_bands(dark, cols[0], cols[1]):
                local, _ = fit_template(vertical_rules(dark, y0 + 4, y1 - 4, 0.6))
                c = local or cols
                cell = lambda k: gray[y0 + 5:y1 - 4, c[k] + 6:c[k + 1] - 5]
                serial_text = _ocr(cell(0), "eng", "--psm 7 -c tessedit_char_whitelist=0123456789")
                if not serial_text:
                    serial_text = _ocr(cell(0), "eng", "--psm 6 -c tessedit_char_whitelist=0123456789")
                building = _ocr(cell(2), "hin+eng", "--psm 6")
                if not building and not serial_text:
                    continue                      # header, tehsil caption or empty band
                locality = _ocr(cell(1), "hin+eng", "--psm 6")
                if re.fullmatch(r"[\d\s]*", locality) and re.fullmatch(r"[\d\s]*", building):
                    continue                      # the "1 2 3 4 5" column-number row
                rows.append(ScanRow(pno + 1, read_serial(serial_text), serial_text, locality, building,
                                    _ocr(cell(3), "hin+eng", "--psm 6"), (c[0], y0, c[4], y1)))
    return {
        "parser": PARSER_NAME, "parser_version": PARSER_VERSION, "dpi": DPI,
        "pages_read": pages_read, "pages_skipped": pages_skipped,
        "rows": clean_rows([asdict(r) for r in rows]),
    }
