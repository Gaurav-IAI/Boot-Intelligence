# Project Overview — Uttarakhand Booth Intelligence POC

A concise description of what this application does, where its data comes from,
what it produces, and what a user actually gets out of it.

Longer companions: [POC_FINAL_REPORT.md](POC_FINAL_REPORT.md) (results),
[DATA_SOURCE_RESEARCH.md](DATA_SOURCE_RESEARCH.md) (why these sources),
[DEPLOYMENT.md](DEPLOYMENT.md) (how to run it).

---

## 1. What it is

A working pipeline that rebuilds the official electoral hierarchy for
**Uttarakhand (S28)** —

```
State → District → Assembly Constituency → Polling Station (Part) → Electoral Roll → Elector
```

— from public ECI and CEO Uttarakhand sources, stores it in PostgreSQL with full
provenance, and serves a browsable booth-intelligence dashboard on top of it.

The dashboard also covers **Uttar Pradesh (S24)** and **Telangana (S29)**, which
are chosen from a header dropdown. For these two states only the ECI district and
constituency list is loaded. Their booth-level panels say "Not yet ingested"
until a CEO adapter exists for them.

It is a **data acquisition and verification** system, not a campaign tool. Every
figure it shows is traceable to a specific published document, page and checksum,
and anything it could not verify is labelled as unverified rather than estimated.

---

## 2. What it does, stage by stage

One command (`python -m app serve`) runs the whole flow; each stage is skipped
when its data is already present, so restarts are fast and idempotent.

| Stage | Work | Code |
|---|---|---|
| **hierarchy** | State code discovered by name (never hard-coded) → districts → constituencies | `sources/eci_api/` |
| **parts** | Current polling stations for each constituency | `sources/ceo_uttarakhand/` |
| **ps-list** | Station names, buildings and area descriptions from the PS List 2026 PDFs | `extraction/parsers/ps_list_2026.py` |
| **rolls** | Download roll PDF → checksum → text-vs-scan detection → parse electors → validate → flag duplicates → store | `extraction/`, `services/validation.py` |
| **mapping** | Official 2003 → 2025 part mapping, looked up by the villages printed on each roll | `services/pipeline.py` |
| **ps-list-2024** | OCR of the scanned 2024 station list, used only to *suggest* a match on review rows | `extraction/ocr/` |
| **form20** | Booth-wise election results (Form 20) with per-candidate vote columns | `extraction/parsers/form20_2012.py` |
| **verify** | Offline re-check: row counts, duplicate links, result-to-constituency links | `services/verification.py` |
| **review** | Writes the CSV/JSON review queues | `services/review.py` |

Two non-obvious problems the parsers handle: **Kruti Dev** legacy font encoding
(PS List 2026, Form 20 2012) and **conjuncts mapped into Latin-Extended**
(Roll 2003). Ambiguous glyphs are left unmapped so they *lower the confidence
score* instead of being guessed at, and the raw text is always kept alongside the
converted text.

---

## 3. Data sources

16 sources are catalogued in `config/sources.yaml`, each with its access type and
whether it may be automated. The ones that actually feed the database:

| Source | What it yields | Access |
|---|---|---|
| ECI gateway `/common/states`, `/districts`, `/acs` | State code, 13 districts, 70 constituencies | Public JSON |
| CEO UK SIR 2026 `Parts` endpoint | Current polling stations per constituency | Public JSON |
| CEO UK Polling Station List 2026 (PDF) | Station names, buildings, area descriptions | Public PDF |
| CEO UK **Legacy Roll 2003** (PDF) | **Full elector records** — name, relative, age, gender, EPIC, house | Public PDF |
| CEO UK `village-details` | Official 2003 → 2025 part mapping | Public JSON |
| CEO UK **Form 20, 2012** (PDF) | Booth-wise votes, per-candidate columns, NOTA, rejected, totals | Public PDF |
| CEO UK Polling Station List 2024 (scanned PDF) | OCR suggestions for unresolved mapping rows | Public PDF, OCR |

**Deliberately not used:** the current SIR 2026 roll download and the 2018–2023
roll portal are **CAPTCHA-gated**, and the ECI roll endpoints encrypt their
parameters client-side. Neither is worked around — they are recorded as blocked,
and an official ungated alternative (the 2003 legacy roll) is used instead. The
extraction path does not care how a PDF was obtained, so a manually downloaded
SIR 2026 roll can be fed straight to `extract-roll`. See
[PRIVACY_AND_COMPLIANCE.md](PRIVACY_AND_COMPLIANCE.md).

---

## 4. What is stored

Ten tables (`src/app/database/models.py`), all with source, source URL, source
document, page number, parser version and extraction confidence carried on the
row itself. Current contents of the populated database:

| Table | Rows | Notes |
|---|---|---|
| `states` / `districts` | 1 / 13 | Uttarakhand, S28 |
| `assembly_constituencies` | 71 | 70 current + 1 in 2003 delimitation |
| `polling_stations` | 12,548 | 12,543 SIR-2026 + 5 ROLL-2003; 3,772 carry area descriptions |
| `electoral_rolls` | 5 | AC 15 (2003) parts 6–10, with checksum, page count, text/scan status |
| `electors` | 5,544 | 2,702 carry an EPIC; mean extraction confidence 99.65% |
| `election_results` | 6,647 | Form 20 2012, booth-wise, across 48 constituencies |
| `part_mapping` | 22 | Official village mapping, 2003 → current |
| `source_fetches` | 380 | Every HTTP request: status, bytes, SHA-256, local path |
| `contact_records` | 0 | Consent-gated by design; nothing ingested |

