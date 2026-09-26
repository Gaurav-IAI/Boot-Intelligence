"""District "List of Polling Stations" PDFs published for Uttar Pradesh (SIR 2026 draft).

Each district election office publishes one PDF per assembly constituency
(Annexure-1): a ten-column table whose columns are numbered 1..10 under the
header —

    1 station no. | 2 locality | 3 building | 4 room area | 5 entry/exit doors |
    6 polling area | 7 for all / men / women | 8 electors | 9 max distance | 10 remarks

The text layer mixes two broken Hindi encodings within one cell:

* Kruti Dev (ASCII letters: ``uxj fuxe xkft;kckn`` = नगर निगम गाजियाबाद), handled by
  `krutidev`, and
* a Unicode font whose ToUnicode map is wrong for its conjunct glyphs and stores the
  short-i sign in visual order (``कàपोिजटͪ वघालय`` = कम्पोजिट विद्यालय), repaired
  here with a table read off real pages. Unmapped glyphs lower the confidence.

Some pages carry edit patches pasted over the original rows, so a station number
can occur twice; the copies are merged (the one with an elector count wins) and a
disagreement is reported, never silently resolved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import krutidev

PARSER_NAME = "ps_list_up_columns"
PARSER_VERSION = "1.5.0"

# glyph -> Devanagari, for the mis-mapped Unicode font (read off Ghaziabad 2025 pages)
GLYPHS: dict[str, str] = {
    "à": "म्", "¢": "क्ष", "Û": "न्", "ȣ": "ी", "Þ": "ब्", "è": "स्", "Ö": "ण्",
    "È": "क्", "Ē": "ग्र", "Ħ": "ब्र", "Ù": "त्त",
}
SHORT_I = "ͪͧǒǑǓ"                      # variants of the short-i sign (visual order)
_REPH = "["                             # reph printed after its consonant: पूव[ = पूर्व
_CONS = "कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसहक़ख़ग़ज़ड़ढ़फ़"
_CLUSTER = re.compile(rf"(?:[{_CONS}]्)*[{_CONS}]़?")
_BROKEN = re.compile(rf"[{''.join(GLYPHS)}{SHORT_I}\[]")
# "ernkrkvksa dh dqy la[;k%&459698" (Kruti Dev: मतदाताओं की कुल संख्या) under the last table
_TOTAL = re.compile(r"ernkrkvksa\s+dh\s+dqy\s+la\[;k\s*%?\s*&?\s*(\d{4,7})")


def fix_visual_unicode(s: str) -> str:
    """Repair the mis-mapped Unicode font: conjunct glyphs, reph, visual-order short-i."""
    for k, v in GLYPHS.items():
        s = s.replace(k, v)
    s = s.replace("घालय", "द्यालय")      # this font's ligature for द्य in विद्यालय
    for k in SHORT_I:
        s = s.replace(k, "ि")
    # short-i precedes its consonant cluster (possibly across an inserted space)
    s = re.sub(rf"ि\s*({_CLUSTER.pattern})", lambda m: m.group(1) + "ि", s)
    # reph: "C[" -> "र्C"
    s = re.sub(rf"({_CLUSTER.pattern})\[", lambda m: "र्" + m.group(1), s)
    return s


# Font B: the font the Uttarakhand rolls use; `devanagari_fix` repairs it. These are the
# glyphs it lacks, read off Ghaziabad lists (applied before the shared repair).
GLYPHS_B_EXTRA: dict[str, str] = {
    "Ȩ": "द्य", "̱": "म्", "̢": "न्", "̏": "ख्", "Ɋ": "न्य", "ɰ": "ब्ल", "̭": "ब्", "ū": "द्र",
}
_SIGNATURE_A = set(GLYPHS) | set(SHORT_I)
_SIGNATURE_B = set("ƗƘŊŮˋȨ̢̱̏Ɋɰ̭ū")


def detect_font(text: str) -> str:
    """'A' (visual-order font) or 'B' (the Uttarakhand-roll font), by signature glyphs."""
    a = sum(text.count(c) for c in _SIGNATURE_A)
    b = sum(text.count(c) for c in _SIGNATURE_B)
    return "B" if b > a else "A"


def fix_font_b(s: str) -> str:
    """The glyphs this font adds to the shared table, then the shared repair (conjunct
    glyphs, visual-order short-i, reph) used for the Uttarakhand rolls."""
    from .devanagari_fix import clean
    for k, v in GLYPHS_B_EXTRA.items():
        s = s.replace(k, v)
    return clean(s)


def decode(text: str, font: str = "A") -> tuple[str, float]:
    """A cell to Unicode: Kruti Dev words converted, mis-mapped Unicode repaired according to
    the document's font. Confidence falls with the share of characters still undecoded."""
    if font == "A":
        # a short-i sign split from its consonant by an extracted space
        text = re.sub(rf"([{SHORT_I}])\s+(?=[ऀ-ॿ])", r"\1", text)
    out = []
    for tok in text.split():
        uni = any("ऀ" <= ch <= "ॿ" for ch in tok)
        if font == "B" and (uni or any(c in _SIGNATURE_B for c in tok)):
            out.append(fix_font_b(tok))
        # "[" alone is also Kruti Dev (ख), so it only marks the broken Unicode font next to
        # Devanagari or that font's other glyphs
        elif font == "A" and (uni or _BROKEN.search(tok.replace("[", ""))):
            out.append(fix_visual_unicode(tok))
        elif re.search(r"[A-Za-z]", tok) and not re.fullmatch(r"[A-Z0-9.\-/]+", tok):
            out.append(krutidev.convert(tok).replace("]", ","))   # Kruti ']' is a comma
        else:
            out.append(tok)
    s = " ".join(out)
    s = re.sub(r"्\s+(?=[ऀ-ॿ])", "्", s)            # a word split after a halant
    s = re.sub(r"&(?=\d)", "-", s)                   # Kruti Dev "&" is a hyphen ("ब्लाक&1")
    leftovers = rf"[a-z{''.join(GLYPHS)}{SHORT_I}{''.join(_SIGNATURE_B)}\[\]]"
    bad = len(re.findall(leftovers, s))
    return s.strip(), max(0.0, 1.0 - bad / max(len(s.replace(" ", "")), 1))


