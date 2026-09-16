# Uttarakhand Vidhan Sabha — Election Data Extraction & Booth Intelligence POC

A working proof of concept that builds

```
State → District → Assembly Constituency → Polling Station / Part → Electoral Roll → Elector records
```

for **Uttarakhand (S28)** from official, publicly accessible sources, stores it in
PostgreSQL, and reports booth-level statistics with full provenance.

**It contains no CAPTCHA bypass, no authentication bypass and no voter-search
brute-forcing.** Where a source is gated, that is detected, documented, and an
official alternative is used. See
[docs/PRIVACY_AND_COMPLIANCE.md](docs/PRIVACY_AND_COMPLIANCE.md).

---

## What it actually does (verified end-to-end)

| Stage | Source | Result |
|---|---|---|
| State code | ECI gateway `/common/states` | `Uttarakhand → S28`, **discovered, never hard-coded** |
| Districts | ECI gateway | 13 |
| ACs | ECI gateway | 10 in Dehradun (70 statewide) |
| Polling stations | CEO Uttarakhand SIR 2026 JSON | 234 for AC 19 Raipur |
| Station names / areas | CEO Uttarakhand PS List 2026 PDF | 234 enriched (Kruti Dev transliterated) |
| **Electoral rolls** | CEO Uttarakhand **Legacy Roll 2003** PDFs | **5 parts, 5,544 elector records** |
| Part mapping | CEO Uttarakhand `village-details` | 17 official 2003→2025 mappings |
| Booth results | CEO Uttarakhand Form 20 (2012) | 152 booths, 151 pass the votes-sum check |

**Extraction rate 99.82%** (5,544 extracted / 5,554 expected) ·
**mean confidence 99.65%** · **HTTP success 100% (19/19)** · **65 tests passing**.

The remaining 10 records are **genuine gaps in the published rolls** — deleted
electors whose serials appear nowhere in the source PDFs. Verified word by word,
not assumed.

---

## Setup

Requires **Python 3.11+** (developed on 3.14) and **Docker** for PostgreSQL.

```bash
cd "path/to/POC"

python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt     # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # Linux/macOS

# Playwright is only needed to re-run the research scripts, not the pipeline
.venv/Scripts/python.exe -m playwright install chromium

cp .env.example .env          # defaults already match docker-compose.yml
docker compose up -d          # PostgreSQL 17 on host port 5433
```

Optional — only for scanned PDFs (Form 20 2017/2022):

```bash
# Windows: https://github.com/UB-Mannheim/tesseract/wiki  (install the 'hin' data)
# Debian:  sudo apt install tesseract-ocr tesseract-ocr-hin
# then set TESSERACT_CMD in .env if it is not on PATH
```

Without Tesseract everything else still runs; scanned documents are recorded as
blocked rather than silently producing empty rows.

---

## Run

```bash
export PYTHONPATH=src          # Windows PowerShell: $env:PYTHONPATH="src"

python -m app serve                         # prepares ALL data, then dashboard at http://127.0.0.1:8000
```

`serve` is the only command needed. Before starting the dashboard it creates the
schema and runs the whole flow as one pipeline:

| Stage | Work | Runs |
|---|---|---|
| hierarchy | state → 13 districts → 70 constituencies (ECI) | when missing |
| parts, ps-list | current polling stations + names/areas (CEO UK) | when missing |
| rolls | download → text/scan check → extract → validate → duplicates → store; scanned PDFs are recorded as blocked | when missing |
| mapping | official 2003 → 2025 village mapping, looked up by the villages printed on each roll | when missing |
| ps-list-2024 | for constituencies with mapping rows in review: download the scanned Polling Station List 2024 and read its table by OCR (Tesseract + `hin`) into `data/processed/ocr/ps_list_2024_AC{n}.json`. Used only to show a "Possible: Part N" suggestion on review rows — OCR text similarity never makes a row Verified | when missing |
| form20 | booth results + vote columns for each configured year (default 2012) | when missing |
| verify | link duplicates to their original, link results to constituencies, check stored row counts | every start (offline) |
| review | write `data/processed/review/`: `mapping_verification.csv`, `record_review.csv` (no names), `result_review.csv`, `quality_report.json` | every start (offline) |

On a complete database the network stages make no request and the dashboard starts
immediately; a failed stage is reported and retried on the next start.

```bash
python -m app serve --refresh               # re-run every stage (re-check sources)
python -m app serve --skip-pipeline         # start on existing data without checking
python -m app pipeline --dry-run            # show the plan and what is missing, no requests
python -m app pipeline                      # run every stage without starting the dashboard
python -m app quality                       # data-quality summary in the terminal
```

### All commands

```bash
python -m app discover-state      --state Uttarakhand
python -m app discover-districts  --state Uttarakhand
python -m app discover-acs        --state Uttarakhand --district Dehradun
python -m app discover-parts      --ac 19 --limit 20
python -m app inspect-roll        --roll data/raw/roll2003/AC15/P0006.pdf
python -m app extract-roll        --roll data/raw/roll2003/AC15/P0006.pdf --out out.json
python -m app analyze-booth       --part 6 --edition ROLL-2003
python -m app pipeline            --district Dehradun --ac 19 --limit 5
python -m app quality
python -m app serve               --host 127.0.0.1 --port 8000
```

Global flags on `pipeline`: `--dry-run`, `--missing-only`, `--resume/--no-resume`,
`--limit`, `--parts` (default `6,7,8,9,10`), `--all-districts/--district-only`,
`--concurrency`, `--skip-form20`, `--skip-mapping`, `--quiet`.
Flags on `serve`: `--host`, `--port`, `--refresh`, `--skip-pipeline`, `--concurrency`, `--verbose`.

