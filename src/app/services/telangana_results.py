"""Telangana Form 20 — not loaded yet.

The CEO Telangana Form 20 files (2018: GE_2018/Form-20/INDEX.html, 2023:
GE_2023/FORM-20/INDEX.html, 119 PDFs each) are public, but scanned: 2023 has no
text layer and 2018's embedded OCR layer reads digits as letters (l0, t2, I).

Local OCR was evaluated on 2023 AC 1 (Sirpur), with rows checked by the sheet's own
arithmetic (candidate votes = total valid; valid + rejected + NOTA = total):
Tesseract (eng, digits only, per page or per cell) and RapidOCR (per cell, with a
Tesseract fallback) each reconciled well under 10% of rows. Under the
verification standard nothing below that would be stored, so this adapter
refuses rather than filling the dashboard with unverifiable numbers.
"""
from __future__ import annotations

TG_FORM20_INDEXES = {
    2023: "https://ceotelangana.nic.in/GE_2023/FORM-20/INDEX.html",
    2018: "https://ceotelangana.nic.in/GE_2018/Form-20/INDEX.html",
}


def load_telangana(db, http, run, *, code, years, acs, resume, refresh):
    raise LookupError(
        "Telangana Form 20 is published only as scanned PDFs, and local OCR (Tesseract, RapidOCR) "
        "reconciled under 10% of rows in testing, so nothing is loaded. Options: a stronger "
        "table-OCR service with the same row-arithmetic check, or the Returning Officers' "
        "soft copies from CEO Telangana. Index pages: "
        + ", ".join(f"{y}: {u}" for y, u in sorted(TG_FORM20_INDEXES.items())))