@dataclass
class PsRow:
    number: int
    locality: str | None
    building: str | None
    area: str | None
    electors: int | None
    remarks: str | None
    page: int
    confidence: float


@dataclass
class PsListResult:
    ac_number: int | None = None
    rows: list[PsRow] = field(default_factory=list)
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)

    font: str = "A"
    printed_total: int | None = None     # "total electors" printed under the table, if any

    @property
    def serial_coverage(self) -> float:
        """Stations found per number in the list's own range. A constituency split across
        districts (e.g. 58 Dholana, part in Hapur) lists only its own run of numbers."""
        if not self.rows:
            return 0.0
        numbers = [r.number for r in self.rows]
        return len(self.rows) / (max(numbers) - min(numbers) + 1)


def _column_centres(words) -> tuple[dict[int, float], float] | None:
    """x-centres of the numbered header row 1..10 and the y below it."""
    marks = [w for w in words if w[4].strip() in {str(i) for i in range(1, 11)}]
    best = None
    for w in marks:                          # the line with the most distinct 1..10 labels
        band = [m for m in marks if abs(m[1] - w[1]) < 14]
        labels = {m[4].strip() for m in band}
        if best is None or len(labels) > len(best[1]):
            best = (band, labels)
    if best is None or len(best[1]) < 8:
        return None
    chosen: dict[int, tuple] = {}
    for m in sorted(best[0], key=lambda m: m[1]):      # the topmost word per label
        chosen.setdefault(int(m[4]), m)
    # label -> x: a label that was not found must not shift the others onto wrong columns
    centres = {n: (m[0] + m[2]) / 2 for n, m in chosen.items()}
    # the header ends below its own labels, not below a station number "1" just under it
    return centres, max(m[3] for m in chosen.values())


