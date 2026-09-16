# POC Final Report — Uttarakhand Election Data Extraction & Booth Intelligence

**Date:** 2026-09-14
**Scope:** Uttarakhand only (state code `S28`, discovered at runtime)
**Status:** working end-to-end on PostgreSQL, from live official sources

Every figure below was produced by running the pipeline against the live sources
and reading the database afterwards. Nothing here is estimated.

---

## Headline result

| Metric | Value |
|---|---|
| Elector records extracted | **5,544** across 5 booths |
| Expected (per the documents) | 5,554 |
| **Extraction rate** | **99.82%** |
| Mean extraction confidence | **99.65%** |
| Records passing validation | 98.88% (5,482) |
| HTTP request success | 100% (19/19 on a clean run; fewer when `--resume` reuses cached PDFs) |
| Polling stations catalogued | 239 |
| Booth-level results (Form 20 2012) | 152 booths, 151 pass the arithmetic self-check |
| Official 2003→2025 part mappings | 17 |
| Automated tests | 65, all passing, no network |
| CAPTCHA / auth bypasses implemented | **0** |

The 10-record shortfall is **not** extraction failure: those serial numbers do
not appear anywhere in the published PDFs. Verified by scanning every word of
every page.

---

## 1. What official data was successfully obtained?

| Layer | Source | Volume |
|---|---|---|
| State + official code | ECI gateway `/common/states` | `Uttarakhand → S28`, stateId 27 |
| Districts | ECI gateway `/common/districts/S28` | 13 |
| Assembly constituencies | ECI gateway `/common/acs/{districtCd}` | 10 for Dehradun (70 statewide available) |
| Polling stations (current) | CEO UK `/asdlist/SearchAdsEpic/Parts` | 234 for AC 19 Raipur |
| Station names / areas | CEO UK `PSSIR2026/19.pdf` | 234 enriched |
| Polling stations (2003) | CEO UK legacy `/part-names` | 98 for AC 15 Rajpur |
| **Electoral rolls** | CEO UK **Legacy Roll 2003** PDFs | **5 parts, 200 pages, 5,544 electors** |
| Part mapping | CEO UK `/village-details` | 17 village-level 2003→2025 rows |
| Booth results | CEO UK Form 20 Vidhan Sabha 2012 | 152 booths, 72,376 total votes |

The complete district → AC master for all **13 districts / 70 ACs** is also
available from the PS-list index page, so statewide expansion needs no discovery
work.

---

## 2. Which ECI APIs worked?

**Fully public, no auth, no CAPTCHA — automated:**

| Endpoint | Result |
|---|---|
| `GET /api/v1/common/states` | 200, 36 rows |
| `GET /api/v1/common/districts/{stateCd}` | 200, 13 rows for S28 |
| `GET /api/v1/common/acs/{districtCd}` | 200, 10 rows for S2813 |

These three carry `stateCd`, `districtCd`, `asmblyNo`, `acId`, `category` and
`pcNo`, so the whole hierarchy is keyed on official codes, never on names.

**Reached 200 for the portal but not reproducible externally:**

| Endpoint | Why not |
|---|---|
| `GET /printing-publish/get-publish-eroll-type` | query-parameter *values* are encrypted client-side and rotate per page load |
| `POST /printing-publish/get-ac-languages` | same encryption; body is not readable JSON |
| `POST /printing-publish/get-publish-part-list` | same; a plaintext POST returns 400 with an empty body |

Reproducing that encryption would mean reverse-engineering a protection
mechanism, so these are documented and **not automated**. Two useful facts were
still obtained by observing them: Uttarakhand's only published 2026 roll is
**SIR DraftRoll – 2026 (published 2026-07-14)**, and its rolls are published in
**Hindi only** (`{"HIN":"HINDI"}`) — which is what forces Devanagari OCR.

**Not called at all:** `GET /authn-voter/validate-token` (401 anonymous).

---

## 3. Which endpoints required CAPTCHA?

| Endpoint / flow | Evidence |
|---|---|
| **ECI e-roll PDF download** (SIR Draft 2026) | page loads `captcha-service/getCaptcha/EROLL` and renders a required `Captcha *` field directly above "Download Selected PDFs" (`research/network/04_eroll_filled.png`) |
| ECI voice CAPTCHA (accessibility) | `captcha-service/generateVoiceCaptcha/{id}` |
| CEO UK `SearchAdsEpic/SearchEpic` (EPIC search) | `SearchAdsEpic/Captcha` |
| CEO UK `SearchAdsEpic/DownloadAsdPdf` | same |
| CEO UK `SearchAdsEpic/DownloadBlaMinutes` | same |
| CEO UK roll portal 2018–2023 (`election.uk.gov.in/`) | `Captcha1` control inside `__VIEWSTATE` |