**Booth numbers are never used as a join key.** Part numbering differs between
2003, 2012, the 2025 mapping and SIR 2026, so a booth is linked to its historical
self only through the official mapping table — never by matching part numbers.

---

## 5. What you get out

### The dashboard (`http://127.0.0.1:8000`)

| Page | What it answers |
|---|---|
| `/` | State-level summary: coverage, mapping status, which constituencies have results |
| `/constituencies`, `/district/{id}` | What data exists for every constituency, district by district |
| `/ac/{id}` | Booth list with electorate figures, flags and a **Booth Data Health** score (0–100), plus written observations |
| `/ac/{id}/performance` | Historical booth results: turnout, margins, closest and widest booths, margin histogram |
| `/ac/{id}/changes` | Booth lineage — which 2003 part became which current part, and on what official evidence |
| `/station/{id}` | Single booth: station details, roll provenance, paginated elector records, mapping lineage |
| `/result/{id}` | One booth's Form 20 row: every vote column, totals, and whether the sum check passes |
| `/quality` | Per-roll extraction quality, review queues, active database backend |
| `/search` | Free-text search across constituencies, stations and places |

### Per-booth analytics

Total electors, gender split, age bands (18-25 / 26-40 / 41-60 / 61+), serial
range and gaps, EPIC coverage, official vs extracted count with the difference,
mean confidence, rows below threshold, duplicates flagged, and the source
document with its URL and checksum.

### Files on disk

```
data/raw/…                              every source PDF, unmodified, checksummed
data/processed/review/quality_report.json   headline quality + per-roll status
data/processed/review/mapping_verification.csv  every mapping row and its status
data/processed/review/record_review.csv     flagged/low-confidence records (no names)
data/processed/review/result_review.csv     Form 20 rows failing or lacking the sum check
data/processed/ocr/ps_list_2024_AC{n}.json  OCR output, suggestion only
```

### CLI

`discover-state`, `discover-districts`, `discover-acs`, `discover-parts`,
`inspect-roll`, `extract-roll`, `analyze-booth`, `pipeline`, `quality`, `serve`.

---

## 6. How it decides what is verified

The verification standard is deliberately conservative, and every page and export
reads the **same calculation** — there is no second, friendlier number anywhere.

* A **mapping row** is `Verified` only on official documentary evidence. OCR text
  similarity can raise a *"Possible: Part N"* suggestion on a review row; it can
  never promote that row to Verified.
* A **Form 20 row** is verified only when its candidate columns sum to the printed
  total (`verified + review == rows`, always). Current data: 6,491 of 6,647
  verified, 156 in review.
* **Mapping status today:** 11 Verified, 9 Review Required, 2 Not Mapped.
* **Records are never deleted.** Duplicates and validation failures are flagged,
  linked to their original, and kept.
* **Booth Data Health (0–100)** scores completeness and traceability only —
  electorate data 20, record validation 20, extraction confidence 15, provenance
  15, historical result link 15, official mapping 15. It is not a political
  indicator.

---

## 7. What it deliberately does not do

* No CAPTCHA bypass, no authentication bypass, no voter-search brute-forcing.
* No contact data, no phone numbers — `contact_records` exists with a consent
  field and is empty.
* No inference of caste, religion or political preference from names.
* No guessed values: unreadable glyphs lower confidence, unmatched booths stay
  unmatched, missing data is displayed as missing.
* Form 20 candidate names are not attributed to vote columns — the rotated
  headers cannot be matched reliably, so only labelled totals are stored.

---

## 8. Honest limits

1. Current-year rolls cannot be fetched automatically (CAPTCHA); manual download
   works with the same parser.
2. Elector records exist for **5 booths of one 2003 constituency**, not the whole
   state — enough to prove the pipeline, not a statewide dataset.
3. Form 20 2017/2022 need OCR; that path is implemented and unit-tested but not
   demonstrated on a real scan.
4. PS List 2026 cell boundaries are imprecise (merged cells); locality and
   building are stored joined rather than wrongly split.
5. ~3% of elector rows carry at least one unmapped conjunct glyph, reflected in
   their confidence score.
6. `section_number` stays NULL — the 2003 roll format does not print it.
7. Scale is untested: five booths, not seventy constituencies.

---

## 9. Why the architecture is shaped this way

One source = one adapter (`src/app/sources/`), one document format = one parser
(`src/app/extraction/parsers/`). The database, pipeline and dashboard know
nothing about any specific portal. Adding a state, a year, or a new roll format
means adding an adapter or a parser — not rewriting the pipeline. States are
listed in `src/app/states.py`. Booth-level stages run only for the state that has
a CEO adapter. Rows keyed by AC number (Form 20 results, part mappings) are
matched only within their own state, because every state has an AC 19.

65 tests run offline in about a second, parsing slices of the **real** published
PDFs committed under `tests/fixtures/`.