def _rules(page) -> tuple[list[float], list[float]]:
    """x of vertical and y of horizontal ruling lines (lines or thin rectangles)."""
    vs, hs = set(), set()
    for g in page.get_drawings():
        for it in g["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.x - b.x) < 1 and abs(a.y - b.y) > 20:
                    vs.add(round(a.x))
                elif abs(a.y - b.y) < 1 and abs(a.x - b.x) > 20:
                    hs.add(round(a.y))
            elif it[0] == "re":
                r = it[1]
                if r.width < 2 and r.height > 20:
                    vs.add(round(r.x0))
                elif r.height < 2 and r.width > 20:
                    hs.add(round(r.y0))
    return sorted(vs), sorted(hs)


def _dedupe(words) -> list:
    """Drop words that overlap an already kept word by more than half its area — the
    duplicates left by edit patches pasted over the original row."""
    kept: list = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        area = max((w[2] - w[0]) * (w[3] - w[1]), 1e-6)
        if any(max(0, min(w[2], k[2]) - max(w[0], k[0])) * max(0, min(w[3], k[3]) - max(w[1], k[1]))
               > 0.5 * area for k in kept):
            continue
        kept.append(w)
    return kept


def _fill_labels(labels: dict[int, int], columns: int | None) -> dict[int, int]:
    """Complete a header whose numbers were not all found. On the standard ten-column grid
    where every found label sits in its own column, the missing ones do too; otherwise a
    missing label follows the label before it."""
    if columns == 10 and all(c == n for n, c in labels.items()):
        return {n: n for n in range(1, 11)}
    out = dict(labels)
    for n in range(2, 11):
        if n not in out and n - 1 in out and out[n - 1] + 1 not in out.values():
            out[n] = out[n - 1] + 1
    return out


def _fits_grid(page_rules: list[float], grid: list[float]) -> bool:
    """A continuation page drawn on the known grid: every rule it has is one of the grid's
    (the last page may lack a rule or two)."""
    return len(page_rules) >= len(grid) - 2 and all(any(abs(x - y) <= 3 for y in grid) for x in page_rules)


def parse_ps_list_up(path: Path, ac_number: int | None = None) -> PsListResult:
    """Rows of every page. The numbered header row (1..10) may be printed on the first page
    only (Annexure-3 lists): its label -> ruled-column map is then reused on pages with the
    identical ruled grid. Some lists add an unnumbered serial column; labels, not column
    positions, decide which cell is which."""
    res = PsListResult(ac_number=ac_number)
    found: dict[int, list[PsRow]] = {}
    layout: tuple[list[float], dict[int, int]] | None = None     # (grid, label -> column)
    printed_total: int | None = None
    with pymupdf.open(path) as doc:
        res.page_count = doc.page_count
        res.font = font = detect_font("".join(pg.get_text() for pg in doc))
        for pno, page in enumerate(doc, start=1):
            words = _dedupe(page.get_text("words"))
            m = _TOTAL.search(page.get_text())
            if m:
                printed_total = int(m.group(1))
            vlines, hlines = _rules(page)
            head = _column_centres(words)
            ruled = len(vlines) >= 11
            if ruled:
                bounds = vlines[1:-1]
            elif head is not None:
                xs = [head[0][n] for n in sorted(head[0])]
                bounds = [(xs[i] + xs[i + 1]) / 2 for i in range(len(xs) - 1)]
            else:
                bounds = []

            def col(w) -> int:
                return sum((w[0] + w[2]) / 2 > b for b in bounds) + 1

            if head is not None:
                centres, head_y = head
                labels = _fill_labels({n: sum(x > b for b in bounds) + 1 for n, x in centres.items()},
                                      len(vlines) - 1 if ruled else None)
                if ruled:
                    layout = (vlines, labels)
            elif layout is not None and len(vlines) >= 3 and _fits_grid(vlines, layout[0]):
                labels, head_y = layout[1], -1.0          # continuation page: same grid, no header
                bounds = layout[0][1:-1]
            else:
                digits = sum(1 for w in words if w[4].strip().isdigit())
                if len(hlines) >= 5 and digits >= 10:        # a table page that could not be read
                    res.warnings.append(f"page {pno}: column header not found")
                else:                                        # a notice / signature / summary page
                    res.warnings.append(f"page {pno}: no station table")
                continue
            table_bottom = max(hlines) if hlines else float("inf")
            ps_col = labels.get(1, 1)

            body = [w for w in words if (w[1] + w[3]) / 2 > head_y + 1 and w[3] <= table_bottom + 2]
            starts = sorted((w[1], int(w[4])) for w in body if col(w) == ps_col and w[4].strip().isdigit())
            # a row begins at the rule above its station number (or just above the number)
            tops = [max((h for h in hlines if h <= y + 2), default=y - 4) for y, _ in starts]
            for i, (y, number) in enumerate(starts):
                top = tops[i]
                bottom = tops[i + 1] if i + 1 < len(starts) else table_bottom
                cells: dict[int, list] = {}
                for w in body:
                    if top - 1 <= (w[1] + w[3]) / 2 < bottom and col(w) != ps_col:
                        cells.setdefault(col(w), []).append(w)

                def text(label: int) -> str:
                    c = labels.get(label)
                    ws = sorted(cells.get(c, []), key=lambda w: (round(w[1] / 3), w[0])) if c else []
                    return " ".join(w[4] for w in ws)

                loc, c1 = decode(text(2), font)
                bld, c2 = decode(text(3), font)
                area, c3 = decode(text(6), font)
                rem, _ = decode(text(10), font)
                digits = [int(t) for t in text(8).split() if t.isdigit() and 50 <= int(t) <= 3000]
                found.setdefault(number, []).append(PsRow(
                    number=number, locality=loc or None, building=bld or None, area=area or None,
                    electors=digits[-1] if digits else None, remarks=rem or None, page=pno,
                    confidence=round(min(c1, c2, c3) if area else min(c1, c2), 3)))
    res.printed_total = printed_total
    for number in sorted(found):
        copies = found[number]
        counts = {r.electors for r in copies if r.electors is not None}
        best = max(copies, key=lambda r: (r.electors is not None, r.confidence, len(r.area or "")))
        if len(counts) > 1:
            res.warnings.append(f"station {number}: copies disagree on electors {sorted(counts)}; count withheld")
            best.electors = None
        res.rows.append(best)
    return res
