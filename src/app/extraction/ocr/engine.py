"""OCR for scanned PDFs (Form 20 2017/2022, and any scanned roll).

Uttarakhand rolls and result sheets are published in Hindi only (confirmed from
the ECI portal's own `get-ac-languages` response: `{"HIN": "HINDI"}`), so an
English-only OCR configuration is not sufficient — Devanagari support is
required. Tesseract with the `hin` traineddata is the default because it is the
only engine that installs without a GPU or a large model download; PaddleOCR and
EasyOCR give better Devanagari accuracy and are the recommended next step.

Tesseract is an OPTIONAL, external dependency. If it is not installed,
`ocr_available()` returns False with a reason and the pipeline records the page
as `extraction_status='blocked_no_ocr_engine'` rather than silently producing
empty rows.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from ...config import settings

log = logging.getLogger(__name__)

PARSER_NAME = "tesseract_devanagari"


@dataclass
class OcrPage:
    page_number: int
    text: str
    mean_confidence: float
    word_count: int


@dataclass
class OcrResult:
    engine: str
    languages: str
    dpi: int
    pages: list[OcrPage] = field(default_factory=list)
    available: bool = True
    reason: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    @property
    def mean_confidence(self) -> float:
        scored = [p.mean_confidence for p in self.pages if p.word_count]
        return round(sum(scored) / len(scored), 4) if scored else 0.0


_DEFAULT_INSTALLS = (
    Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
)


def _tesseract_cmd() -> str | None:
    if settings.tesseract_cmd:
        return settings.tesseract_cmd
    found = shutil.which("tesseract")
    if found:
        return found
    # The Windows installer does not add itself to PATH.
    return next((str(p) for p in _DEFAULT_INSTALLS if p.exists()), None)


def tesseract_config() -> str:
    """Point Tesseract at the project's language data, if present.

    Uses TESSDATA_PREFIX instead of `--tessdata-dir`: pytesseract does not unquote
    config arguments on Windows, so a data path containing spaces would break.
    Returns extra CLI config (currently none) for call sites to pass through.
    """
    d = settings.tessdata_dir
    if d and Path(d).is_dir() and any(Path(d).glob("*.traineddata")):
        os.environ["TESSDATA_PREFIX"] = str(Path(d))
    return ""


def ocr_available() -> tuple[bool, str]:
    """Whether OCR can run here, and why not if it cannot."""
    cmd = _tesseract_cmd()
    if not cmd:
        return False, (
            "tesseract is not installed or not on PATH. Install it and the 'hin' "
            "language data, or set TESSERACT_CMD in .env. "
            "Windows: https://github.com/UB-Mannheim/tesseract/wiki"
        )
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = cmd
        langs = set(pytesseract.get_languages(config=tesseract_config()))
    except Exception as exc:
        return False, f"tesseract found at {cmd} but is not usable: {exc}"

    wanted = set(settings.ocr_languages.split("+"))
    missing = wanted - langs
    if missing:
        return False, (
            f"tesseract at {cmd} is missing language data for {sorted(missing)}. "
            f"Uttarakhand rolls are Hindi-only, so 'hin' is required."
        )
    return True, f"tesseract at {cmd} with languages {sorted(wanted)}"


def render_page(page: pymupdf.Page, dpi: int | None = None) -> bytes:
    dpi = dpi or settings.ocr_dpi
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes("png")


def ocr_pdf(path: Path, max_pages: int | None = None) -> OcrResult:
    """OCR a scanned PDF. Returns a result flagged unavailable if no engine."""
    ok, reason = ocr_available()
    result = OcrResult(
        engine=PARSER_NAME, languages=settings.ocr_languages, dpi=settings.ocr_dpi,
        available=ok, reason=reason,
    )
    if not ok:
        log.warning("OCR unavailable: %s", reason)
        return result

    import io

    import pytesseract
    from PIL import Image

    pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd()

    with pymupdf.open(path) as doc:
        n = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
        for pno in range(n):
            png = render_page(doc[pno])
            img = Image.open(io.BytesIO(png))
            data = pytesseract.image_to_data(
                img, lang=settings.ocr_languages, config=tesseract_config(),
                output_type=pytesseract.Output.DICT,
            )
            words, confs = [], []
            for txt, conf in zip(data["text"], data["conf"]):
                if not txt.strip():
                    continue
                words.append(txt)
                try:
                    c = float(conf)
                except (TypeError, ValueError):
                    continue
                if c >= 0:
                    confs.append(c / 100.0)
            result.pages.append(OcrPage(
                page_number=pno + 1,
                text=" ".join(words),
                mean_confidence=round(sum(confs) / len(confs), 4) if confs else 0.0,
                word_count=len(words),
            ))
            log.info("OCR page %d/%d: %d words, confidence %.2f",
                     pno + 1, n, len(words), result.pages[-1].mean_confidence)
    return result
