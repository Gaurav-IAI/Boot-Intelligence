"""Telangana Form 20 (booth-level) — not loaded yet.

Constituency-level results for 2018 and 2023 are loaded from the ECI statistical
reports instead (services/ac_results.py; `python -m app results --state Telangana`).

The CEO Telangana Form 20 files (2018: GE_2018/Form-20/INDEX.html, 2023:
GE_2023/FORM-20/INDEX.html, 119 PDFs each) are public, but scanned: 2023 has no
text layer and 2018's embedded OCR layer reads digits as letters (l0, t2, I).

Local OCR was evaluated on 2023 AC 1 (Sirpur), with rows checked by the sheet's own
arithmetic (candidate votes = total valid; valid + rejected + NOTA = total):
Tesseract (eng, digits only, per page or per cell) and RapidOCR (per cell, with a
Tesseract fallback) each reconciled well under 10% of rows. A per-cell prototype on
2018 sheets reconciled about 79% of rows, but has not been run over all 119. Under the
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
        "Telangana Form 20 is published only as scanned PDFs, so booth-level rows are not loaded "
        "(constituency-level results come from the ECI statistical reports). Options: a stronger "
        "table-OCR service with the same row-arithmetic check, or the Returning Officers' "
        "soft copies from CEO Telangana. Index pages: "
        + ", ".join(f"{y}: {u}" for y, u in sorted(TG_FORM20_INDEXES.items())))
