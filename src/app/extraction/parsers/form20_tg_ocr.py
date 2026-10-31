"""CEO Telangana Form 20 (2018): scanned result sheets, read by OCR one cell at a time.

Nothing is stored on the OCR alone. A booth row is kept only when exactly one
combination of the digit readings satisfies both of the sheet's own printed checks:
the candidate columns add up to "Total of valid votes", and valid + rejected + NOTA =
"Total". Rows that do not reconcile are counted and dropped.

Grid: under the header, the row of printed column numbers (1, 2, 3 ...) is a thin band
between two long horizontal rules. Its vertical rules give every column's edges, and the
long horizontal rules below it give the rows. Each cell is read on its own: Tesseract
(digits only, one line) first; for a row that does not reconcile, second opinions from
Tesseract's single-character and single-word modes and from RapidOCR.

Column layout (2018): Sl. No., PS No., one column per candidate, Total of valid votes,
No. of rejected votes, NOTA, Total, No. of tendered votes. The last pages add POSTAL,
ETPBS and Total rows; the per-candidate "Total" rows are what the service matches against
the ECI statistical report to name each column.
"""
from __future__ import annotations

import itertools
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

PARSER_NAME = "form20_tg_ocr"
PARSER_VERSION = "1.0.0"

DPI = 220
TRAILING = 5                 # valid, rejected, NOTA, total, tendered
LEADING = 2                  # Sl. No., PS No.
MAX_COMBINATIONS = 20000
_ZERO = set("oO。○°DQ〇")
_PS = re.compile(r"^(\d{1,4})-?([A-E])?$")


@dataclass
class TgRow:
    page: int                            # 1-based page of the PDF
    kind: str                            # booth | total | postal | etpbs
    serial: int | None = None
    ps_label: str | None = None          # "12" or "12A" as printed
    votes: list[int] | None = None       # candidate columns; set only when the row reconciles
    total_valid: int | None = None
    rejected: int | None = None
    nota: int | None = None
    total: int | None = None
    tendered: int | None = None
    options: list[list[str]] | None = None   # Total/POSTAL rows: every reading of each column

    @property
    def reconciled(self) -> bool:
        return self.votes is not None


@dataclass
class PageRead:
    page: int
    columns: int | None                  # candidate columns on this page (None: no grid found)
    rows: list[TgRow] = field(default_factory=list)


@dataclass
class TgSheet:
    candidate_columns: int | None
    rows: list[TgRow]
    pages: int
    warnings: list[str] = field(default_factory=list)

    @property
    def booth_rows(self) -> list[TgRow]:
        return [r for r in self.rows if r.kind == "booth"]

    @property
    def booths_read(self) -> list[TgRow]:
        """Booths that can be stored: placed (PS number confirmed) and reconciled."""
        return [r for r in self.booth_rows if r.reconciled and r.ps_label is not None]

    @property
    def booths_expected(self) -> int:
        """Booths the sheet covers: the highest confirmed PS number, plus the auxiliary
        stations (12A ...) confirmed. An unread auxiliary station is not counted."""
        labels = {r.ps_label for r in self.booth_rows if r.ps_label}
        top = max((ps_number(x)[0] for x in labels), default=0)
        return top + sum(1 for x in labels if ps_number(x)[1])

    def label_row(self, which: str) -> TgRow | None:
        """'evm': the Total row after the booths; 'grand': the Total after POSTAL/ETPBS."""
        seen_postal = False
        for r in self.rows:
            if r.kind in ("postal", "etpbs"):
                seen_postal = True
            elif r.kind == "total" and seen_postal == (which == "grand"):
                return r
        return None

    def total_row(self, which: str) -> TgRow | None:
        """The Total row, only when its readings reconcile."""
        r = self.label_row(which)
        return r if r is not None and r.reconciled else None


# ----------------------------------------------------------------------------- engines
_RAPID = None


def _engines():
    global _RAPID
    import pytesseract

    from ..ocr.engine import _tesseract_cmd
    cmd = _tesseract_cmd()
    if not cmd:
        raise RuntimeError("tesseract is not installed")
    pytesseract.pytesseract.tesseract_cmd = cmd
    if _RAPID is None:
        import rapidocr_onnxruntime.utils as rapid_utils
        from rapidocr_onnxruntime import RapidOCR
        make_options = rapid_utils.SessionOptions

        def one_thread():               # many worker processes: one inference thread each
            opt = make_options()
            opt.intra_op_num_threads = 1
            opt.inter_op_num_threads = 1
            return opt

        rapid_utils.SessionOptions = one_thread
        _RAPID = RapidOCR()
    return pytesseract, _RAPID