**None of these were solved, submitted, replayed or bypassed.** There is a test
(`tests/test_sources.py::TestNoCaptchaEndpointsAreImplemented`) that fails if any
of them ever appears as a call in the source adapters.

Also encountered and **not** circumvented: directory listings under `/Pdf_Roll/`
and `/PSSIR2026/` return **403**. Document indexes come from the official HTML
index pages instead.

---

## 4. Which data required PDF extraction?

Everything except the three ECI JSON endpoints and two CEO Uttarakhand JSON
endpoints:

| Document | Pages processed | Text layer? |
|---|---|---|
| Legacy Roll 2003, AC 15 parts 6–10 | 200 | yes |
| Polling Station List 2026, AC 19 | 18 | yes (Kruti Dev) |
| Form 20 Vidhan Sabha 2012, AC 19 | 13 | yes (Kruti Dev) |

All three are **text-based**, so OCR was not needed for any data that actually
reached the database. Each PDF is stored with its SHA-256, byte size, page count
and source URL.

---

## 5. Which data required OCR?

| Document | Status |
|---|---|
| Form 20 Vidhan Sabha **2022** | scanned images, **0 extractable characters** on page 1 → OCR required |
| Form 20 Vidhan Sabha **2017** | has an embedded OCR layer, but it is corrupted beyond use → would need re-OCR from the page images |

The OCR module is implemented (`src/app/extraction/ocr/engine.py`): it detects
text-vs-scan before deciding, renders at 300 dpi, runs Tesseract with `hin+eng`,
and returns a per-page mean confidence.

**It was not demonstrated on a real scan**, because Tesseract was not installed
in this environment. That is reported rather than worked around: `ocr_available()`
returns `(False, reason)`, and the pipeline records such a document as blocked
instead of writing empty rows. `python -m app inspect-roll --roll <pdf>` prints
the engine status.

For production, Tesseract Hindi is the cheapest option but not the best;
**PaddleOCR or EasyOCR give materially better Devanagari accuracy** and should be
benchmarked before committing.

---

## 6. Which fields were successfully extracted?

Per elector, from the roll's eight printed columns:

| Field | Coverage | Notes |
|---|---|---|
| serial number | 100% | |
| elector name | 100% | Devanagari, conjuncts repaired |
| relative name | 100% | |
| relative type | 100% | normalised FATHER / MOTHER / HUSBAND / OTHER |
| gender | 100% | normalised M / F / O / UNKNOWN (2,925 M, 2,618 F) |
| age | 100% | validated to 18–120; implausible values → NULL |
| EPIC | **48.7%** (2,702 of 5,544) | genuinely optional in the source |
| source page | 100% | |
| raw text | 100% | preserved for every record |
| confidence | 100% | mean 99.65% |

Plus, per booth: part number, polling-station number and name, areas covered,
roll year, roll type, qualifying date.

Form 20 2012, per booth: station serial, station name, the per-candidate vote
vector, total valid votes, tendered votes, total votes.

---

## 7. Which fields were unavailable?

| Field | Why | Stored as |
|---|---|---|
| house number | column exists in the 2003 roll but is **empty throughout** | NULL |
| section number | not printed in this roll format | NULL |
| EPIC (51.3% of rows) | optional in the source | NULL |
| candidate names ↔ vote columns (Form 20) | rotated headers cannot be matched to columns; column count varies 21/22/23 | `candidate_name` NULL; only labelled totals stored |
| party affiliation | not in Form 20 2012 | NULL |
| NOTA | not a column in 2012 | NULL |
| **phone / mobile / email** | **not in electoral rolls at all** | **no such column on `electors`** |

No field was invented, and no field was back-filled by inference.

---

## 8. How many sample booths were processed?

**5 booths** — AC 15 (Rajpur, 2003 delimitation) parts 6, 7, 8, 9, 10 — chosen
because the official mapping shows they fall inside today's AC 19 (Raipur),
which is also the AC used for the current-hierarchy and Form 20 work.

**239 polling stations** are catalogued in total (234 current + 5 with rolls),
and **152 booths** have 2012 results.

---

## 9. Accuracy and mismatch statistics

