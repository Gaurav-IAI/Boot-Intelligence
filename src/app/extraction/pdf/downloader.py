"""Download PDFs to disk with checksums, resume support and provenance."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)


@dataclass
class StoredPdf:
    path: Path
    url: str
    sha256: str
    size_bytes: int
    page_count: int | None
    is_text_pdf: bool | None
    text_chars: int
    reused: bool


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inspect_pdf(path: Path, sample_pages: int = 5) -> tuple[int, bool, int]:
    """Return (page_count, is_text_pdf, chars_found_in_sample).

    A PDF counts as text-based when the sampled pages yield a meaningful amount of
    extractable text; otherwise it is a scan and needs OCR.
    """
    with pymupdf.open(path) as doc:
        pages = doc.page_count
        chars = 0
        for i in range(min(sample_pages, pages)):
            chars += len(doc[i].get_text().strip())
    per_page = chars / max(min(sample_pages, pages), 1)
    return pages, per_page >= 100, chars


def download_pdf(
    fetch,
    dest: Path,
    url: str,
    *,
    resume: bool = True,
) -> StoredPdf:
    """`fetch` is a zero-arg callable returning the PDF bytes.

    With `resume`, an existing non-empty file on disk is reused instead of being
    re-fetched — the politeness rule that matters most when scaling up.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if resume and dest.exists() and dest.stat().st_size > 0:
        data = dest.read_bytes()
        pages, is_text, chars = inspect_pdf(dest)
        log.info("reusing cached %s (%d bytes)", dest.name, len(data))
        return StoredPdf(dest, url, sha256_bytes(data), len(data), pages, is_text, chars, True)

    data = fetch()
    if not data.startswith(b"%PDF"):
        raise ValueError(f"{url} did not return a PDF (first bytes: {data[:16]!r})")
    dest.write_bytes(data)
    pages, is_text, chars = inspect_pdf(dest)
    log.info("downloaded %s (%d bytes, %d pages, text=%s)", dest.name, len(data), pages, is_text)
    return StoredPdf(dest, url, sha256_bytes(data), len(data), pages, is_text, chars, False)


@dataclass
class StoredFile:
    path: Path
    url: str
    sha256: str
    size_bytes: int
    reused: bool


def download_file(fetch, dest: Path, url: str, *, magic: bytes | tuple[bytes, ...],
                  resume: bool = True) -> StoredFile:
    """Like `download_pdf` for any document type, checked against its leading `magic` bytes
    (one signature or several, e.g. .xls or .xlsx), so an HTML error page is never stored
    as the document."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if resume and dest.exists() and dest.stat().st_size > 0 and dest.read_bytes().startswith(magic):
        data = dest.read_bytes()
        log.info("reusing cached %s (%d bytes)", dest.name, len(data))
        return StoredFile(dest, url, sha256_bytes(data), len(data), True)
    data = fetch()
    if not data.startswith(magic):
        raise ValueError(f"{url} did not return the expected document (first bytes: {data[:16]!r})")
    dest.write_bytes(data)
    log.info("downloaded %s (%d bytes)", dest.name, len(data))
    return StoredFile(dest, url, sha256_bytes(data), len(data), False)