def _digits(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return "".join("0" if c in _ZERO else c for c in s if c.isdigit() or c in _ZERO)


def _tess(c, psm: int, whitelist: str = "0123456789") -> str:
    from PIL import Image
    pt, _ = _engines()
    big = Image.fromarray(c).resize((c.shape[1] * 2, c.shape[0] * 2), Image.LANCZOS)
    return pt.image_to_string(big, config=f"--psm {psm} -c tessedit_char_whitelist={whitelist}").strip()


MAX_STACK_HEIGHT = 16000


def tess_batch(crops: list, whitelist: str = "0123456789", scale: float = 2.0) -> list[str]:
    """Read many cells with one Tesseract run: the cells are stacked one per line on a white
    canvas and each recognised word is given back to the cell whose band it falls in.
    (Starting Tesseract once per cell costs more than the recognition itself.)"""
    from PIL import Image
    pt, _ = _engines()
    out = [""] * len(crops)
    items = [(i, c) for i, c in enumerate(crops) if c is not None]
    gap = 24
    while items:
        chunk, height = [], gap
        while items and (not chunk or height + items[0][1].shape[0] * scale + gap < MAX_STACK_HEIGHT):
            i, c = items.pop(0)
            im = Image.fromarray(c).resize((int(c.shape[1] * scale), int(c.shape[0] * scale)), Image.LANCZOS)
            chunk.append((i, im, height))
            height += im.height + gap
        canvas = Image.new("L", (max(im.width for _, im, _ in chunk) + 40, int(height)), 255)
        for _, im, y in chunk:
            canvas.paste(im, (20, int(y)))
        d = pt.image_to_data(canvas, config=f"--psm 6 -c tessedit_char_whitelist={whitelist}",
                             output_type=pt.Output.DICT)
        words: dict[int, list] = {}
        for text, top, h, left in zip(d["text"], d["top"], d["height"], d["left"]):
            text = (text or "").strip()
            if not text:
                continue
            mid = top + h / 2
            for k, (_, im, y) in enumerate(chunk):
                if y <= mid <= y + im.height:
                    words.setdefault(k, []).append((left, text))
                    break
        for k, (i, _, _) in enumerate(chunk):
            out[i] = "".join(t for _, t in sorted(words.get(k, [])))
    return out


def rapid_batch(crops: list) -> list[str]:
    import numpy as np
    _, rapid = _engines()
    out = [""] * len(crops)
    items = [(i, np.stack([c] * 3, -1)) for i, c in enumerate(crops) if c is not None]
    if items:
        res, _ = rapid.text_recognizer([im for _, im in items])
        for (i, _), r in zip(items, res):
            out[i] = r[0] if r else ""
    return out


def _nums(texts: list[str]) -> list[str]:
    return [t if t.isdigit() else "" for t in texts]


def reconcile(options: list[list[str]]) -> tuple[int, ...] | None:
    """The one combination of readings (candidates.., valid, rejected, NOTA, total) that
    satisfies both printed checks; None when there is none or more than one.

    Counted exactly, without trying every combination: ways[s] is how many choices of the
    candidate readings add up to s (capped at 2 — only "one" or "more than one" matters)."""
    if not options or len(options) < 5 or any(not o for o in options):
        return None
    cands, (valid, rejected, nota, total) = options[:-4], options[-4:]
    ways: dict[int, tuple[int, tuple[int, ...]]] = {0: (1, ())}
    for opts in cands:
        nxt: dict[int, tuple[int, tuple[int, ...]]] = {}
        for s0, (count, path) in ways.items():
            for v in {int(o) for o in opts}:
                c, p = nxt.get(s0 + v, (0, path + (v,)))
                nxt[s0 + v] = (min(c + count, 2), p)
        ways = nxt
    found: list[tuple[int, ...]] = []
    n = 0
    for v in {int(o) for o in valid}:
        count, path = ways.get(v, (0, ()))
        if not count:
            continue
        for r in {int(o) for o in rejected}:
            for x in {int(o) for o in nota}:
                if v + r + x in {int(o) for o in total}:
                    n += count
                    found.append(path + (v, r, x, v + r + x))
    return found[0] if n == 1 else None


def trimmed(readings: list[str]) -> list[str]:
    """Readings plus each with a stray first or last digit dropped (a pen tick read as '1'
    or '4'). Used only for the printed Total rows, which name columns by exact match."""
    out = list(readings)
    for t in readings:
        for v in (t[1:], t[:-1]):
            if len(t) >= 3 and v and v not in out:
                out.append(v)
    return out


# ----------------------------------------------------------------------------- grid
def _longest(m):
    import numpy as np
    best = np.zeros(m.shape[0], int)
    cur = np.zeros(m.shape[0], int)
    for j in range(m.shape[1]):
        cur = np.where(m[:, j], cur + 1, 0)
        best = np.maximum(best, cur)
    return best


def _bands(flags, gap: int = 8) -> list[list[int]]:
    import numpy as np
    out: list[list[int]] = []
    for i in np.where(flags)[0]:
        if out and i - out[-1][1] <= gap:
            out[-1][1] = int(i)
        else:
            out.append([int(i), int(i)])
    return out


def _rules(a, width_share: float = 0.45):
    """Horizontal rules, tolerant of a slightly tilted scan: a line that drifts across a
    few pixel rows still counts as one long run once rows are OR-ed with their neighbours."""
    import numpy as np
    d = a.copy()
    for k in (1, 2, 3):
        d[k:] |= a[:-k]
        d[:-k] |= a[k:]
    return _bands(_longest(d) > a.shape[1] * width_share)


def _row_spans(hb, hdr: int, height: int) -> list[tuple[int, int]]:
    """Row spans below the column-number band. A band holding two or three rows (a faint
    rule between them) is split at the usual row height; rows after the last detected rule
    are added at the same height while they stay on the page."""
    import statistics
    spans = [(hb[i][1] + 1, hb[i + 1][0]) for i in range(hdr + 1, len(hb) - 1)]
    tall = [b - a for a, b in spans if b - a > 60]
    if not tall:
        return spans
    pitch = statistics.median(tall)
    out: list[tuple[int, int]] = []
    for a, b in spans:
        k = max(1, round((b - a) / pitch))
        step = (b - a) / k
        out.extend((int(a + i * step), int(a + (i + 1) * step)) for i in range(k))
    y = hb[-1][1] + 1
    while y + pitch < height - 40:
        out.append((int(y), int(y + pitch)))
        y += pitch
    return out


def _deskew(g):
    """Straighten a tilted scan: the angle (within +-2 degrees) at which the dark pixels
    line up into the sharpest rows, i.e. the table rules become horizontal."""
    import numpy as np
    from PIL import Image
    small = Image.fromarray(((g[::4, ::4] < 150) * 255).astype(np.uint8))
    best, best_score = 0.0, -1.0
    for angle in np.arange(-2.0, 2.01, 0.1):
        rows = np.asarray(small.rotate(float(angle), resample=Image.NEAREST, fillcolor=0)).sum(axis=1, dtype=np.int64)
        score = float((rows.astype(np.float64) ** 2).sum())
        if score > best_score:
            best, best_score = float(angle), score
    if abs(best) < 0.05:
        return g
    return np.asarray(Image.fromarray(g).rotate(best, resample=Image.BICUBIC, fillcolor=255))


def _crop(c):
    import numpy as np
    c = c.copy()
    dark = c < 140
    # strip ruling lines caught at a cell's edge: they would swamp the digits
    c[:, dark.mean(axis=0) > 0.85] = 255
    c[dark.mean(axis=1) > 0.85, :] = 255
    ys, xs = np.where(c < 140)
    if len(ys) < 12:
        return None
    c = c[max(0, ys.min() - 4):ys.max() + 5, max(0, xs.min() - 4):xs.max() + 5]
    return np.pad(c, 25, constant_values=255)


def _label(c) -> str:
    t = re.sub(r"[^A-Z]", "", _tess(c, 7, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz").upper())
    if "POST" in t:
        return "postal"
    if "ETP" in t or "TPB" in t:
        return "etpbs"
    if "TOT" in t or "OTAL" in t:
        return "total"
    return ""


def _anchor(a, hb):
    """The column-number band: the topmost thin band between two rules that is crossed by
    a full set of column rules (in typed sheets the data rows below are thin too, so the
    first such band is taken, not the one with the most rules). Returns its vertical
    rules and its index among the rules."""
    found = []
    for bi in range(min(len(hb) - 1, 8)):
        y0, y1 = hb[bi][1] + 1, hb[bi + 1][0]
        if 12 <= y1 - y0 <= 60:
            v = _vrules(a, hb[bi][0], hb[bi + 1][1])
            if len(v) >= LEADING + TRAILING + 2:
                found.append((bi, v))
    if not found:
        return None
    most = max(len(v) for _, v in found)
    bi, v = next((bi, v) for bi, v in found if len(v) >= most - 1)
    # a blank cell at the right edge is margin or the scanner's edge, not a column:
    # every real column has its number printed in this band
    y0, y1 = hb[bi][1] + 4, hb[bi + 1][0] - 3
    while len(v) > LEADING + TRAILING + 2 and a[y0:y1, v[-2][1] + 3:v[-1][0] - 3].mean() < 0.01:
        v = v[:-1]
    return v, bi


def _vrules(a, top: int, bottom: int):
    """Vertical rules running the full height between two horizontal rules (both rules
    included): a digit never touches both, so short rows cannot fake a rule."""
    return _bands(_longest(a[top:bottom + 1].T) >= (bottom + 1 - top) * 0.9, gap=6)


def _split_first(a, spans, vb) -> int | None:
    """Typed sheets number "No. & Name of the Polling Station" as one column (1) but draw a
    rule between the number and the name in every data row. Returns that rule's offset from
    the band's first rule when most rows have it, else None."""
    offsets = []
    for top, bottom in spans:
        y0, y1 = top + 5, bottom - 5
        if y1 - y0 < 15:
            continue
        rv = _vrules(a, top - 4, bottom + 4)
        # only a rule inside the band's first cell counts
        inside = [r for r in rv if vb[0][1] + 10 < r[0] < vb[1][0] - 10]
        if len(inside) == 1:
            offsets.append(inside[0][0] - vb[0][0])
    if len(offsets) * 2 < max(1, len(spans)):
        return None
    return sorted(offsets)[len(offsets) // 2]


def read_page(path: str | Path, page_index: int) -> PageRead:
    """Read one page. Runs in a worker process, so it opens the PDF itself."""
    import numpy as np
    import pymupdf
    from PIL import Image

    with pymupdf.open(str(path)) as doc:
        pix = doc[page_index].get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
    raw = np.array(Image.frombytes("L", (pix.width, pix.height), pix.samples))
    page = PageRead(page=page_index + 1, columns=None)
    # rotating blurs digits a little, so a page is straightened only when its grid cannot
    # be found as scanned
    for straighten in (False, True):
        g = _deskew(raw) if straighten else raw
        a = g < 150
        hb = _rules(a)
        anchor = _anchor(a, hb)
        if anchor is not None:
            break
    if anchor is None:
        return page

    def cut(y0, y1, x0, x1):
        return _crop(g[y0:y1, x0:x1])
    scale = 2.0          # reading cells from a 300 dpi render instead did no better (tested)
    vb, hdr = anchor
    spans = _row_spans(hb, hdr, a.shape[0])
    split = _split_first(a, spans, vb)
    if split is not None:
        # typed layout: [No. | Name] share band column 1; there is no separate serial column
        vb = [vb[0], (vb[0][0] + split, vb[0][1] + split)] + vb[1:]
    ncol = len(vb) - 1
    page.columns = ncol - LEADING - TRAILING
    lines = []                                   # (crops, merged first-two-columns crop)
    for top, bottom in spans:
        # a rule's band is already 3 rows wider than the rule (tilt tolerance); typed sheets
        # have short rows with digits close to the rules, so they get no further inset
        inset = 5 if bottom - top > 60 else 0
        y0, y1 = top + inset, bottom - inset
        if y1 - y0 < 15:
            continue
        # the column rules of this row itself where they are all visible (a scan can bow)
        rv = _vrules(a, top - 4, bottom + 4)
        cols = rv if len(rv) == len(vb) and all(abs(p[0] - q[0]) < 25 for p, q in zip(rv, vb)) else vb
        crops = [cut(y0, y1, cols[ci][1] + 6, cols[ci + 1][0] - 5) for ci in range(ncol)]
        if split is not None:
            crops[0], crops[1] = None, crops[0]  # PS number from the number cell; the name is not read
        lines.append((crops, cut(y0, y1, cols[0][1] + 6, cols[2][0] - 5)))
    if not lines:
        return page

    # 1. PS numbers (with auxiliary letters) and serials
    ps_read = tess_batch([c[1] for c, _ in lines], "0123456789ABCDE-", scale)
    ps_rapid = None
    serials = _nums(tess_batch([c[0] for c, _ in lines], scale=scale))
    rows: list[tuple[TgRow, list]] = []
    for k, ((crops, merged), ps) in enumerate(zip(lines, ps_read)):
        row = TgRow(page=page.page, kind="booth")
        m = _PS.match(ps.replace(" ", ""))
        if not m and crops[1] is not None:
            ps_rapid = ps_rapid or rapid_batch([c[1] for c, _ in lines])
            m = _PS.match(unicodedata.normalize("NFKC", ps_rapid[k]).upper().replace(" ", ""))
        if m:
            row.ps_label = m.group(1) + (m.group(2) or "")
            row.serial = int(serials[k]) if serials[k] else None
        else:
            row.kind = _label(merged) if merged is not None else ""
            if not row.kind:
                continue                       # blank or unreadable row: not a booth we can place
        rows.append((row, crops))

    # 2. vote cells: candidates .. Total (tendered is not in the checks)
    cells = [cr for _, crops in rows for cr in crops[LEADING:-1]]
    width = ncol - LEADING - 1
    first = _nums(tess_batch(cells, scale=scale))
    options = [[[f] if f else [] for f in first[i * width:(i + 1) * width]] for i in range(len(rows))]
    hits = [reconcile(o) for o in options]
    # 3. second opinions for rows that do not reconcile, and always for Total/POSTAL rows
    todo = [i for i, h in enumerate(hits) if h is None or rows[i][0].kind != "booth"]
    if todo:
        sub = [cr for i in todo for cr in rows[i][1][LEADING:-1]]
        alternatives = [_nums(tess_batch(sub, scale=scale * 1.5)), _nums(tess_batch(sub, scale=scale * 0.75)),
                        [_digits(t) for t in rapid_batch(sub)]]
        for n, i in enumerate(todo):
            opts = options[i]
            for alt in alternatives:
                for j, v in enumerate(alt[n * width:(n + 1) * width]):
                    if v and v not in opts[j]:
                        opts[j].append(v)
            if rows[i][0].kind != "booth":
                opts = [trimmed(o) for o in opts]
                rows[i][0].options = opts
            hits[i] = reconcile(opts)
    tendered = _nums(tess_batch([crops[-1] for _, crops in rows], scale=scale))
    for (row, _), hit, t in zip(rows, hits, tendered):
        if hit is not None:
            row.votes = list(hit[:-4])
            row.total_valid, row.rejected, row.nota, row.total = hit[-4:]
            row.tendered = int(t) if t else None
        page.rows.append(row)
    return page


# ----------------------------------------------------------------------------- sheet
def assemble(pages: list[PageRead]) -> TgSheet:
    """Combine page reads: one candidate-column count for the sheet, serial numbers checked
    against their neighbours, duplicate booth labels dropped."""
    counts = [p.columns for p in pages if p.columns]
    warnings: list[str] = []
    if not counts:
        return TgSheet(None, [], len(pages), ["no result grid found on any page"])
    ncand = max(set(counts), key=counts.count)
    rows: list[TgRow] = []
    for p in sorted(pages, key=lambda p: p.page):
        if p.columns is None:
            continue
        if p.columns != ncand:
            warnings.append(f"page {p.page}: {p.columns} candidate columns, the sheet has {ncand}; page skipped")
            continue
        rows.extend(p.rows)

    # A booth's PS number is accepted only when a neighbouring row agrees with it (the next
    # number, or the same number for an auxiliary station), or its serial reads the same.
    booths = [r for r in rows if r.kind == "booth"]
    nums = [ps_number(r.ps_label)[0] if r.ps_label else None for r in booths]

    def fits(i: int, j: int) -> bool:
        if not (0 <= j < len(booths)) or nums[i] is None or nums[j] is None:
            return False
        lo, hi = (i, j) if i < j else (j, i)
        return nums[hi] - nums[lo] in (0, 1) and (nums[hi] != nums[lo] or bool(ps_number(booths[hi].ps_label)[1]))

    for i, r in enumerate(booths):
        ok = nums[i] is not None and nums[i] > 0 and (
            fits(i, i - 1) or fits(i, i + 1) or (r.serial is not None and r.serial == nums[i]))
        if not ok:
            r.ps_label = None
        r.serial = None                 # the Sl. No. column reads unreliably; counted below instead
    counts: dict[str, int] = {}
    for r in booths:
        if r.ps_label:
            counts[r.ps_label] = counts.get(r.ps_label, 0) + 1
    for r in booths:
        if r.ps_label and counts[r.ps_label] > 1:
            r.ps_label = None           # one label is one booth: duplicates are not placed
    return TgSheet(ncand, rows, len(pages), warnings)


def column_overreads(sheet: TgSheet) -> list[int]:
    """Candidate columns whose read booths already add up to more than the printed Total
    row for that column: some reading in that column must be wrong."""
    evm = sheet.total_row("evm")
    if evm is None:
        return []
    sums = [0] * len(evm.votes)
    for r in sheet.booths_read:
        for j, v in enumerate(r.votes):
            sums[j] += v
    return [j for j, (s, t) in enumerate(zip(sums, evm.votes)) if s > t]


def ps_number(label: str) -> tuple[int, str]:
    m = _PS.match(label)
    return int(m.group(1)), m.group(2) or ""
