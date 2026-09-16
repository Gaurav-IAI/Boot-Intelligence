# Privacy and compliance

This POC processes personal data about identifiable individuals — names, ages,
family relationships and, where the source publishes it, EPIC numbers. That it
is *published* does not make it unregulated, and India's DPDP Act 2023 applies
to processing it. This document states what the system does, what it refuses to
do, and where the boundaries are drawn in code.

---

## 1. Sources actually used

Every source below is reachable by anonymous HTTP with no protection mechanism.
The full registry, including the ones we refuse to automate, is
[`config/sources.yaml`](../config/sources.yaml).

| Source | What it gives | Personal data? |
|---|---|---|
| ECI gateway `/common/states`, `/common/districts`, `/common/acs` | administrative hierarchy | no |
| CEO UK `/asdlist/SearchAdsEpic/Parts` | polling stations (SIR 2026) | no |
| CEO UK `PSSIR2026/{ac}.pdf` | station names, areas covered | no |
| CEO UK Legacy Roll 2003 PDFs | **elector records** | **yes** |
| CEO UK `village-details` | 2003→2025 part mapping | no |
| CEO UK Form 20 2012 | booth-level vote totals | no |

Only one source carries personal data, and it is a document the Electoral
Registration Officer published for public inspection.

---

## 2. Restricted sources — detected, documented, not automated

| Source | Restriction | What we do |
|---|---|---|
| ECI e-roll PDF download (SIR 2026) | **CAPTCHA** (`captcha-service/getCaptcha/EROLL`) | not automated; a human downloads and the parser is pointed at the file |
| ECI `get-publish-eroll-type` / `get-publish-part-list` | request parameters **encrypted client-side** | observed for documentation only; never reproduced |
| ECI `authn-voter/*` | authentication token | never called |
| CEO UK `SearchAdsEpic/SearchEpic`, `DownloadAsdPdf`, `DownloadBlaMinutes` | **CAPTCHA** | not implemented |
| CEO UK roll portal 2018–2023 | **CAPTCHA** in `__VIEWSTATE` | not implemented |
| CEO UK `/Pdf_Roll/` directory listing | **403 Forbidden** | not circumvented; official index pages used instead |

There is a test that fails if any of these ever appear as a call in the source
adapters: `tests/test_sources.py::TestNoCaptchaEndpointsAreImplemented`.

---

## 3. Automation boundaries — what this system will not do

Stated as prohibitions, because they are enforced by absence of code, not by a
runtime flag:

* **No CAPTCHA solving, submitting or bypassing** — not by OCR, not by a solving
  service, not by replaying a token, not through a headless browser.
* **No anti-bot circumvention.** Playwright was used once, for research, to
  observe the portals' own network calls. It does not spoof fingerprints and is
  not part of the pipeline.
* **No authentication bypass** and no use of leaked or borrowed credentials.
* **No ERONET or other internal endpoints.**
* **No brute-forced voter search.** The legacy portal exposes `/search`,
  `/epic-no` and `/export`, which return individual elector rows and take name,
  age and gender filters. Enumerating booths through them would be exactly the
  pattern the project brief prohibits. They are documented in the research notes
  and **deliberately left unimplemented**; a test asserts the client has no such
  method. The POC reads the **published PDF** instead — the same data, as the
  document the ERO actually issued.
* **No rate-limit circumvention.** Default 3 concurrent requests with a
  ≥0.4 s inter-request delay; `Retry-After` is honoured; only 429 and 5xx are
  retried, with exponential backoff and jitter.

---

## 4. Contact data is structurally separate

The business ambition behind this work involves contacting voters. **Public
electoral rolls do not contain phone numbers**, and no amount of processing turns
a roll into contact data.

The `electors` table therefore has **no** `mobile`, `phone`, `email` or `contact`
column. A separate `contact_records` table exists for consented data only, with
mandatory `source`, `consent_status` and `consent_timestamp` columns. It is
**not populated by the pipeline** — only by test/mock data or by an explicit
consented import.

A test enforces the separation:
`tests/test_database.py::TestContactSeparation`.

Never attempt to join an elector to a phone number by name matching. It is
unreliable, and it converts a published register into a marketing list, which is
a purpose the data subjects did not consent to.

---

## 5. Sensitive fields and minimisation

| Field | Stored? | Why |
|---|---|---|
| name, relative name, relation | yes | the record's identity; the roll's purpose |
| age, gender | yes | booth demographics, the analytical point |
| EPIC | yes, when printed | the official identifier; absent → NULL |
| house number | column exists, always NULL in this edition | not published; never inferred |
| raw OCR/text of the row | yes | auditability (section 6) |
| phone, email, caste, religion, income | **never** | not in the source; would be invention |

Fields absent from a source stay NULL. The parser does not guess: an
unparseable age becomes NULL rather than a plausible number.

---

## 6. Auditability

Every elector row carries: `source`, `source_url`, `source_document`,
`source_page`, `raw_text`, `parser_version`, `extraction_method`,
`extraction_confidence` and `extracted_at`. Every roll carries a SHA-256 of the
downloaded PDF, its byte size, page count and the URL it came from. Every remote
fetch is logged to `source_fetches`.

Any record in the database can therefore be traced to a page of a named official
document and re-checked by a human.

Corrections are auditable too: the conjunct-repair table records *why* each
mapping exists, and unmapped glyphs reduce a row's confidence rather than being
silently guessed.

---

## 7. Data retention and handling

* **Raw PDFs** are kept under `data/raw/` as the evidentiary record. They are
  git-ignored.
* **The database** is local. Nothing is transmitted anywhere.
* **Logs never contain personal data.** The HTTP client logs URLs, status codes,
  byte counts and request ids. Parsers log counts and confidence, not names.
  There is no code path that writes a voter dump to the application log.
* **Secrets** live in `.env`, which is git-ignored; `.env.example` carries only
  placeholders. No credentials are needed for any source the POC uses.
* **Retention** is not enforced by the POC because it holds a 2003 roll for five
  booths. A production system must set a retention period tied to a stated
  purpose, and delete on expiry.

---

## 8. Before this becomes production

These are compliance obligations, not engineering tasks, and they are outside
what a POC can settle:

1. **State the purpose** for processing, and check it against the purpose for
   which the roll was published (electoral transparency and public inspection).
2. **Establish the lawful basis** under DPDP 2023 for processing at scale, and
   record it.
3. **Re-check the terms of use** of each source site before bulk collection; a
   published document being fetchable is not itself permission to build a
   derived national database.
4. **Decide retention and deletion** per dataset.
5. **Access control and encryption at rest** — the POC has neither.
6. **Get legal sign-off before any contact-data work.** The gap between "public
   electoral roll" and "outreach list" is a legal question, not a technical one.
7. **Be deliberate about aggregation risk.** One booth's roll is a public
   document; all 70 ACs joined to results and contact data is a different thing,
   and should be treated as such.