| Part | Pages | Extracted | Expected | Diff | Gaps | Confidence | EPIC |
|---|---|---|---|---|---|---|---|
| 6 | 12 | 342 | 342 | **0** | 0 | 99.87% | 212 |
| 7 | 38 | 1,061 | 1,072 | −11 | 11 | 99.71% | 694 |
| 8 | 27 | 758 | 760 | −2 | 3 | 99.71% | 581 |
| 9 | 78 | 2,107 | 2,105 | +2 | 1 | 99.60% | 621 |
| 10 | 45 | 1,276 | 1,275 | +1 | 0 | 99.60% | 594 |
| **Total** | **200** | **5,544** | **5,554** | **−10** | **15** | **99.65%** | **2,702** |

**Reading the discrepancies honestly:**

* **Negative differences (parts 7, 8) are gaps in the source, not misses.** Serials
  354–357, 686, 757–758, 822–823, 1007–1009 of part 7 appear nowhere in the PDF —
  deleted electors. Confirmed by scanning every word on every page.
* **Positive differences (parts 9, 10) are duplicate or stray serials in the
  source.** Part 10 has 1,276 rows numbered 1–1275 and then prints **2220** on
  its final row. Taking the highest serial as the roll's length would have
  invented ~900 phantom missing electors, so such outliers are detected,
  excluded from the expected-count basis, and shown in the UI as
  *"excluding 1 stray serial(s) [2220] present in the source"*.
* **52 duplicates flagged, 0 deleted** — 3 by exact part+serial, the rest by
  matching name + relative + age (mostly genuine namesakes in large urban booths).
  Each is linked to the row it duplicates with a `duplicate_kind` for review.
* **1.12% of records fail a validation rule** — almost all an unrecognised EPIC
  format or a fuzzy-duplicate flag. They are stored and marked, never dropped.
* **~3% of records carry at least one unmapped conjunct glyph.** Those glyphs are
  deliberately left unmapped rather than guessed, and they lower the row's
  confidence score accordingly.

**Form 20 2012:** 151 of 152 booths pass the document's own arithmetic (candidate
votes must sum to the printed total valid votes, with the sheet's column count) —
**99.3%**. The single row requiring review is part 141: one printed candidate cell
is blank, so its columns cannot be aligned to candidates and its margin is withheld.
(An earlier parser version misread part 87; that was corrected and part 87 now
reconciles with the source sheet.)

**"Official count" is a stated proxy, not a published figure.** The 2003 roll
prints no summary total, so the expected count is the highest serial number in
the document, with stray serials excluded. That basis is stored per roll
(`official_count_basis`) and displayed everywhere the difference is shown.

---

## 10. What can be scaled?

Straightforwardly, with no new research:

| Work | Effort | Volume |
|---|---|---|
| All 70 ACs of the current hierarchy | minutes | 3 API calls per district |
| All polling stations statewide | ~70 requests | roughly 12,000 stations |
| All PS List 2026 PDFs | 70 downloads | ~50 MB |
| **All 2003 roll PDFs statewide** | **~11,000 downloads** | the API exposes every part of every AC |
| Form 20 2012 for all 70 ACs | 70 downloads | ~10,000 booth results |
| Part mapping statewide | one lookup per village | one-to-many, already handled |

The architecture supports it: one adapter per source, idempotent upserts,
`--resume` reusing downloaded files, and conservative pacing. At the default
3 concurrent / 0.4 s spacing, the full 2003 roll is roughly **2–3 days** of
polite fetching — and that pace should not be raised without checking the site's
terms.

Adding another state means writing a CEO adapter for it; the database, parsers
and analytics are unchanged. The ECI portion already works for all 36 states.

---

## 11. What cannot currently be automated?

1. **Current-year (SIR 2026) roll PDFs** — CAPTCHA-gated. This is the most
   significant limitation, and it is a deliberate stop.
2. **ECI `printing-publish/*` endpoints** — client-side encrypted parameters.
3. **CEO UK roll portal 2018–2023** — CAPTCHA in `__VIEWSTATE`.
4. **ASD lists and BLO-BLA minutes** — CAPTCHA on download.
5. **Per-elector EPIC search** — technically open, deliberately unimplemented:
   enumerating booths through a search API is the brute-force pattern the brief
   prohibits, and the published PDF gives the same data legitimately.
6. **Form 20 2017** — its embedded OCR layer is corrupt; it needs re-OCR.
7. **Directory listings** — 403, not circumvented.

---