`--resume` (default) reuses already-downloaded PDFs, and every write is an upsert,
so re-running is safe and idempotent.

### Research utility

```bash
python scripts/inspect_eci_api.py            # probe endpoints, save samples
python scripts/research_portals.py --all     # browser observation (needs Playwright)
```

`inspect_eci_api.py` writes response samples to `research/samples/`. Gated
endpoints are probed only far enough to confirm the gate still exists.

`research_portals.py` observes the ECI portal's own network calls and
**re-verifies that every page documented as CAPTCHA-gated still is** — if a gate
appears or disappears, it reports the drift rather than letting the docs rot.

### Tests

```bash
python -m pytest -q      # 65 tests, no network, ~1s
```

Parser tests run against slices of the **real** published PDFs, committed under
`tests/fixtures/`.

---

## Example output

```
Booth / Part: 6   (AC 15 राजपुर, Dehradun, edition ROLL-2003)
Polling station: प्राथमिक स्कूल अस्थल
Roll: Final 2003   extraction: text

Total electors extracted: 342
  Male:           175
  Female:         167
  Other/Unknown:  0

Age distribution:
  18-25      95  ( 27.8%)
  26-40     118  ( 34.5%)
  41-60      93  ( 27.2%)
  61+        36  ( 10.5%)

Serial range: 1-342   gaps: 0
Records carrying an EPIC: 212 (62.0%)

Counts:
  Official count:       342   (basis: highest serial number printed in the roll)
  Extracted:            342
  Difference:            +0   (+0.00%)

Extraction quality:
  Mean confidence:        99.87%
  Rows below 95% conf.:   6
  Rows failing validation:0
  Duplicates flagged:     0

Source: ceo_uk_legacy_roll_2003
  document: data/raw/roll2003/AC15/P0006.pdf  (12 pages)
  url:      https://election.uk.gov.in/search2003uk/api/uklegacydata/pdf?filePath=...
```

---

## Where the data comes from, and why

The **current SIR 2026 roll PDFs are CAPTCHA-gated** on the ECI portal, and the
portal's roll endpoints encrypt their request parameters client-side. Neither is
worked around.

Instead the POC uses CEO Uttarakhand's **published Legacy Roll 2003** PDFs, which
are served by ordinary HTTP GET with no protection mechanism at all — a genuine
official electoral roll with full elector records. The extraction pipeline is
independent of how a PDF was obtained, so a manually downloaded SIR 2026 roll can
be fed straight to `extract-roll`.

Full reasoning: [docs/DATA_SOURCE_RESEARCH.md](docs/DATA_SOURCE_RESEARCH.md).

---

## Two text encodings, both handled

Neither source is plain Unicode, and getting this wrong corrupts every name:

* **Kruti Dev** (PS List 2026, Form 20 2012) — `jktdh; izkFkfed fo|ky;` is
  actually `राजकीय प्राथमिक विद्यालय`.
* **Conjuncts mapped into Latin-Extended** (Roll 2003) — `पुŜष` is `पुरुष`,
  `संƥा` is `संख्या`.

Both converters reorder the short-i vowel sign and the reph, score their own
confidence, and always keep the raw text. Glyphs whose meaning is genuinely
ambiguous are **left unmapped** so they lower the confidence score instead of
being guessed at. Details: [docs/UTTARAKHAND_ROLL_FORMAT.md](docs/UTTARAKHAND_ROLL_FORMAT.md).

---

## Layout

```
config/sources.yaml          16 sources: URL, access type, CAPTCHA status, automate yes/no
docs/                        research, roll format, API findings, privacy, final report
research/samples/            captured API responses
research/network/            Playwright traces + screenshots
src/app/
  config.py  http_client.py  settings; retries, backoff, legacy TLS, politeness
  sources/eci_api/           ECI gateway adapter (public endpoints only)
  sources/ceo_uttarakhand/   SIR parts, Legacy Roll 2003, Form 20
  extraction/pdf/            download, checksum, text-vs-scan detection
  extraction/ocr/            Tesseract Devanagari, degrades honestly when absent
  extraction/parsers/        krutidev, devanagari_fix, roll_2003, ps_list_2026, form20_2012
  database/                  models + idempotent repositories
  services/                  pipeline, validation
  analytics/                 booth statistics, quality report
  browser/                   Playwright research helpers (NOT in the pipeline)
  cli/  web/                 Typer CLI, FastAPI dashboard
scripts/                     endpoint research utilities
tests/                       65 tests + real-PDF fixtures
data/raw/                    downloaded PDFs (git-ignored)
```

One source = one adapter, so adding a state means adding an adapter, not
rewriting the database or the pipeline.

---

## Known limitations

1. **Current-year rolls cannot be downloaded automatically** — CAPTCHA. Manual
   download works with the same parser.
2. **Elector records are from the 2003 roll**, a different delimitation from the
   current 70 ACs. The official `part_mapping` links them; part numbers are never
   assumed stable.
3. **Form 20 candidate names are not attributed** to vote columns — the rotated
   headers cannot be matched reliably, so only labelled totals are stored.
4. **Form 20 2017/2022 need OCR**, and Tesseract was not installed in the
   development environment, so that path is implemented and unit-tested but not
   demonstrated on a real scan.
5. **PS List 2026 cell boundaries** are imprecise (merged Excel cells); locality
   and building are stored joined rather than wrongly split.
6. **~3% of elector rows** carry at least one unmapped conjunct glyph, reflected
   in their confidence score.
7. **Section numbers** are not printed in the 2003 roll format, so
   `section_number` stays NULL.
8. **Scale is untested** — five booths, not seventy constituencies.

## Next steps

See [docs/POC_FINAL_REPORT.md](docs/POC_FINAL_REPORT.md) §13.