## 12. What requires manual intervention?

| Task | Who | How often |
|---|---|---|
| Downloading a current-roll PDF | a person solving the CAPTCHA on the ECI portal | per part needed |
| Installing Tesseract + `hin` (or PaddleOCR) | ops, once | once per environment |
| Reviewing flagged duplicates | data analyst | per ingest |
| Reviewing rows below 95% confidence | analyst who reads Devanagari | per ingest |
| Extending the conjunct map for new glyphs | developer, from the confidence report | rare |
| Re-verifying source URLs | developer | quarterly, or when a fetch fails |
| Legal sign-off before contact-data work | counsel | before any such work |

Once a roll PDF exists on disk — however it got there — the pipeline is fully
automatic: `python -m app extract-roll --roll <file>`.

---

## 13. What should be the next phase?

**Phase 1 — harden what exists (1–2 weeks)**

1. Install Tesseract `hin` (and benchmark PaddleOCR against it) so the OCR path
   is demonstrated on Form 20 2022, not just unit-tested.
2. Scale to one full AC: all ~98 parts of AC 15, then all of AC 19's current
   stations. Watch for layout variants the 5-part sample did not show.
3. Add Alembic migrations — the POC uses `create_all`, which is fine for a POC
   and not fine for a system that accumulates data.
4. Extend the conjunct map from the confidence report until the unmapped-glyph
   rate is below 1%.

**Phase 2 — statewide (3–4 weeks)**

5. All 70 ACs: hierarchy, polling stations, PS lists, Form 20 2012.
6. Statewide 2003 roll ingest as a resumable, rate-limited background job with
   per-part status, run over days rather than hours.
7. Statewide part mapping, with the one-to-many relationships preserved.
8. Turn the quality dashboard into a monitored job — alert on a drop in
   extraction rate or confidence, which is how a silent upstream layout change
   gets caught.

**Phase 3 — decisions that are not engineering**

9. **Get a legal position on bulk collection** before running Phase 2 at full
   scale. A published document being fetchable is not the same as permission to
   build a derived national database, and aggregation changes the risk profile.
10. **Decide whether current-year rolls are needed.** If they are, the honest
    options are a data-sharing request to the CEO/ECI, or a supervised manual
    download workflow — not CAPTCHA solving.
11. **Keep contact data out** until there is a lawful basis and a consent
    mechanism. The schema already separates it; keep it that way.

**Phase 4 — beyond Uttarakhand**

12. Add a second state to prove the adapter boundary holds. Expect each CEO site
    to differ as much as Uttarakhand's three Form 20 editions differ from each
    other — that variance is the real cost of national coverage, not the volume.

---

## Acceptance criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
| A | Discover Uttarakhand → District → AC → Polling Station | **met** | 13 districts, 10 ACs, 234 stations; `python -m app discover-*` |
| B | Ingest ≥1 real official electoral-roll document | **met** | 5 real roll PDFs, 200 pages |
| C | Extract structured elector records | **met** | 5,544 records, 8 fields |
| D | Compare extracted vs official count | **met** | per-booth diff and %, basis always stated |
| E | Data stored in PostgreSQL | **met** | PostgreSQL 17 via `docker compose` |
| F | Booth-level statistics | **met** | `analyze-booth`, `quality`, dashboard |
| G | Every record traceable to source document + page | **met** | source, URL, document, page, raw text, parser version, confidence |
| H | Reproducible without editing code | **met** | `python -m app pipeline`; idempotent, `--resume` |
| I | No CAPTCHA or security bypass | **met** | 0 implemented; enforced by test |

---

## Honest summary

The POC proves the pipeline works on real official Uttarakhand data, end to end,
at 99.8% extraction accuracy with complete provenance — and it does so **without
touching a single protected mechanism**.

The one thing it does **not** prove is access to *current* electoral rolls. Those
are CAPTCHA-gated, and no amount of engineering changes that; it is a permissions
question, not a technical one. What the POC does show is that the moment a
current roll PDF is available by any legitimate route, everything downstream —
parsing, validation, storage, analytics — already works.

Two findings are worth carrying forward beyond this project, because both would
have silently corrupted the data if missed: **the Hindi text in these documents
is not Unicode** (two different legacy encodings, in different documents), and
**the published rolls contain real data-entry anomalies** — gaps, duplicate
serials, and a stray serial 945 places beyond the end of its booth. A pipeline
that trusts the highest serial number as the roll's length will report a 74%
extraction failure on that booth and be wrong.
