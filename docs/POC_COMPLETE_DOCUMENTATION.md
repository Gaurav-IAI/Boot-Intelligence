# Uttarakhand Election Data POC — Complete Documentation

**Document version:** 1.0
**Date:** 2026-09-14
**Applies to:** the POC as it exists today in `D:\AI-Centre\Political Application Research\POC`

> **How to read this document.** Sections 1–8 are written for any reader, technical
> or not. Sections 9–25 are for developers and testers. Sections 26–35 are for
> everyone again. Every number, command, route, table and version in this document
> was verified against the running system on 2026-09-14 — nothing is estimated,
> and nothing planned is described as if it exists.

---

## Table of contents

1. [Executive overview](#1-executive-overview)
2. [Business objective](#2-business-objective)
3. [POC scope](#3-poc-scope)
4. [End-to-end system flow](#4-end-to-end-system-flow)
5. [Data source architecture](#5-data-source-architecture)
6. [Abbreviations](#6-abbreviations)
7. [Core election data concepts](#7-core-election-data-concepts)
8. [Historical vs current data](#8-historical-vs-current-data)
9. [Electoral roll extraction flow](#9-electoral-roll-extraction-flow)
10. [Extraction methods](#10-extraction-methods)
11. [Elector record structure](#11-elector-record-structure)
12. [Database architecture](#12-database-architecture)
13. [Data provenance](#13-data-provenance)
14. [Data quality system](#14-data-quality-system)
15. [Part 6 case study](#15-part-6-case-study)
16. [Part 10 case study](#16-part-10-case-study)
17. [Form 20 / election result data](#17-form-20--election-result-data)
18. [Command line interface](#18-command-line-interface)
19. [Dashboard / UI documentation](#19-dashboard--ui-documentation)
20. [UI metric explanation](#20-ui-metric-explanation)
21. [User journey](#21-user-journey)
22. [Technical stack](#22-technical-stack)
23. [Project directory structure](#23-project-directory-structure)
24. [Data storage structure](#24-data-storage-structure)
25. [API / endpoint flow](#25-api--endpoint-flow)
26. [Security and ethical limitations](#26-security-and-ethical-limitations)
27. [Current limitations](#27-current-limitations)
28. [What has been completed](#28-what-has-been-completed)
29. [What has not been completed](#29-what-has-not-been-completed)
30. [How to run the POC](#30-how-to-run-the-poc)
31. [Troubleshooting](#31-troubleshooting)
32. [Testing](#32-testing)
33. [Data quality example table](#33-data-quality-example-table)
34. [Glossary](#34-glossary)
35. [Architecture summary](#35-architecture-summary)

---

## 1. Executive overview

### What is this POC?

This is a **proof of concept** — a working but deliberately small system built to
answer one question:

> *Can we reliably build a booth-by-booth picture of an Indian state's electorate,
> using only official, publicly available election data?*

The state chosen is **Uttarakhand**. The POC takes official government sources,
works its way down the election hierarchy, downloads the published electoral-roll
documents, reads the individual voter records out of them, checks the results, and
stores everything in a database that can be browsed through a small web dashboard.

It is **not** a product. It processes five polling booths, not seventy
constituencies. Its purpose is to prove the pipeline works end to end and to
surface the problems that would otherwise only appear at scale.

### What the POC actually does today

Each capability below is implemented and was verified for this document:

| Capability | Implemented | Evidence |
|---|---|---|
| Discover the state and its official code | Yes | `Uttarakhand → S28`, looked up at runtime |
| Discover districts | Yes | 13 districts stored |
| Discover Assembly Constituencies | Yes | 10 current ACs stored (Dehradun district) |
| Discover polling stations / parts | Yes | 239 polling stations stored |
| Acquire electoral-roll documents | Yes | 5 official PDFs downloaded, 200 pages |
| Extract individual elector records | Yes | 5,544 records |
| Validate extracted records | Yes | 62 records flagged, none deleted |
| Detect duplicates | Yes | 52 flagged across two duplicate types |
| Score extraction confidence | Yes | mean 99.65% |
| Calculate booth-level statistics | Yes | `analyze-booth` + dashboard |
| Maintain source provenance | Yes | zero missing provenance fields |
| Ingest historical election results | Yes | 152 booth results (Form 20, 2012) |
| Expose everything through a dashboard | Yes | 4 web routes |

### What it deliberately does not do

* It does **not** download current-year (2026) electoral rolls — those are
  protected by a CAPTCHA on the official portal, and the POC does not bypass it.
* It does **not** collect, infer or store voter phone numbers.
* It does **not** run OCR on real scanned documents — the OCR code exists and is
  unit-tested, but the OCR engine is not installed in this environment.

---

## 2. Business objective

### The problem

Indian elections are won and lost at the **polling booth** — a unit of roughly
500–1,500 voters. Almost all publicly discussed election data, however, is
published at constituency level or above. The detail that matters operationally —
who is registered where, how the electorate is shaped booth by booth, how a booth
voted historically — exists in official documents but is not available as data.

It is published as **PDF documents**, one per booth, in the tens of thousands, in
Hindi, in formats that change between years.

### Why booth-level data matters

* A constituency-level number is an average over 200+ booths that can differ
  enormously from each other.
* Planning, resource allocation and verification all happen at booth level.
* Comparing an electoral roll against past results, booth by booth, is how
  irregularities become visible.

### Why this is technically difficult

The POC exists because these obstacles are real, and each one was hit during
development:

1. **The documents are PDFs, not data.** Voter records must be recovered from page
   layout.
2. **The Hindi text is not Unicode.** Two *different* legacy encodings appear in
   the documents, and naive extraction produces silent gibberish that still looks
   like text.
3. **Formats change between years.** Three consecutive Vidhan Sabha elections
   published Form 20 in three materially different ways.
4. **The published documents contain real errors** — duplicate registrations,
   repeated serial numbers, a serial printed 945 places beyond the end of its
   booth. A pipeline that trusts the documents blindly produces wrong statistics.
5. **Identifiers are not stable over time.** Constituency and booth numbers are
   reassigned between revisions.

### Why official sources matter

Election data has legal and political weight. A figure that cannot be traced back
to a specific page of a specific government document is not usable for anything
serious. This is why the POC treats **provenance** as a first-class requirement:
every stored record names the document, page and URL it came from.

### Why historical and current data must be separated

This is the single most important design constraint in the project.

Constituency numbers and booth numbers are **reassigned** when boundaries are
redrawn. In this POC's own data, **AC 15 means "Rajpur" in the 2003 records and
"Chakrata" today** — two different places, same number. Merging data on the number
alone would silently combine unrelated constituencies.

### Why data quality and provenance matter

The POC reports a shortfall of 10 records out of 5,554. Investigation showed those
serial numbers do not appear anywhere in the source documents — they are **deleted
voters**, not extraction failures. Without per-record provenance that distinction
would be impossible to make, and the honest conclusion ("our extraction is
complete; the source has gaps") would have been indistinguishable from the
alarming one ("our extraction is losing records").

---

## 3. POC scope

### 3.1 Geography

**Uttarakhand only.**

The state is **not hard-coded**. At run time the POC calls the Election Commission
of India's public state list and resolves the name to its official code:

```
Uttarakhand  →  state code S28  (ECI internal stateId 27, type ST)
```

The resolved code is stored in the `states` table. A test
(`tests/test_sources.py`) fails if a literal `"S28"` ever appears as a value the
code depends on.

### 3.2 Current data processed

Current (2026 revision) polling-station discovery is implemented and run for one
constituency:

* District: **Dehradun** (district code `S2813`)
* Assembly Constituency: **AC 19 — Raipur** (current delimitation)
* **234 polling stations** discovered and stored, each enriched with the station
  name and the areas it covers from the official Polling Station List 2026 PDF

**No current-year electoral roll (voter records) has been ingested** — see
[section 27](#27-current-limitations).

### 3.3 Historical electoral roll data processed

Voter records come from the **2003 electoral roll**, which the Chief Electoral
Officer of Uttarakhand publishes as freely downloadable PDFs:

* Assembly Constituency: **AC 15 — Rajpur** (2003 delimitation)
* Parts processed: **6, 7, 8, 9, 10** (five booths)
* **5,544 elector records** extracted from **200 pages**

### 3.4 Historical election results processed

* Election: **Vidhan Sabha (State Assembly) 2012**
* Constituency: **AC 19 — Raipur**
* **152 booth-level result rows** stored

### 3.5 Current sample size — verified counts

Read directly from the PostgreSQL database on 2026-09-14:

| Table | Rows |
|---|---:|
| `states` | 1 |
| `districts` | 13 |
| `assembly_constituencies` | 11 |
| `polling_stations` | 239 |
| `electoral_rolls` | 5 |
| `electors` | **5,544** |
| `elections` | 1 |
| `election_results` | 152 |
| `part_mapping` | 17 |
| `source_fetches` | 22 |
| `contact_records` | 0 |

The 11 constituencies are **10 current** (Dehradun district) + **1 historical**
(AC 15 Rajpur, 2003). The 239 polling stations are **234 current** (SIR-2026
edition) + **5 historical** (ROLL-2003 edition).

### 3.6 Stages distinguished

The POC keeps these ideas separate, and so does this document:

| Stage | Meaning | Current figure |
|---|---|---|
| **Discovered** | Known to exist from an official listing | 13 districts, 70 ACs statewide available, 234 current stations |
| **Downloaded** | Document fetched to local disk with a checksum | 5 roll PDFs, 1 station-list PDF, 1 Form 20 PDF |
| **Extracted** | Records read out of a document | 5,544 electors, 152 booth results |
| **Validated** | Checked against rules | all 5,544 checked; 62 flagged |
| **Stored** | Written to PostgreSQL | all of the above |
| **Analyzed** | Booth statistics computed | 5 booths |

---

## 4. End-to-end system flow

```text
                     OFFICIAL ELECTION SOURCES
                                │
                                ↓
                        STATE DISCOVERY
                 (ECI public API → "Uttarakhand" = S28)
                                │
                                ↓
                       DISTRICT DISCOVERY
                        (ECI public API → 13)
                                │
                                ↓
              ASSEMBLY CONSTITUENCY DISCOVERY
                     (ECI public API → 10 in Dehradun)
                                │
                                ↓
               POLLING STATION / PART DISCOVERY
          (CEO Uttarakhand JSON → 234;  PDF → names + areas)
                                │
                                ↓
                  ELECTORAL ROLL ACQUISITION
           (CEO Uttarakhand Legacy Roll 2003 PDF service)
                                │
                                ↓
                   PDF INSPECTION (text or scan?)
                     ┌──────────┴──────────┐
                     ↓                     ↓
             TEXT EXTRACTION            OCR PATH
            (implemented, used)   (implemented, NOT demonstrated)
                     └──────────┬──────────┘
                                ↓
                    ELECTOR RECORD EXTRACTION
              (geometric column parsing + text repair)
                                │
                                ↓
                           VALIDATION
                                │
                                ↓
                      DUPLICATE DETECTION
                                │
                                ↓
                      CONFIDENCE SCORING
                                │
                                ↓
                     POSTGRESQL DATABASE
                                │
                                ↓
                     BOOTH-LEVEL ANALYTICS
                                │
                                ↓
                           DASHBOARD
```

### Step by step

**1. State discovery.** The POC requests the ECI's public list of states and
searches it by name. This returns Uttarakhand's official code `S28`. Storing the
code rather than assuming it means the same code is used everywhere downstream.

**2. District discovery.** Using the state code, the POC requests the district
list — 13 districts, each with an official code such as `S2813` for Dehradun.

**3. Assembly Constituency discovery.** Using a district code, the POC requests the
constituencies in that district — 10 for Dehradun, each with its official number,
name in English and Hindi, reservation category and Parliamentary Constituency
number.

**4. Polling station / part discovery.** Constituency-level data comes from the
Election Commission; booth-level data does not (see [section 25](#25-api--endpoint-flow)).
The POC uses the Chief Electoral Officer of Uttarakhand's public JSON endpoint,
which returns all 234 parts of AC 19 with their numbers and names. It then
downloads the official *Polling Station List 2026* PDF for the same constituency
and adds the station building name and the areas each station serves.

**5. Electoral roll acquisition.** The current roll is CAPTCHA-protected. The POC
instead uses the CEO's published **2003 roll**, served as one PDF per booth by
ordinary web request with no protection. Each PDF is saved to disk with a SHA-256
checksum so it can be proved unchanged later.

**6. PDF inspection.** Before extracting, the POC checks whether the PDF contains
real text or is a scanned image, by sampling pages and counting characters. This
decides which path is taken. All five roll PDFs are text-based.

**7. Elector record extraction.** Covered in detail in [section 9](#9-electoral-roll-extraction-flow).

**8. Validation.** Each record is checked against rules for age, gender, EPIC
format, and consistency with its parent booth. Failures are **flagged, never
deleted**.

**9. Duplicate detection.** Two kinds are detected — the same serial number twice,
and the same person (name + relative + age) under two serials.

**10. Confidence scoring.** Every record gets a score reflecting how much of its
text was successfully decoded.

**11. Database.** Everything is written to PostgreSQL. Re-running does not create
duplicates.

**12. Analytics.** Booth statistics are computed on demand from stored records.

**13. Dashboard.** A small web interface lets a user browse the hierarchy and read
the statistics.

---

## 5. Data source architecture

### 5.1 Sources actually used by the pipeline

| Source | Purpose | Data obtained | Method | Status |
|---|---|---|---|---|
| ECI Gateway — state list | Resolve state to official code | 36 states with codes | Public JSON API | **Used** |
| ECI Gateway — district list | District hierarchy | 13 districts for S28 | Public JSON API | **Used** |
| ECI Gateway — AC list | Constituency hierarchy | 10 ACs for Dehradun | Public JSON API | **Used** |
| CEO Uttarakhand — SIR 2026 parts | Current polling stations | 234 parts for AC 19 | Public JSON API | **Used** |
| CEO Uttarakhand — Polling Station List 2026 | Station names, areas | 234 stations enriched | Direct HTTP PDF | **Used** |
| CEO Uttarakhand — Legacy Roll 2003 API | 2003 hierarchy + mapping | districts, ACs, parts, village mapping | Public JSON API | **Used** |
| CEO Uttarakhand — Legacy Roll 2003 PDFs | **Voter records** | 5 booth rolls, 5,544 electors | Direct HTTP PDF | **Used** |
| CEO Uttarakhand — Form 20, Vidhan Sabha 2012 | Historical booth results | 152 booth results | HTML index + direct HTTP PDF | **Used** |

### 5.2 Sources researched but deliberately not used

| Source | Type | Why not used |
|---|---|---|
| ECI e-roll PDF download (SIR Draft 2026) | PDF via portal | **CAPTCHA-gated.** Not bypassed. |
| ECI `printing-publish/*` endpoints | JSON API | Request parameters are **encrypted client-side** by the portal; reproducing that would mean defeating a protection mechanism |
| ECI `authn-voter/*` | JSON API | Requires authentication; out of scope |
| CEO UK `SearchAdsEpic/SearchEpic`, `DownloadAsdPdf`, `DownloadBlaMinutes` | JSON / PDF | **CAPTCHA-gated** |
| CEO UK electoral roll portal, 2018–2023 editions | HTML (ASP.NET) | **CAPTCHA-gated** |
| CEO UK Legacy 2003 `/search`, `/epic-no`, `/export` | JSON API | Open, but returns individual voter records by name/age/gender filters. Using it to enumerate booths would be a brute-force voter search. **Intentionally not implemented.** |
| Form 20, Vidhan Sabha 2017 | PDF | Contains an embedded OCR text layer that is corrupted and unusable |
| Form 20, Vidhan Sabha 2022 | PDF (scanned images) | Requires OCR; the OCR engine is not installed here |
| Directory listings (`/Pdf_Roll/` etc.) | HTML | Return **403 Forbidden**; not circumvented |

### 5.3 Source registry file

`config/sources.yaml` documents all 16 sources with URL, access type, CAPTCHA
status, verification date and an explicit `automate: true/false` flag.

> **Note (accuracy):** this file is a **documentation registry**. It is referenced
> by a settings path (`settings.sources_config`) but is **not parsed at run time** —
> no code imports a YAML library. Source URLs live in the adapter modules.

---

## 6. Abbreviations

Every abbreviation below appears in this project's code, database, dashboard, CLI
output, configuration or documents. Expansions are the meaning **as used in this
project**.

| Abbreviation | Full form | Meaning in this POC |
|---|---|---|
| **AC** | Assembly Constituency | A state-legislature seat. Stored in `assembly_constituencies`; identified by number + name + delimitation. |
| **ACs (2003)** | — | Dashboard label: constituencies belonging to the 2003 boundary set |
| **ACs (current)** | — | Dashboard label: constituencies in today's boundary set |
| **API** | Application Programming Interface | Machine-readable endpoints used to fetch hierarchy data |
| **ASD** | Absent, Shifted and Dead (electors) | A CEO Uttarakhand list of voters removed from the draft roll. The POC uses one **unprotected** endpoint on that page (the part list) and does **not** download the ASD list itself, which is CAPTCHA-gated. |
| **BLO** | Booth Level Officer | Official responsible for a booth's roll. Referenced in source research only; no BLO data is ingested. |
| **CEO** | Chief Electoral Officer | The state-level election authority. "CEO Uttarakhand" is the POC's main data source. |
| **CLI** | Command Line Interface | The `python -m app ...` commands |
| **DB** | Database | PostgreSQL instance holding all stored data |
| **ECI** | Election Commission of India | National election authority; source of the state/district/AC hierarchy |
| **EPIC** | Electors Photo Identity Card | The voter ID number. Optional in the source; stored when printed. |
| **ER / ERO** | Electoral Registration Officer | Officer who publishes a roll; appears in the roll document footer |
| **ERONET** | Electoral Roll Management Network | ECI's **internal** roll-management system. Referenced in the project only to state that it is **never accessed**. |
| **FK** | Foreign Key | Database link between tables |
| **HTTP / HTTPS** | Hypertext Transfer Protocol (Secure) | How documents and API responses are fetched |
| **JSON** | JavaScript Object Notation | Format of the API responses |
| **OCR** | Optical Character Recognition | Reading text from scanned images. Implemented, not demonstrated on a real scan. |
| **ORM** | Object Relational Mapper | SQLAlchemy, which maps Python classes to database tables |
| **PC** | Parliamentary Constituency | A national-parliament seat. Stored as `pc_number` on each AC; shown in the dashboard as "PC". |
| **PDF** | Portable Document Format | The format all source documents are published in |
| **POC** | Proof of Concept | This project |
| **PS** | Polling Station | Used in source names such as "PS List 2026" |
| **SHA-256** | Secure Hash Algorithm, 256-bit | Checksum proving a downloaded PDF is unchanged |
| **SIR** | Special Intensive Revision | A full door-to-door revision of the electoral roll. The current roll edition is labelled `SIR-2026`; the 2003 roll's own header describes it as a special intensive revision (`विशेष गहन पुनरीक्षण-2003`). |
| **SQL** | Structured Query Language | Language used to query the database |
| **ST / SC / GEN** | Scheduled Tribe / Scheduled Caste / General | Constituency reservation category, shown in the dashboard as "Category" |
| **TLS** | Transport Layer Security | Encryption for HTTPS; one source requires a legacy TLS setting |
| **UI** | User Interface | The web dashboard |
| **URL** | Uniform Resource Locator | Web address recorded for every fetched document |
| **UT** | Union Territory | State type returned by the ECI API (Uttarakhand is `ST`, a State) |
| **VS** | Vidhan Sabha | State Legislative Assembly; `VIDHAN_SABHA` is the stored election type |

### Project-specific labels

| Label | Where seen | Meaning |
|---|---|---|
| `S28` | database, API | Uttarakhand's official ECI state code |
| `S2813` | database, API | Dehradun's official district code |
| `ROLL-2003` | `polling_stations.edition` | The 2003 roll edition |
| `SIR-2026` | `polling_stations.edition` | The current (2026 Special Intensive Revision) edition |
| `current` / `2003` | `assembly_constituencies.delimitation` | Which boundary set an AC belongs to |
| `Form 20` | source documents | The statutory Final Result Sheet giving booth-level votes |
| `Part` | everywhere | The official unit of an electoral roll — one booth's list |

---

## 7. Core election data concepts

### State

The top level. India's states and union territories each have an ECI code.
This POC handles one: **Uttarakhand, code `S28`**.

### District

An administrative subdivision of a state. Uttarakhand has **13**. Each has an
official code (`S2801`–`S2813`). Districts group constituencies geographically but
are not themselves electoral units.

### Assembly Constituency (AC)

The seat a voter elects one Member of the Legislative Assembly for. Uttarakhand
has **70**. Each has:

* a **number** (1–70) — official but **reassigned when boundaries change**
* a **name** in English and Hindi
* a **category** — GEN, SC or ST
* a **Parliamentary Constituency (PC) number** it sits inside

### Polling station

The physical place a voter votes — typically a school, panchayat building or
community hall. The POC stores its name and the villages, streets or localities it
serves.

### Part

**A Part is the unit of the electoral roll**: the numbered list of voters attached
to one polling station. Part 6 of AC 15 is "the voter list for polling station 6".

In this POC, the source data uses **Part number and polling-station number
interchangeably** — the 2003 roll header prints both and they carry the same value
(`भाग संख्या -6` and `मतदेय स्थल की संख्या 6`). The database keeps both columns
(`part_number`, `polling_station_number`) rather than assuming they will always
agree, because that equivalence is a property of this source, not a guarantee.

### "Booth"

**"Booth" is used in this project as an informal synonym for a Part / polling
station**, in phrases like "booth-level statistics" and the `analyze-booth`
command. It is **not a separate database entity** — there is no `booths` table. If
you see "booth", read "one Part and its polling station".

### The hierarchy

```text
State                  (Uttarakhand, S28)
  └── District         (Dehradun, S2813)
        └── Assembly Constituency   (AC 19 Raipur — current
              │                      AC 15 Rajpur — 2003)
              └── Polling Station / Part   (Part 6, "प्राथमिक स्कूल अस्थल")
                    └── Electoral Roll     (Final roll, 2003, Hindi)
                          └── Elector records  (342 individual voters)
```

---

## 8. Historical vs current data

This section describes the most important correctness rule in the project.

### The problem in one example

The database contains two constituencies numbered **15**:

| Database id | AC number | Name | Delimitation | Source |
|---|---|---|---|---|
| 11 | **15** | राजपुर (Rajpur) | `2003` | CEO UK Legacy Roll 2003 |
| 1 | **15** | Chakrata | `current` | ECI Gateway API |

**These are different constituencies in different places.** "AC 15" means Rajpur
in the 2003 records and Chakrata today. Any system that joins election data on the
constituency number alone would silently merge two unrelated seats.

The same applies at booth level. Comparing this POC's 2026 polling stations against
its 2012 Form 20 results for AC 19, only **4 of 152** part numbers refer to the
same place. For example:

| Part number | Polling station in 2026 | Polling station in 2012 |
|---|---|---|
| 1 | राजकीय प्राथमिक विद्यालय — अस्थल | रा0प्रा0 विद्यालय अस्थल *(same)* |
| 5 | नरेन्द्र रोज लीन पब्लिक स्कूल — सौंधोवाली | रा0प्रा0 विद्यालय डाडा खुदानेवाला *(different)* |
| 6 | नागल राजकीय प्राथमिक विद्यालय — तरला नागल | पंचायतघर डांडा लखौण्ड *(different)* |

### How the database prevents the mistake

Three columns carry the time/edition context, and each is part of a uniqueness rule:

| Table | Context column | Values present | Uniqueness rule |
|---|---|---|---|
| `assembly_constituencies` | `delimitation` | `current`, `2003` | UNIQUE (`district_id`, `ac_number`, `delimitation`) |
| `polling_stations` | `edition` | `SIR-2026`, `ROLL-2003` | UNIQUE (`ac_id`, `part_number`, `edition`) |
| `electoral_rolls` | `roll_year`, `roll_type` | 2003, `Final` | UNIQUE (`polling_station_id`, `roll_year`, `roll_type`, `language`) |

Because `delimitation` is part of the constituency key, AC 15 Rajpur (2003) and AC
15 Chakrata (current) are separate rows and cannot collide. Because `edition` is
part of the polling-station key, Part 6 of 2003 and Part 6 of 2026 are separate
rows.

### How the two are linked when they must be

The POC does **not** guess. It uses an **official mapping** published by CEO
Uttarakhand, stored in `part_mapping`, at village level:

```
2003: AC 15 Rajpur, Part 6, village "1 रैनी वाला"
   →  2025: AC 19 Raipur, Part 74  (राजकीय प्राथमिक विद्यालय खैरी मानसिंह)

2003: AC 15 Rajpur, Part 6, village "2 बझैत"
   →  2025: AC 19 Raipur, Part 1   (राजकीय प्राथमिक विद्यालय अस्थल)
```

Note that **one old part maps to several new parts** — Part 6 split across Parts 1
and 74. Every mapping row records the method
(`official_ceo_uk_village_mapping`) and a confidence (0.95). 17 such rows exist.

> **Known defect.** The dashboard's station page currently links Form 20 results to
> a polling station by part number alone, without going through this mapping. This
> is documented as finding F-1 in `docs/PHASE1_DATA_INTEGRITY_AUDIT.md` and is
> scheduled for fix before further ingestion. See [section 27](#27-current-limitations).

---

## 9. Electoral roll extraction flow

```text
PDF
 ↓  Document acquisition
 ↓  PDF inspection
 ↓  Text extraction
 ↓  Record detection
 ↓  Field parsing
 ↓  Normalization
 ↓  Validation
 ↓  Duplicate detection
 ↓  Confidence calculation
 ↓  Database insertion
```

### 9.1 Document acquisition

* **Input:** AC number, AC name in Hindi, part number
* **Processing:** builds the official file path, requests it over HTTPS, verifies
  the response starts with the PDF magic bytes `%PDF`, writes it to
  `data/raw/roll2003/AC15/P00NN.pdf`, computes a SHA-256 checksum
* **Output:** a stored PDF plus page count, byte size and checksum
* **Failure conditions:** non-PDF response (an HTML error page is rejected rather
  than saved), network failure after 4 retries with exponential backoff
* **Provenance:** URL, local path, checksum, byte size and page count are written
  to `electoral_rolls`; the fetch is logged in `source_fetches`
* **Resume:** if the file already exists, it is reused rather than re-downloaded

### 9.2 PDF inspection

* **Input:** the PDF file
* **Processing:** extracts text from up to 5 pages and measures characters per page
* **Output:** `(page_count, is_text_pdf, characters_sampled)`; ≥100 characters per
  page means text-based
* **Failure conditions:** a scanned PDF yields 0 characters and is routed to OCR
* **Provenance:** `electoral_rolls.is_text_pdf`, `page_count`

### 9.3 Text extraction and record detection

The parser is **geometric, not line-based**. Naive line reading mangles rows whose
name wraps onto a second line.

* **Input:** the PDF page
* **Processing:**
  1. Find the page's table boundaries. The top edge is 12 points below the printed
     `(1) … (8)` column-number row — which sits ~74 points higher on continuation
     pages than on page 1. The bottom edge is just above the footnote, identified
     by a word starting `कॉलम` positioned **left of** the serial column.
  2. Find **row anchors**: tokens in the serial-number column that begin with
     digits. (A plain "is this a number?" test fails, because the embedded font
     emits stray combining marks — `62̻` instead of `62`.)
  3. Assign every other word on the page to the nearest anchor at or below it, and
     to a column based on its horizontal position.
* **Output:** one dictionary of raw column text per record
* **Failure conditions:** a page with no anchors yields no rows and the page is
  counted as having no records

### 9.4 Field parsing

Eight fixed columns, with horizontal positions measured from real pages:

| Column | Field | x-range (points) |
|---|---|---|
| 1 | serial number | 40–88 |
| 2 | house number | 88–125 |
| 3 | elector name | 125–226 |
| 4 | relation type | 226–272 |
| 5 | relative name | 272–356 |
| 6 | gender | 356–396 |
| 7 | age | 396–428 |
| 8 | EPIC number | 428–560 |

The page header is parsed separately for roll year, qualifying date, part number,
AC number and name, state code, polling-station number and name, the areas covered
and the revision type.

### 9.5 Normalization

**Text repair.** The 2003 roll PDFs embed a font whose character map sends Hindi
conjunct letters to Latin-Extended character codes. Extraction therefore produces
*almost* correct Unicode:

| Extracted | Correct | Meaning |
|---|---|---|
| `पुŜष` | पुरुष | male |
| `मिहला` | महिला | female |
| `संƥा` | संख्या | number |
| `िनवाŊचक` | निर्वाचक | elector |
| `कृˁकुमार` | कृष्णकुमार | a name |

Roughly 100 such character mappings are applied, followed by two reordering passes
(the short-*i* vowel sign moves after its consonant; the *reph* mark moves before
it). **Digits and EPIC numbers are plain ASCII and are unaffected**, so all
numeric data is exact.

Glyphs whose intended letter is genuinely ambiguous are **left unmapped on
purpose** — they lower the record's confidence score instead of being guessed at.

**Value normalization.**

| Field | Rule |
|---|---|
| gender | `पुरुष`→`M`, `महिला`→`F`, `अन्य`→`O`, anything else→`UNKNOWN` |
| relation | `पिता`→`FATHER`, `माता`→`MOTHER`, `पति`→`HUSBAND`, other→`OTHER` |
| age | kept only if 18–120; otherwise stored as empty |
| EPIC | literal `NULL` text and blanks become empty |

### 9.6 Validation, duplicates and confidence

Covered in [section 14](#14-data-quality-system).

### 9.7 Database insertion

* **Input:** parsed and validated records
* **Processing:** records for a roll are replaced as a set, so re-running produces
  the same result rather than duplicating rows
* **Output:** rows in `electors`, summary counts on `electoral_rolls`
* **Provenance:** each row carries source, URL, document, page, raw text, parser
  version, extraction method, confidence and timestamp

---

## 10. Extraction methods

### 10.1 Text extraction — implemented and used

All data currently in the database was obtained this way. PyMuPDF provides each
word with its position on the page; the parser reconstructs the table from those
positions.

**Status: fully implemented, used for all 5,544 records and all 152 booth results.**

### 10.2 OCR — implemented, unit-tested, NOT demonstrated on a real scan

The OCR module (`src/app/extraction/ocr/engine.py`) is written and covered by
tests. It:

* renders pages at 300 dpi
* runs Tesseract configured for `hin+eng` (Hindi is required — the ECI portal
  itself reports Uttarakhand rolls are published in Hindi only)
* returns a mean confidence per page

**It has not been run on a real scanned document**, because the Tesseract engine is
not installed in this environment. Verified live:

```
OCR available : False
Reason        : tesseract is not installed or not on PATH. Install it and the
                'hin' language data, or set TESSERACT_CMD in .env.
```

When the engine is missing the pipeline **records the document as blocked** rather
than writing empty records. `python -m app inspect-roll --roll <file>` reports the
engine status.

> The project's own report notes that Tesseract is the cheapest option but not the
> most accurate for Devanagari, and that PaddleOCR or EasyOCR should be
> benchmarked. **Neither is installed or implemented.**

### 10.3 Source preference hierarchy

This hierarchy is stated in the project's research documentation and is reflected
in what the code actually does:

```text
1. Official API              ← used for state, district, AC, parts
2. Direct HTTP request       ← used for all PDFs
3. Official downloadable doc ← the roll PDFs, PS list, Form 20
4. HTML parsing              ← Form 20 index pages
5. Playwright (browser)      ← RESEARCH ONLY, not in the pipeline
6. OCR                       ← implemented, not demonstrated
```

**Playwright is not part of the extraction pipeline.** It lives in
`src/app/browser/` and is used only by `scripts/research_portals.py` to observe the
official portals' own network behaviour and to re-verify which pages are
CAPTCHA-protected.

---

## 11. Elector record structure

Fields verified from the live `electors` table.

| Field | Description | Example | Kind |
|---|---|---|---|
| `id` | Database identifier | `11088` | System |
| `polling_station_id` | Link to the polling station | `239` | System |
| `electoral_roll_id` | Link to the roll document | `5` | System |
| `part_number` | Part this record belongs to | `10` | Source-derived |
| `section_number` | Section within the part | *(always empty — not printed in this roll format)* | Source-derived |
| `serial_number` | Voter's serial in the roll | `2220` | Source-derived |
| `elector_name` | Voter name, repaired | `अनीता` | Normalized |
| `elector_name_raw` | Voter name exactly as extracted | `अनीता` | Source-derived |
| `relative_name` | Father/mother/husband name, repaired | `राजेन्द्र` | Normalized |
| `relative_name_raw` | Same, exactly as extracted | `राजेȾ` | Source-derived |
| `relative_type` | Relationship | `FATHER`, `MOTHER`, `HUSBAND`, `OTHER` | Normalized |
| `age` | Age at the qualifying date | `35` | Normalized |
| `gender` | Gender | `M`, `F`, `O`, `UNKNOWN` | Normalized |
| `epic_number` | Voter ID number | `MYC0239293` | Source-derived |
| `house_number` | House number | *(always empty in this roll edition)* | Source-derived |
| `source` | Which source system | `ceo_uk_legacy_roll_2003` | Provenance |
| `source_url` | Exact URL fetched | `https://election.uk.gov.in/...` | Provenance |
| `source_document` | File name | `P0010.pdf` | Provenance |
| `source_page` | Page within that document | `45` | Provenance |
| `raw_text` | All raw column text for the row | `serial=2220 \| name=अनीता \| ...` | Provenance |
| `parser_version` | Parser that produced it | `roll_2003_geometric/1.0.0` | Provenance |
| `extraction_method` | How it was read | `text` | Provenance |
| `extraction_confidence` | 0.0–1.0 decode confidence | `1.0` | Calculated |
| `extracted_at` | When extraction ran | timestamp | Provenance |
| `is_valid` | Passed all validation rules | `false` | Calculated |
| `validation_notes` | Why it failed, in words | `same name+relative+age as serial 220` | Calculated |
| `duplicate_of_id` | Link to the record duplicated | *(never populated — see note)* | Calculated |
| `duplicate_kind` | Type of duplicate | `same_name_relative_age` | Calculated |
| `created_at` / `updated_at` | Row timestamps | timestamp | System |

A database **check constraint** enforces that `gender` is one of `M`, `F`, `O`,
`UNKNOWN`. An index on (`electoral_roll_id`, `serial_number`) supports lookups.

> **Accuracy notes.**
> * `section_number` and `house_number` are **always empty** in the current data.
>   The columns exist because the roll format defines them; the 2003 edition does
>   not print them. They are left empty rather than inferred.
> * `duplicate_of_id` is **never populated** (0 of 52 flagged records). The link to
>   the duplicated record exists only as text inside `validation_notes`. This is
>   recorded as finding F-2 in the Phase-1 audit.
> * **There is deliberately no phone, mobile, email or contact column.** See
>   [section 26](#26-security-and-ethical-limitations).

---

## 12. Database architecture

### 12.1 Conceptual model

```text
State
  │
  └── District
        │
        └── Assembly Constituency        (+ delimitation: current / 2003)
              │
              └── Polling Station / Part (+ edition: SIR-2026 / ROLL-2003)
                    │
                    ├── Electoral Roll   (+ roll_year, roll_type, language)
                    │     │
                    │     └── Elector
                    │
                    └── Contact Record   (consent-gated, currently empty)

Election ──── Election Result            (booth-level votes, Form 20)

Part Mapping                              (2003 part ↔ 2025 part, official)

Source Fetch                              (audit log of every download)
```

### 12.2 Tables

**11 tables**, verified from the live PostgreSQL schema.

| Table | Rows | Purpose |
|---|---:|---|
| `states` | 1 | State and its official code |
| `districts` | 13 | Districts within a state |
| `assembly_constituencies` | 11 | Constituencies, per delimitation |
| `polling_stations` | 239 | Parts / polling stations, per edition |
| `electoral_rolls` | 5 | One published roll document per station |
| `electors` | 5,544 | Individual voter records |
| `elections` | 1 | An election event |
| `election_results` | 152 | Booth-level results from Form 20 |
| `part_mapping` | 17 | Official old-part → new-part mapping |
| `source_fetches` | 22 | Audit log of remote fetches |
| `contact_records` | 0 | Consent-gated contact data — intentionally empty |

### 12.3 Keys and relationships

| Table | Primary key | Foreign keys | Uniqueness rule |
|---|---|---|---|
| `states` | `id` | — | `state_code` |
| `districts` | `id` | `state_id` → `states` | (`state_id`, `district_code`) |
| `assembly_constituencies` | `id` | `district_id` → `districts` | (`district_id`, `ac_number`, `delimitation`) |
| `polling_stations` | `id` | `ac_id` → `assembly_constituencies` | (`ac_id`, `part_number`, `edition`) |
| `electoral_rolls` | `id` | `polling_station_id` → `polling_stations` | (`polling_station_id`, `roll_year`, `roll_type`, `language`) |
| `electors` | `id` | `electoral_roll_id`, `polling_station_id`, `duplicate_of_id` (self) | — |
| `elections` | `id` | — | (`election_year`, `election_type`, `state`) |
| `election_results` | `id` | `election_id` → `elections`, `ac_id` → `assembly_constituencies` | — |
| `part_mapping` | `id` | — | — |
| `source_fetches` | `id` | — | — |
| `contact_records` | `id` | `booth_id` → `polling_stations` | — |

### 12.4 Indexes

Besides primary keys and the uniqueness rules above:

| Index | Table | Columns |
|---|---|---|
| `ix_electors_roll_serial` | `electors` | (`electoral_roll_id`, `serial_number`) |
| `ix_results_election_ac_part` | `election_results` | (`election_id`, `ac_id`, `part_number`) |
| `ix_partmap_lookup` | `part_mapping` | (`from_edition`, `from_ac_number`, `from_part_number`) |

### 12.5 Key columns worth knowing

**`electoral_rolls`** — the document-level record:
`roll_year`, `roll_type`, `language`, `qualifying_date`, `pdf_url`,
`local_file_path`, `download_status`, `extraction_status`, `extraction_method`,
`checksum_sha256`, `file_size_bytes`, `page_count`, `is_text_pdf`,
`official_elector_count`, `official_count_basis`, `extracted_elector_count`,
`parser_version`.

**`election_results`** — one row per booth per election:
`ac_number`, `part_number`, `polling_station_name`, `candidate_name`, `party`,
`votes`, `nota_votes`, `total_valid_votes`, `rejected_votes`, `tendered_votes`,
`total_votes`, plus provenance.

> **Accuracy note.** `election_results.ac_id` exists as a foreign key but is
> **empty for all 152 rows** — results are linked to a constituency only by the
> bare `ac_number`. Recorded as finding F-5 in the Phase-1 audit.

**`part_mapping`** — never assumes stability:
`from_edition`, `from_ac_number`, `from_part_number`, `to_edition`,
`to_ac_number`, `to_part_number`, `area_name`, `mapping_method`, `confidence`.

---

## 13. Data provenance

### What provenance means here

**Provenance is the ability to point at any single row in the database and say
exactly which page of which official document it came from, when it was read, by
which version of the parser, and how confident that reading was.**

### What is recorded

| Level | Fields |
|---|---|
| Elector | `source`, `source_url`, `source_document`, `source_page`, `serial_number`, `raw_text`, `parser_version`, `extraction_method`, `extraction_confidence`, `extracted_at`, `is_valid`, `validation_notes` |
| Roll | `pdf_url`, `local_file_path`, `checksum_sha256`, `file_size_bytes`, `page_count`, `is_text_pdf`, `roll_year`, `roll_type`, `official_count_basis` |
| Station / AC / District / State | `source`, `source_url`, `edition` / `delimitation` |
| Fetch | `source_fetches`: URL, status, bytes, checksum, local path |

### Verified completeness

Checked across all **5,544** records:

```
source, source_url, source_document, source_page     : 0 missing
serial_number, part_number                           : 0 missing
raw_text                                             : 0 missing
parser_version, extraction_method, confidence        : 0 missing
extracted_at, is_valid                               : 0 missing
electors with a missing roll or station              : 0
roll-level provenance fields (13 fields × 5 rolls)   : 0 missing
```

### Raw data is never overwritten

Repaired text is stored **alongside** the original, never in place of it:

* `elector_name_raw` retained for **5,544 of 5,544** records
* **3,303** records have a repaired name that differs from the raw text — in every
  case both versions are stored
* `raw_text` (the complete original column text) is empty for **0** records

### Why it matters

* **Verification.** Any figure can be checked against the original document.
* **Correctability.** If a text-repair rule is later found wrong, records can be
  re-derived from stored raw text without re-downloading anything.
* **Honesty.** Because the raw text survives, the POC could prove that its
  "missing" records were absent from the source rather than lost in extraction.
* **Accountability.** Election data may be challenged. An unsourced number is not
  defensible.

---

## 14. Data quality system

### 14.1 Official count

**What it is:** the number of voters the document itself implies the booth has.

**How it is determined:** the 2003 roll prints **no summary total**, so the POC
derives the figure as the **highest serial number printed**, after excluding stray
serials (below). The basis is stored in words alongside the number and displayed
wherever the count appears:

```
official_count_basis = "highest serial number printed in the roll"
official_count_basis = "highest serial number printed, excluding 1 stray
                        serial(s) [2220] present in the source"
```

> **Important:** despite the column name `official_elector_count`, this is a
> **derived expectation, not a figure published by the Election Commission.**
> Recorded as finding F-7 in the Phase-1 audit.

### 14.2 Extracted count

The number of records actually read from the document and stored. Verified equal
to the physical row count in the database for every roll.

### 14.3 Difference

```
difference      = extracted count − official count
difference (%)  = difference ÷ official count × 100
```

A difference can be **negative** (the roll skips serial numbers — deleted voters)
or **positive** (the roll prints a serial twice, or prints a stray serial).

### 14.4 Extraction rate

```
extraction rate = total extracted ÷ total official × 100
```

Currently **99.82%** (5,544 ÷ 5,554). Because the denominator is derived from
serial numbers rather than a published total, an individual booth can exceed 100%.

### 14.5 Validation failure

A record that broke at least one rule. Rules implemented:

| Rule | Condition |
|---|---|
| Serial number | must be present |
| Elector name | must be present |
| Age | must be 18–120 if present |
| Gender | must map to `M`, `F` or `O`; otherwise flagged |
| Relation type | must be a known relationship |
| EPIC format | must match `ABC1234567` or the legacy `UP/1/423/0153455` form |
| Part consistency | record's part must match its parent polling station |
| Duplicate | see below |

**Failed records are flagged, never deleted.** `is_valid` is set to false and the
reason is written to `validation_notes`.

**Live totals across the database: 62 flagged records.**

| Rule that fired | Count |
|---|---:|
| Same name + relative + age as another serial | 48 |
| EPIC does not match a known format | 9 |
| Duplicate serial number | 4 |
| Gender not recognised | 1 |

Of the 62, **52 are duplicates** and **10 are independent** (9 malformed EPIC, 1
unrecognised gender).

> **Accuracy note.** Because duplicates also set `is_valid = false`, the headline
> "records passing validation: 98.88%" mostly measures **duplication in the
> published source**, not extraction quality. Recorded as finding F-4 in the
> Phase-1 audit.

### 14.6 Duplicate

Two kinds are detected:

| `duplicate_kind` | Meaning | Count |
|---|---|---:|
| `same_part_and_serial` | The same serial number printed twice in one roll | 4 |
| `same_name_relative_age` | The same person under two different serials | 48 |

**Total: 52 across the database.** No duplicate is deleted; each is linked to its
original in `validation_notes` and kept for review.

### 14.7 Confidence

A score from 0.0 to 1.0 measuring **how much of a record's text was successfully
decoded**. It is the share of characters that are not unmapped legacy glyphs,
averaged across the name and relative-name fields.

* `1.0` — every character decoded
* `< 1.0` — at least one character could not be mapped to a Hindi letter

Confidence measures **text decoding**, not whether the data is correct. A record
with confidence 1.0 can still be a duplicate.

**Current mean: 99.65%.**

### 14.8 Low-confidence records

Records scoring **below 0.95**. This threshold is used by both the booth analysis
and the dashboard ("Rows < 95%"). It is a review queue, not a failure.

### 14.9 Serial gaps

Serial numbers that are missing between 1 and the official count. **A gap means
the source document does not print that serial** — normally a voter deleted from
the roll. Verified for this data: the missing serials appear nowhere in the PDFs.

### 14.10 Stray serial

A serial number far beyond the end of the booth's normal numbering — detected when
there is a jump of more than 100 and the tail beyond it is at most 1% of records
(minimum 3).

The single example in the data is **serial 2220 in Part 10**, which has 1,276 rows
numbered 1–1275. Without this detection the booth would report ~945 phantom
missing voters.

A stray serial is excluded from the **official count basis only**. The record
itself is kept, with full provenance.

> **Accuracy note.** "Stray" is not a queryable field — it appears only in the
> free-text `official_count_basis`. Recorded as finding F-3 in the Phase-1 audit.

---

## 15. Part 6 case study

*A clean extraction.* Values are the current application output.

```text
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
```

### What each value means

| Value | Meaning |
|---|---|
| **342 electors** | 342 voter records read from a 12-page document |
| **175 male / 167 female** | Gender as printed in the roll, normalized |
| **0 other/unknown** | Every gender value was readable |
| **Age distribution** | All 342 records had a usable age; none unreadable |
| **Serial range 1–342** | Numbering runs from 1 to 342 |
| **0 gaps** | Every serial from 1 to 342 is present — no deleted voters |
| **212 EPIC (62.0%)** | 62% of voters had a voter-ID number printed. The rest genuinely had none — the column is optional. |
| **99.87% confidence** | Almost all text decoded cleanly |
| **6 rows below 95%** | Six records contain one unmapped character each |
| **0 validation failures** | No rule was broken |
| **0 duplicates** | Nobody appears twice |

### Why this is a clean sample

Three independent counts agree exactly:

```
Serial-number tokens in the raw PDF : 342
Records produced by the parser      : 342
Rows stored in the database         : 342
```

Serials 1–342 appear **exactly once each**, with no gaps and no repeats. Nothing
was discarded at any stage.

The six low-confidence records are individually identifiable and each is low for
one honest reason — a single Hindi conjunct character the repair table does not
cover (serials 122, 147, 184, 185, 212, 298). They are **not errors**; they are the
confidence system correctly declining to guess.

---

## 16. Part 10 case study

*A booth with real source anomalies.* Values are the current application output.

```text
Booth / Part: 10   (AC 15 राजपुर, Dehradun, edition ROLL-2003)
Polling station: जूनियर हाईस्कूल क0न0 2 मंगलूवाला
Roll: Final 2003   extraction: text

Total electors extracted: 1,276
  Male:           717
  Female:         558
  Other/Unknown:  1

Age distribution:
  18-25     219  ( 17.2%)
  26-40     517  ( 40.5%)
  41-60     416  ( 32.6%)
  61+       124  (  9.7%)

Serial range: 1-2220   gaps: 0   stray serials in the source: [2220]
Records carrying an EPIC: 594 (46.6%)

Counts:
  Official count:      1275   (basis: highest serial number printed, excluding
                               1 stray serial(s) [2220] present in the source)
  Extracted:           1276
  Difference:            +1   (+0.08%)

Extraction quality:
  Mean confidence:        99.60%
  Rows below 95% conf.:   42
  Rows failing validation:46
  Duplicates flagged:     45
```

> **These figures are extraction and data-quality observations about the
> *documents*. They say nothing about the voters named in them.** A record flagged
> as a duplicate means *this booth's published roll lists the same person twice* —
> not that anyone did anything wrong.

### What the numbers indicate

**1,276 extracted vs 1,275 official.** The roll numbers voters 1–1275 with no gaps,
then prints one extra row numbered **2220**. So 1,276 rows were printed, of which
1,275 are uniquely numbered.

**Stray serial 2220.** Verified directly against page 45 of the source PDF: the
token `2220` is printed at exactly the same position and width as its neighbours
`1273`, `1274`, `1275`. It is genuinely in the document — not a parsing error. Its
record duplicates serial 220 field for field (same name, relative, gender and age,
and both rows are missing the relation column in the source). It is a **source
data-entry anomaly**: elector 220 re-printed with a mistyped serial.

The record is **kept**, flagged `is_valid = false` with
`duplicate_kind = same_name_relative_age`, and full provenance. It is excluded only
from the *expected count basis*.

**45 duplicates.** Investigated individually. **None is a parser artifact** — each
pair was located in the source PDF on different pages with different printed
serials. No serial number is repeated in this booth. Systematic offsets (+269 nine
times, +461 eight times, +637 six times) show that whole blocks of the roll were
re-registered — a known real-world problem with electoral rolls, which is exactly
what de-duplication processes exist to find.

**46 validation failures.** 45 are the duplicates above; **1 is independent** —
serial 725, described below.

**1 other/unknown gender.** Serial 725. The source PDF prints `पति` ("husband", a
*relationship* word) in the **gender** column. Verified by position: the word sits
at x=365.1, squarely inside the gender column, so this is not a parser
misalignment — the document genuinely has the wrong kind of word there.

The POC records the gender as `UNKNOWN`. Context suggests female (the name is
लक्ष्मी, and the named relative is the male voter at serial 724), but **that is an
inference, not what the document says**, so no correction was applied.

**42 rows below 95% confidence.** Each contains at least one Hindi conjunct
character the repair table does not cover.

### Source anomalies vs parser anomalies

This distinction is central to how the POC reports quality:

| Observation | Classified as | Evidence |
|---|---|---|
| Serial 2220 | **Source anomaly** | Token printed at identical geometry to its neighbours |
| 45 duplicates | **Source anomaly** | Both rows found in the PDF, different pages, different serials |
| Serial 725 gender | **Source anomaly** | `पति` printed inside the gender column |
| Low-confidence rows | **Extraction limitation** | Unmapped font characters, honestly scored |
| Missing records | **None found** | Raw PDF token count equals database row count for all five parts |

---

## 17. Form 20 / election result data

### What Form 20 is

Form 20 is the **Final Result Sheet** prescribed by the Conduct of Elections Rules,
1961. After counting, the Returning Officer publishes, **for every polling
station**, how many votes each candidate received. It is the only official
publication of results at booth level.

### What the POC processes

* **Election:** Vidhan Sabha (State Assembly) **2012**
* **Constituency:** AC 19 — Raipur
* **Document:** 13 pages, landscape, text-based, Hindi in the Kruti Dev legacy
  encoding
* **Booth rows parsed: 152**
* **Stored in `election_results`: 152 rows** *(verified against the database)*

The document's own header states the constituency had **120,008 electors**
including service voters.

### What is extracted per booth

| Field | Stored | Notes |
|---|---|---|
| Polling station serial | Yes | as `part_number` |
| Polling station name | Yes | transliterated from Kruti Dev |
| Per-candidate vote counts | **Parsed but not stored per candidate** | see limitation below |
| Total valid votes | Yes | `total_valid_votes` |
| Tendered votes | Yes | `tendered_votes` |
| Total votes | Yes | `total_votes` |
| Source page | Yes | provenance |

### How candidate votes are handled

**Candidate names are NOT attributed to vote columns.** In the source PDF the
candidate names are printed as **rotated column headers**, which the PDF library
emits detached from the columns they belong to. The number of vote columns also
varies between booths (21, 22 or 23) because a column of zeros is sometimes not
printed.

Rather than guess an alignment, the POC stores only the totals the document labels
directly, and leaves `candidate_name`, `party` and `votes` empty. This is a
deliberate choice to avoid publishing plausible-looking but unverifiable
attributions.

### Vote-sum validation

The document guarantees one arithmetic relationship: the per-candidate votes must
add up to the printed *total valid votes*. The POC checks this for every booth.

**Verified live: 151 of 152 booths pass (99.3%).** A row passes only if its vote
columns sum to the printed total valid votes and it has the sheet's column count.
The one row requiring review is part 141 (a blank printed candidate cell shifts
its columns); it is shown as review required with its margin withheld, not hidden.
An earlier parser version misread part 87; that was corrected.

### Known limitations

| Limitation | Detail |
|---|---|
| Candidate-column alignment | Column count varies (21/22/23); names cannot be matched to columns |
| Only one election year processed | 2012 only |
| Form 20 2017 | Embedded OCR layer is corrupted — **not processed** |
| Form 20 2022 | Scanned images, no text layer — **not processed**, needs OCR |
| `ac_id` not populated | Results link to a constituency by bare `ac_number` (audit finding F-5) |
| Dashboard part-number join | Station page links 2012 results to 2026 stations by part number, which is unreliable (audit finding F-1) |

---

## 18. Command line interface

All commands below were verified against `python -m app --help`. **These 11
commands are the complete set.**

Run from the project root with `src` on the Python path:

```bash
# Linux/macOS
export PYTHONPATH=src
# Windows PowerShell
$env:PYTHONPATH="src"
```

| Command | Purpose |
|---|---|
| `init-db` | Create the database schema |
| `discover-state` | Resolve a state to its official ECI code |
| `discover-districts` | List a state's districts |
| `discover-acs` | List a district's constituencies |
| `discover-parts` | List a constituency's polling stations |
| `inspect-roll` | Report whether a PDF is text or a scan, and OCR engine status |
| `extract-roll` | Parse a roll PDF and print records (no database write) |
| `analyze-booth` | Booth statistics for an ingested part |
| `quality` | Data-quality dashboard in the terminal |
| `pipeline` | Run the whole POC end to end |
| `serve` | Start the web dashboard |

---

### `init-db`

Creates all 11 tables.

* **Input:** `--drop` to delete and recreate (destructive)
* **Output:** confirms which database backend was used
* **When:** once before the first run

```bash
python -m app init-db
python -m app init-db --drop      # wipes existing data
```

---

### `discover-state`

* **Input:** `--state` (default `Uttarakhand`), `--concurrency`, `--verbose`
* **Output:** the official code, ECI internal id and state type
* **When:** to confirm the state resolves, or to find another state's code

```bash
python -m app discover-state --state Uttarakhand
# Uttarakhand -> code S28 (ECI stateId 27, type ST)
```

---

### `discover-districts`

* **Input:** `--state`, `--concurrency`, `--verbose`
* **Output:** table of district codes, numbers and names
* **When:** to find a district code before running the pipeline

```bash
python -m app discover-districts --state Uttarakhand
```

---

### `discover-acs`

* **Input:** `--state`, `--district` (default `Dehradun`)
* **Output:** AC number, name, category, PC number and ECI internal id
* **Failure:** an unknown district name lists the valid ones

```bash
python -m app discover-acs --state Uttarakhand --district Dehradun
```

---

### `discover-parts`

* **Input:** `--ac` (default `19`), `--limit` (default 20)
* **Output:** part numbers and names for the current (SIR 2026) revision
* **When:** to see a constituency's booths before ingesting

```bash
python -m app discover-parts --ac 19 --limit 20
```

---

### `inspect-roll`

* **Input:** `--roll <path to PDF>`
* **Output:** page count, whether a text layer exists, which route would be taken,
  and whether the OCR engine is available
* **When:** before extracting an unfamiliar document

```bash
python -m app inspect-roll --roll data/raw/roll2003/AC15/P0006.pdf
```

---

### `extract-roll`

Parses a PDF and prints the records. **Writes nothing to the database** — useful
for checking a document before ingesting it.

* **Input:** `--roll <path>`, `--limit` (rows to display), `--out <file.json>`
* **Output:** header details plus a table of records; optionally full JSON
* **Failure:** if the PDF is a scan and OCR is unavailable, it says so and exits

```bash
python -m app extract-roll --roll data/raw/roll2003/AC15/P0006.pdf --limit 10
python -m app extract-roll --roll data/raw/roll2003/AC15/P0006.pdf --out out.json
```

---

### `analyze-booth`

* **Input:** `--part` (required), `--ac`, `--edition`
* **Output:** the full booth report — counts, gender split, age bands, serial
  range and gaps, EPIC coverage, official vs extracted, quality metrics, source
* **Failure:** if the part has not been ingested it says so

```bash
python -m app analyze-booth --part 6
python -m app analyze-booth --part 10 --edition ROLL-2003
```

> **Note:** `--edition` is optional today. Part numbers exist in both the
> `ROLL-2003` and `SIR-2026` editions, so once current-year rolls are ingested the
> same part number will match two booths. Passing `--edition` is recommended.

---

### `quality`

* **Input:** none
* **Output:** database backend, coverage counts for nine entity types, and ten
  quality metrics
* **When:** after any pipeline run

```bash
python -m app quality
```

---

### `pipeline`

Runs the complete POC.

| Option | Default | Meaning |
|---|---|---|
| `--district` | `Dehradun` | Current district to discover |
| `--ac` | `19` | Current AC to process |
| `--legacy-district` | `देहरादून` | 2003 district (Hindi) |
| `--legacy-ac` | `राजपुर` | 2003 AC (Hindi) |
| `--limit` | `5` | How many parts to ingest |
| `--parts` | — | Explicit parts, e.g. `6,7,8,9,10` |
| `--concurrency` | `3` | Parallel request budget; also sets the politeness delay |
| `--resume / --no-resume` | resume | Reuse already-downloaded PDFs |
| `--dry-run` | off | Print the plan; make no requests and no writes |
| `--skip-form20` | off | Skip Form 20 ingestion |
| `--skip-mapping` | off | Skip part-mapping ingestion |
| `--verbose / --quiet` | verbose | Logging level |

```bash
python -m app pipeline --dry-run
python -m app pipeline --parts 6,7,8,9,10
python -m app pipeline --district Dehradun --ac 19 --limit 5
```

Output is a step-by-step table showing each stage, whether it succeeded, how many
records it produced and any warnings, followed by HTTP statistics and the database
backend in use.

---

### `serve`

* **Input:** `--host` (default `127.0.0.1`), `--port` (default `8000`)
* **Output:** runs the dashboard until stopped with **Ctrl+C**

```bash
python -m app serve
python -m app serve --host 127.0.0.1 --port 8085
```

---

## 19. Dashboard / UI documentation

The dashboard is a small server-rendered web application. **There is no JavaScript
framework, no build step and no login.** It exists to make the stored data
browsable and the quality visible.

### Routes — the complete set

Verified from the application's route table. **Four routes exist.**

| Page | Route | Purpose | Main information |
|---|---|---|---|
| Data quality home | `/` | Landing page and quality summary | Six headline metrics, coverage counts, official-vs-extracted per roll, district list |
| District | `/district/{district_id}` | Constituencies in a district | AC number, name, category, PC, delimitation, station count |
| Constituency | `/ac/{ac_id}` | Booths and results for one AC | Polling station list; Form 20 booth results |
| Polling station | `/station/{station_id}` | Everything about one booth | Booth overview, age distribution, quality, part mapping, elector table, booth result |

The station page accepts a `?page=N` query parameter for paging through electors
(100 per page).

> There is no search, no filter control and no data-export button. Navigation is by
> clicking through the hierarchy.

---

### 19.1 Home — `/`

**Title:** *Data quality*

The landing page answers "can I trust this data?" before showing any of it.

**Displayed:**

* **Database backend** — confirms whether PostgreSQL or the fallback is in use
* **Six headline metric cards:** Extraction rate, HTTP success, Mean confidence,
  Passing validation, Duplicates flagged, Records with EPIC
* **Coverage table:** Districts, ACs (current), ACs (2003), Polling stations,
  Rolls, Electors, Part mappings, Booth results
* **Ingested rolls — official vs extracted:** one row per roll showing AC, Part,
  Roll, Official, Extracted, Diff, Basis and Source. The Diff is colour-coded:
  green at zero, amber within ±15, red beyond.
* **Browse:** the district list, each linking onward
* **A standing note** explaining that elector records come from the published 2003
  roll and that current SIR 2026 rolls are CAPTCHA-gated and not downloaded

**What a user learns:** whether extraction is complete, where it is not, and on
what basis each "official" figure was derived.

**Limitations:** the "Polling stations" count combines both editions (234 current +
5 historical) without splitting them.

---

### 19.2 District — `/district/{district_id}`

**Title:** the district name, in English and Hindi

**Displayed:** district code and source; then a table of constituencies with
**AC no**, **Name**, **Category**, **PC**, **Delimitation** and **Polling
stations**.

**What a user learns:** which constituencies exist in the district and how many
booths are catalogued for each.

**Why the Delimitation column matters:** for Dehradun this page shows two rows
numbered 15 — *राजपुर (2003)* and *Chakrata (current)*. The Delimitation column is
what distinguishes them.

---

### 19.3 Constituency — `/ac/{ac_id}`

**Title:** AC number and name

**Displayed:**

* Category, delimitation and the ECI internal id
* **Polling stations / parts:** Part, Name, Areas, Edition, and whether a roll has
  been ingested
* **Booth-level results — Form 20** (only when results exist): Part, Polling
  station, Valid votes, Tendered, Total, Page — with a note explaining that
  candidate names cannot be reliably matched to their vote columns

**What a user learns:** the full booth list for a constituency, which booths have
voter data, and how each booth voted historically.

**Limitations:**

* The Form 20 table **does not display the election year**. The data shown is from
  **2012**, on a page headed by the current constituency. *(Audit finding F-6.)*
* Only the first 400 result rows are shown.

---

### 19.4 Polling station — `/station/{station_id}`

The most detailed page. **Title:** *Part N*.

**Sections, in order:**

1. **Header** — station name, edition (e.g. `ROLL-2003`), source system, and the
   areas the station serves
2. **Booth overview — {roll type} {year}** — six cards: Official count (with its
   basis), Extracted, Difference, Male, Female, Other/unknown
3. **Age distribution** — four bands with counts, bars and percentages
4. **Extraction quality** — Mean confidence, Rows < 95%, Failing validation,
   Duplicates flagged, Serial range, Serial gaps, With EPIC; plus the source
   document, page count, extraction method and full URL
5. **Official part mapping (2003 → 2025)** — Area/village, 2003 AC/part, 2025
   AC/part, Method, Confidence; with a note that one old part can split across
   several new ones
6. **Electors** — paginated table: Sl, Name, Relation, Relative, Gender, Age,
   EPIC, House, Pg (source page), Conf, Flags. Flags show `dup` for duplicates and
   `check` for validation failures.
7. **Booth result (Form 20)** — Year, Station, Valid, Tendered, Total

**What a user learns:** everything known about one booth, down to the individual
record and the page it was read from.

**Limitations:**

* The **House** column is always empty — the 2003 roll does not print it
* The Form 20 section matches results by part number alone, which is unreliable
  across years *(audit finding F-1 — see [section 27](#27-current-limitations))*
* A station with no ingested roll (all `SIR-2026` stations) shows "0 records"

---

## 20. UI metric explanation

Every metric visible on the dashboard, in plain language.

### Extraction rate

* **What it means:** how much of the expected voter data we actually recovered.
* **How it is calculated:** total extracted records ÷ total official count × 100.
* **Why it matters:** the single number that says whether extraction is working.
  Currently **99.82%**.

### HTTP success

* **What it means:** the share of file and API downloads that succeeded.
* **How it is calculated:** successful fetches ÷ total fetches × 100, from the
  `source_fetches` audit log.
* **Why it matters:** distinguishes a data problem from a network problem.
* **Caveat:** the log is append-only, so this accumulates across runs.

### Mean confidence

* **What it means:** on average, how much of each record's text decoded cleanly.
* **How it is calculated:** average of `extraction_confidence` across all records.
* **Why it matters:** a falling value signals that documents changed or the text
  repair rules no longer fit. Currently **99.65%**.

### Passing validation

* **What it means:** share of records that broke no rule.
* **How it is calculated:** valid records ÷ all records × 100.
* **Why it matters:** a review queue indicator. Currently **98.88%** — note that
  most failures are duplicates present in the source, not extraction errors.

### Duplicates flagged

* **What it means:** records that repeat another record.
* **How it is calculated:** count of records with a `duplicate_kind`.
* **Why it matters:** duplicates in a published roll are a real-world data-quality
  problem worth measuring. **None are deleted.** Currently **52**.

### Records with EPIC

* **What it means:** how many voters had a voter-ID number printed.
* **How it is calculated:** count of records with a non-empty `epic_number`.
* **Why it matters:** EPIC is the only stable identifier for a voter; coverage
  determines whether records can be matched across rolls. Currently **2,702**
  (48.7%).

### Official count

* **What it means:** how many voters the document implies the booth has.
* **How it is calculated:** highest printed serial number, excluding strays.
* **Why it matters:** the benchmark extraction is measured against. **Always read
  the "basis" text next to it** — it is derived, not published.

### Extracted count

* **What it means:** how many records were actually read and stored.
* **How it is calculated:** a count of stored records.
* **Why it matters:** the other half of the comparison.

### Difference

* **What it means:** extracted minus official.
* **How it is calculated:** subtraction, with a percentage.
* **Why it matters:** negative usually means the roll skips serials (deleted
  voters); positive means the roll repeats or strays.

### Male / Female / Other-unknown

* **What it means:** gender counts as printed in the roll.
* **How it is calculated:** grouped by the normalized `gender` field. "Other /
  unknown" covers both a genuine third-gender entry and a value that could not be
  read.
* **Why it matters:** the basic demographic shape of a booth. A high
  other/unknown count would indicate a reading problem.

### Age distribution

* **What it means:** voters grouped into 18–25, 26–40, 41–60 and 61+.
* **How it is calculated:** counted from the normalized `age` field; ages outside
  18–120 are excluded and reported separately.
* **Why it matters:** the demographic profile of a booth.

### Rows < 95%

* **What it means:** records with at least one character that could not be decoded.
* **Why it matters:** a targeted review list rather than a blanket failure.

### Failing validation

* **What it means:** records that broke at least one rule, with the reason stored.
* **Why it matters:** each one is a specific, inspectable observation.

### Serial range and Serial gaps

* **What it means:** the lowest and highest serial numbers, and how many are
  missing in between.
* **Why it matters:** gaps normally mean deleted voters. Confirming that a gap is
  absent from the source — rather than lost in extraction — is exactly what the
  provenance data makes possible.

### With EPIC (per booth)

* **What it means:** how many of this booth's voters have a voter-ID printed.
* **Why it matters:** coverage varies by booth (62.0% in Part 6, 46.6% in Part 10).

---

## 21. User journey

```text
Open the dashboard  (http://127.0.0.1:8000)
        ↓
Read the quality summary
  "Is this data trustworthy? Where is it incomplete?"
        ↓
Pick a district from the Browse list
        ↓
Pick an Assembly Constituency
  (check the Delimitation column — current or historical)
        ↓
Pick a Polling Station / Part
  (the "Roll" column shows which booths have voter data)
        ↓
Read the Booth overview
  official vs extracted, gender split, age distribution
        ↓
Review the Extraction quality panel
  confidence, validation failures, duplicates, serial gaps
        ↓
Check the Official part mapping
  how this 2003 booth relates to today's booths
        ↓
Inspect individual elector records
  page through them; flagged rows are marked "dup" or "check"
        ↓
Trace anything questionable back to its source
  every record shows its source page; the panel shows the document URL
```

A typical first session: open `/`, note the extraction rate and that five rolls are
ingested, click **Dehradun**, see both AC 15 rows and understand why the
Delimitation column exists, click **AC 15 राजपुर**, click **Part 6**, and read a
clean booth end to end. Then open **Part 10** to see what an imperfect source looks
like and how the system reports it.

---

## 22. Technical stack

Versions are those actually installed in the project's virtual environment.

| Component | Technology | Version | Purpose |
|---|---|---|---|
| Language | Python | 3.14.2 | Application |
| Web framework | FastAPI | 0.141.1 | Dashboard routes |
| Application server | Uvicorn | 0.52.4 | Serves the dashboard |
| Templating | Jinja2 | 3.1.6 | Server-rendered HTML (via FastAPI) |
| Database | PostgreSQL | 17.11 | Persistent storage, in Docker |
| ORM | SQLAlchemy | 2.0.52 | Table definitions and queries |
| Database driver | psycopg | 3.3.5 | PostgreSQL connectivity |
| HTTP client | httpx | 0.28.1 | All API and document downloads |
| PDF processing | PyMuPDF | 1.28.2 | Text extraction with coordinates, page rendering |
| HTML parsing | BeautifulSoup4 + lxml | 4.15.0 / 6.1.3 | Reading official index pages |
| OCR wrapper | pytesseract | 0.3.13 | Scanned PDFs *(engine not installed)* |
| Image handling | Pillow | 12.3.0 | Page images for OCR |
| Browser automation | Playwright | 1.62.0 | **Research scripts only**, not the pipeline |
| CLI framework | Typer | 0.27.2 | Command line interface |
| Terminal output | Rich | 15.0.0 | Tables and formatting |
| Settings | Pydantic / pydantic-settings | 2.13.5 | Configuration from environment |
| Testing | pytest | 9.1.1 | 65 automated tests |
| Containerization | Docker Compose | — | Runs PostgreSQL |

### PostgreSQL's role

PostgreSQL is the system of record. It holds every table described in
[section 12](#12-database-architecture) and is run as a container defined in
`docker-compose.yml`, published on host port **5433** (chosen to avoid clashing
with any locally installed PostgreSQL).

A **SQLite fallback** exists so the pipeline can still run on a machine without
PostgreSQL. When it is used, the CLI and dashboard both display
`sqlite (FALLBACK — PostgreSQL was unreachable)` so a run is never silently
non-PostgreSQL. **The current data is in PostgreSQL.**

> **Declared but unused.** `requirements.txt` also lists `pdfplumber`, `pypdf`,
> `numpy` and `tenacity`. None is imported by the application — the HTTP client
> implements its own retry logic and PyMuPDF handles all PDF work. They are
> leftovers from evaluation and could be removed.

---

## 23. Project directory structure

```text
POC/
├── README.md                  Quick start and overview
├── requirements.txt           Python dependencies
├── pyproject.toml             Package metadata
├── pytest.ini                 Test configuration
├── docker-compose.yml         PostgreSQL container definition
├── .env.example               Configuration template (no secrets)
├── .gitignore
│
├── config/
│   └── sources.yaml           Registry of all 16 data sources (documentation)
│
├── docs/
│   ├── DATA_SOURCE_RESEARCH.md      Which sources exist and why each was chosen
│   ├── API_FINDINGS.md              Exact request/response shapes observed
│   ├── UTTARAKHAND_ROLL_FORMAT.md   Document layouts and text encodings
│   ├── PRIVACY_AND_COMPLIANCE.md    What the POC will and will not do
│   ├── POC_FINAL_REPORT.md          Build report and results
│   ├── PHASE1_DATA_INTEGRITY_AUDIT.md  Independent audit of the stored data
│   └── POC_COMPLETE_DOCUMENTATION.md   This document
│
├── research/
│   ├── samples/               Captured API responses (JSON)
│   └── network/               Browser research traces and screenshots
│
├── scripts/
│   ├── inspect_eci_api.py     Probe endpoints and save sample responses
│   └── research_portals.py    Browser research; re-verify CAPTCHA gates
│
├── src/app/
│   ├── __main__.py            Entry point for `python -m app`
│   ├── config.py              Settings loaded from environment
│   ├── http_client.py         Retries, backoff, politeness delay, legacy TLS
│   ├── sources/
│   │   ├── eci_api/           Election Commission adapter (public endpoints only)
│   │   └── ceo_uttarakhand/   CEO adapter: parts, 2003 roll, Form 20
│   ├── extraction/
│   │   ├── pdf/               Download, checksum, text-vs-scan detection
│   │   ├── ocr/               Tesseract Devanagari (engine not installed)
│   │   └── parsers/           krutidev, devanagari_fix, roll_2003,
│   │                          ps_list_2026, form20_2012
│   ├── database/
│   │   ├── models.py          All 11 tables
│   │   ├── repositories.py    Idempotent inserts and updates
│   │   └── session.py         Engine, PostgreSQL with SQLite fallback
│   ├── services/
│   │   ├── pipeline.py        Orchestration of the whole flow
│   │   └── validation.py      Rules, duplicates, stray-serial detection
│   ├── analytics/booth.py     Booth statistics and quality report
│   ├── browser/               Playwright research helpers (NOT the pipeline)
│   ├── cli/main.py            The 11 commands
│   └── web/                   FastAPI app and 5 HTML templates
│
├── tests/
│   ├── fixtures/              Slices of real published PDFs
│   ├── test_text_encoding.py  13 tests
│   ├── test_parsers.py        18 tests
│   ├── test_validation.py     14 tests
│   ├── test_database.py       12 tests
│   └── test_sources.py         8 tests
│
└── data/
    ├── raw/                   Downloaded PDFs (not in version control)
    ├── processed/
    └── fixtures/
```

### Directory responsibilities

| Directory | Responsibility |
|---|---|
| `src/app/sources/` | One adapter per data owner. Adding a state means adding an adapter here, not changing the database or pipeline. |
| `src/app/extraction/` | Turning documents into records. Independent of how a document was obtained. |
| `src/app/database/` | Table definitions and idempotent writes. |
| `src/app/services/` | Orchestration and quality rules. |
| `src/app/analytics/` | Read-only statistics. |
| `src/app/web/` | The dashboard. |
| `src/app/browser/` | Research only. Never used by the pipeline. |
| `scripts/` | Standalone research utilities. |
| `docs/` | All written analysis and reports. |
| `research/` | Raw evidence captured during research. |
| `tests/fixtures/` | Real PDF slices so parsers are tested against true layouts. |

---

## 24. Data storage structure

| What | Where | In version control? |
|---|---|---|
| Downloaded roll PDFs | `data/raw/roll2003/AC15/P00NN.pdf` | No |
| Polling station list PDF | `data/raw/ps_list_2026/19.pdf` | No |
| Form 20 PDF | `data/raw/form20/2012_AC19.pdf` | No |
| Extracted records | PostgreSQL (`uk_election` database) | No |
| Test fixtures | `tests/fixtures/*.pdf` | Yes |
| Captured API samples | `research/samples/*.json` | Yes |
| Browser research traces | `research/network/` | Partially |
| Source registry | `config/sources.yaml` | Yes |
| Configuration template | `.env.example` | Yes |
| Local configuration | `.env` | **No** |

**Raw PDFs are kept permanently** as the evidentiary record. Each is stored with a
SHA-256 checksum in `electoral_rolls` so it can be proved unchanged.

**Logs** are written to the terminal, not to files. The HTTP client logs URLs,
status codes, byte counts and request ids. Parsers log counts and confidence.
**No personal data is written to logs** — there is no code path that writes voter
records to the application log.

### Secrets

Environment-specific secrets are stored in `.env` and are intentionally not
documented. `.env` is excluded from version control; `.env.example` contains only
placeholders. **No credentials of any kind are required for any data source the
POC uses** — every source is public and unauthenticated.

---

## 25. API / endpoint flow

### 25.1 Public endpoints used by the pipeline

#### ECI Gateway — `https://gateway-voters.eci.gov.in/api/v1`

| Endpoint | Method | Parameters | Response | How the POC uses it |
|---|---|---|---|---|
| `/common/states` | GET | none | Array of 36 states | Resolves "Uttarakhand" → `S28`; stores the code |
| `/common/districts/{stateCd}` | GET | state code in the path | Array of 13 districts | Populates `districts`; gives codes like `S2813` |
| `/common/acs/{districtCd}` | GET | district code in the path | Array of constituencies | Populates `assembly_constituencies` with number, names, category, PC |

No authentication, no CAPTCHA, no API key. Responses are bare JSON arrays.

#### CEO Uttarakhand — `https://election.uk.gov.in`

| Endpoint | Method | Parameters | Response | How the POC uses it |
|---|---|---|---|---|
| `/asdlist/SearchAdsEpic/Parts` | GET | `acNo` | Array of parts | Current polling stations for a constituency |
| `/PSSIR2026/{acNumber}.pdf` | GET | AC number in the path | PDF | Station names and areas served |
| `/search2003uk/api/uklegacydata/district-names` | GET | none | 13 districts (Hindi) | 2003 hierarchy |
| `/search2003uk/api/uklegacydata/ac-names` | GET | `districtName` | ACs in that district | 2003 hierarchy |
| `/search2003uk/api/uklegacydata/part-names` | GET | `acName` | Parts in that AC | 2003 booth list |
| `/search2003uk/api/uklegacydata/villages` | GET | `searchText` | Matching villages | Input to the mapping lookup |
| `/search2003uk/api/uklegacydata/village-details` | GET | `villageName` | Official 2003→2025 mapping | Populates `part_mapping` |
| `/search2003uk/api/uklegacydata/pdf` | GET | `filePath` | PDF | **The electoral roll itself** |
| `/Vidhan_sabha2012/form20_2012_PDF/form20_2012.htm` | GET | none | HTML index of 70 ACs | Locates the Form 20 file |
| `/Vidhan_sabha2012/form20_2012_PDF/{nn}-{Name}.pdf` | GET | — | PDF | Booth-level results |

The roll PDF path follows the pattern the official portal itself uses:

```
files/Roll2003/{AC:02d}-{AC name in Hindi}/P{AC:03d}{part:04d}.pdf
```

> **Technical note.** `election.uk.gov.in` requires legacy TLS renegotiation, which
> modern Python refuses by default. The HTTP client enables
> `OP_LEGACY_SERVER_CONNECT` for that host only.

### 25.2 Endpoint status classification

| Status | Endpoints |
|---|---|
| **Public / official — used** | The 13 endpoints above |
| **Researched but not used** | Legacy 2003 `/search`, `/epic-no`, `/export` — they return individual voter records and would constitute a brute-force voter search |
| **CAPTCHA-gated — not automated** | ECI e-roll PDF download; `captcha-service/getCaptcha/EROLL`; CEO `SearchAdsEpic/SearchEpic`, `DownloadAsdPdf`, `DownloadBlaMinutes`; the 2018–2023 roll portal |
| **Encrypted parameters — not automated** | ECI `printing-publish/get-publish-eroll-type`, `get-ac-languages`, `get-publish-part-list` |
| **Requires authentication — never called** | ECI `authn-voter/*` |
| **Forbidden by the server** | Directory listings under `/Pdf_Roll/`, `/PSSIR2026/` (HTTP 403) |

### 25.3 Request behaviour

* **Concurrency:** 3 by default, configurable with `--concurrency`
* **Delay:** at least 0.4 seconds between requests
* **Retries:** 4 attempts with exponential backoff and jitter; only HTTP 429 and
  5xx are retried; `Retry-After` is honoured
* **Validation:** a response claiming success is rejected if its content type is
  wrong — an HTML error page is never saved as a PDF
* **Audit:** every fetch is recorded in `source_fetches`

---

## 26. Security and ethical limitations

This section describes deliberate design decisions. It is **not legal advice**.

### What the POC does not do

| Not done | Detail |
|---|---|
| **CAPTCHA bypass** | No solving, submitting, replaying or automating of any CAPTCHA — not by OCR, not by a solving service, not through a browser |
| **Authentication bypass** | No authenticated endpoint is called; no credentials, borrowed or otherwise, are used |
| **Anti-bot circumvention** | Browser automation is used only for research, with no fingerprint spoofing |
| **Internal system access** | ERONET and other internal ECI systems are never accessed |
| **Brute-force voter search** | The legacy portal's per-voter search API is open but deliberately unimplemented |
| **Rate-limit circumvention** | Conservative defaults; `Retry-After` honoured |
| **Private mobile-number scraping** | Electoral rolls contain no phone numbers, and none are collected or inferred |
| **Reverse mobile-to-voter enumeration** | Not implemented and not possible from this data |
| **Contact enrichment** | No name-matching of voters against external contact data |
| **Political targeting** | No segmentation, scoring or targeting features exist |
| **Circumventing a 403** | Blocked directory listings are respected; official index pages are used instead |

### Enforced by code, not policy

* `tests/test_sources.py` **fails** if a CAPTCHA-gated endpoint ever appears as a
  call in a source adapter, or if the per-voter search method is ever added.
* The `electors` table has **no** `mobile`, `phone`, `email` or `contact` column,
  and `tests/test_database.py` fails if one is added.
* A separate `contact_records` table exists for consented data only, with mandatory
  `source`, `consent_status` and `consent_timestamp` columns. **It is never
  populated by the pipeline and currently holds 0 rows.**

### What the POC focuses on instead

Official, published election data — the documents an Electoral Registration Officer
puts out for public inspection — and rigorous measurement of how completely and
accurately that data can be recovered.

`docs/PRIVACY_AND_COMPLIANCE.md` records the compliance questions that must be
answered before this work is scaled or used for anything beyond data quality
analysis.

---

## 27. Current limitations

| Limitation | Current status | Impact | Possible future work |
|---|---|---|---|
| Current-year (2026) rolls cannot be downloaded automatically | **Blocked** — CAPTCHA-gated on the ECI portal | No current voter records; the POC uses the 2003 roll instead | A data-sharing request, or a supervised manual download; the parser already accepts any file |
| OCR not demonstrated on a real scan | **Partially implemented** — code written and unit-tested, Tesseract not installed | Form 20 2017/2022 cannot be processed | Install Tesseract with Hindi data; benchmark PaddleOCR/EasyOCR |
| Voter records are from the 2003 delimitation | **By design** given the CAPTCHA | Constituency and booth numbers differ from today's | Official `part_mapping` already links them |
| Dashboard links Form 20 results to stations by part number | **Defect (audit F-1)** | Attributes 2012 votes to the wrong booth for ~97% of stations | Route through `part_mapping`, or show results only at AC level |
| `duplicate_of_id` never populated | **Defect (audit F-2)** | Duplicate links exist only as free text | Populate the existing column at ingest |
| "Stray" and "excluded" are not queryable | **Gap (audit F-3)** | Stray records discoverable only as duplicates | Add `record_status` + `exclusion_reason` columns |
| Validation mixes duplicates with data errors | **Gap (audit F-4)** | "98.88% passing validation" mostly measures source duplication | Report the two separately |
| `election_results.ac_id` not populated | **Gap (audit F-5)** | Results keyed by bare AC number | Populate the existing foreign key |
| Form 20 AC page omits the election year | **Defect (audit F-6)** | 2012 results appear under a current-AC heading | One-line template change |
| Form 20 candidate columns cannot be aligned | **Source limitation** | Per-candidate votes are not stored | Would require OCR of rotated headers |
| `official_elector_count` is derived, not published | **By design, documented** | Extraction rate can exceed 100% for a booth | Rename, or annotate in the UI |
| ~3% of records contain an unmapped character | **Known** | Reflected in confidence scores | Extend the character map from the confidence report |
| `section_number` and `house_number` always empty | **Source limitation** | Columns unused | Populated if a future roll prints them |
| `config/sources.yaml` not read at run time | **Documentation only** | Registry can drift from the code | Load it, or note it clearly (done here) |
| Four declared dependencies unused | **Housekeeping** | Larger install than needed | Remove `pdfplumber`, `pypdf`, `numpy`, `tenacity` |
| No database migrations | **Not implemented** | Schema changes need a rebuild | Add Alembic |
| Scale untested | **By design** | 5 booths of ~12,000 statewide | Phase 2 |
| No authentication on the dashboard | **By design for a local POC** | Not suitable for public hosting | Add access control before deployment |
| `--edition` optional on `analyze-booth` | **Latent risk** | Will become ambiguous once 2026 rolls are ingested | Make it required or default to newest |

---

## 28. What has been completed

Every item verified before inclusion.

```text
[x] Dynamic state discovery (Uttarakhand -> S28, never hard-coded)
[x] District discovery (13 districts)
[x] Assembly Constituency discovery (10 current ACs)
[x] Polling station / part discovery (234 current stations)
[x] Polling station enrichment from the official PS List 2026 PDF
[x] Historical electoral roll acquisition (5 official PDFs, checksummed)
[x] PDF text-vs-scan detection
[x] PDF text extraction (geometric column parsing)
[x] Legacy text decoding - Kruti Dev encoding
[x] Legacy text decoding - conjunct glyph repair
[x] Elector record parsing (8 fields)
[x] Value normalization (gender, relation, age, EPIC)
[x] Validation rules with reasons stored
[x] Duplicate detection (two kinds)
[x] Stray serial detection
[x] Confidence scoring per record
[x] PostgreSQL storage (11 tables, idempotent writes)
[x] Complete provenance (zero missing fields across 5,544 records)
[x] Booth-level analytics
[x] Data quality reporting (CLI and dashboard)
[x] Official part mapping 2003 -> 2025 (17 rows)
[x] Form 20 ingestion (Vidhan Sabha 2012, 152 booth results)
[x] Form 20 vote-sum self-check (151/152 pass)
[x] Web dashboard (4 routes)
[x] Command line interface (11 commands)
[x] Automated tests (65, all passing)
[x] Research utilities (endpoint probing, CAPTCHA-gate re-verification)
[x] Docker Compose PostgreSQL setup
[x] Documentation (7 documents including this one)
[x] Independent Phase-1 data integrity audit
```

---

## 29. What has not been completed

### Not implemented

```text
[ ] Current-year (2025/2026) electoral roll ingestion
[ ] Per-candidate vote storage from Form 20
[ ] Database schema migrations (Alembic)
[ ] Dashboard search, filtering or data export
[ ] Dashboard authentication
[ ] Any state other than Uttarakhand
[ ] Statewide scale (5 of ~12,000 booths)
[ ] Automated monitoring or alerting on quality regressions
```

### Partially implemented

```text
[~] OCR - code written and unit-tested; engine not installed;
    never run on a real scanned document
[~] Form 20 - 2012 processed; 2017 unusable (corrupt OCR layer);
    2022 requires OCR
[~] Polling station enrichment - the PS List PDF's merged cells mean
    locality and building name are stored joined, not split
[~] Count semantics - source/valid/invalid/duplicate are queryable;
    stray and excluded are not
[~] config/sources.yaml - maintained as documentation, not loaded by code
```

### Intentionally excluded

```text
[x] CAPTCHA solving or bypass                    - never
[x] Authenticated ECI endpoints                  - out of scope
[x] Per-voter search enumeration                 - would be a brute-force search
[x] Voter contact data collection                - not in electoral rolls
[x] Contact enrichment or political targeting    - out of scope
[x] Directory listing circumvention              - 403 respected
```

### Blocked by the source

```text
[!] SIR 2026 roll PDFs          - CAPTCHA-gated
[!] ECI printing-publish API    - client-side encrypted parameters
[!] 2018-2023 roll editions     - CAPTCHA-gated
[!] ASD lists, BLO-BLA minutes  - CAPTCHA-gated
[!] Form 20 2017                - embedded OCR layer is corrupt
```

### Known defects awaiting fix

```text
[ ] F-1  Dashboard Form 20 / polling station join by part number  (HIGH)
[ ] F-2  duplicate_of_id never populated                          (MEDIUM)
[ ] F-3  stray / excluded not queryable                           (MEDIUM)
[ ] F-4  validation conflates duplicates with data errors         (MEDIUM)
[ ] F-5  election_results.ac_id never populated                   (MEDIUM)
[ ] F-6  AC page Form 20 table omits the election year            (LOW)
```

Full detail in `docs/PHASE1_DATA_INTEGRITY_AUDIT.md`.

---

## 30. How to run the POC

### Prerequisites

* **Python 3.11 or later** (developed and tested on 3.14.2)
* **Docker Desktop** running, for PostgreSQL

### Setup

```bash
cd "D:\AI-Centre\Political Application Research\POC"

# 1. Create a virtual environment and install dependencies
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
#  Linux/macOS: source .venv/bin/activate && pip install -r requirements.txt

# 2. Copy the configuration template
copy .env.example .env
#  Linux/macOS: cp .env.example .env

# 3. Start PostgreSQL
docker compose up -d
```

`docker compose up -d` starts PostgreSQL 17 on host port **5433**. The defaults in
`.env.example` already match it.

**Optional** — only needed for scanned documents:

```bash
# Windows: https://github.com/UB-Mannheim/tesseract/wiki  (install the 'hin' data)
# Debian:  sudo apt install tesseract-ocr tesseract-ocr-hin
# Then set TESSERACT_CMD in .env if it is not on PATH.
```

Everything else runs without it.

### Running

```bash
# Set the Python path
$env:PYTHONPATH="src"          # Windows PowerShell
export PYTHONPATH=src          # Linux/macOS

# 1. Create the database schema
python -m app init-db

# 2. Preview what the pipeline will do — no requests, no writes
python -m app pipeline --dry-run

# 3. Run the full pipeline
python -m app pipeline --parts 6,7,8,9,10

# 4. Review data quality
python -m app quality

# 5. Inspect a booth
python -m app analyze-booth --part 6 --edition ROLL-2003

# 6. Start the dashboard
python -m app serve
```

Then open **http://127.0.0.1:8000**.

**To stop the dashboard:** press **Ctrl+C** in the terminal running it.
**To stop PostgreSQL:** `docker compose down` (add `-v` to also delete the data).

### Expected output

**`pipeline`** prints a step-by-step table — state discovery, district discovery,
AC discovery, part discovery, PS-list enrichment, one row per ingested roll, part
mapping and Form 20 — each with a success flag, a record count and a description.
It ends with HTTP statistics and the database backend in use. Warnings (such as the
Form 20 candidate-column variation) are listed separately rather than hidden.

**`quality`** prints a coverage table (9 entity counts) and a quality table
(10 metrics including extraction rate and mean confidence).

**`analyze-booth`** prints the full booth report described in
[section 15](#15-part-6-case-study).

### Reproducing the current dataset

The exact command that produced the data described in this document:

```bash
python -m app init-db --drop
python -m app pipeline --parts 6,7,8,9,10
```

`--resume` is on by default, so already-downloaded PDFs are reused. Every database
write is an insert-or-update, so re-running produces the same result rather than
duplicating rows.

---

## 31. Troubleshooting

### `No module named app`

The application lives in `src/app`, which must be on the Python path.

```bash
$env:PYTHONPATH="src"          # Windows PowerShell
export PYTHONPATH=src          # Linux/macOS
```

Run commands from the project root. Confirm with `python -m app --help`.

### PostgreSQL is not running

The CLI reports `sqlite (FALLBACK — PostgreSQL was unreachable)` and the dashboard
shows the same. To fix:

```bash
docker ps                                    # is the container running?
docker compose up -d                         # start it
docker compose logs postgres                 # if it will not start
```

If Docker Desktop itself is not running, start it and wait for the engine to become
available, then retry. The container is named `uk-election-poc-db` and reports
`(healthy)` when ready.

### Port 5433 already in use

Another service holds the port. Change the host port in `docker-compose.yml` and
update `DATABASE_URL` in `.env` to match.

### OCR unavailable

```
OCR engine: UNAVAILABLE — tesseract is not installed or not on PATH.
```

This is expected in the default setup and affects only scanned documents. Install
Tesseract **with the `hin` language data** and, if it is not on PATH, set
`TESSERACT_CMD` in `.env`. Check with:

```bash
python -m app inspect-roll --roll <path-to-pdf>
```

### A source is CAPTCHA-gated

If you try to obtain a current-year roll, you will find the official portal
requires a CAPTCHA. **The POC does not bypass it, by design.** The legitimate
workflow is:

1. A person downloads the PDF from the official portal, solving the CAPTCHA.
2. Save it locally.
3. Run `python -m app extract-roll --roll <file>` — the extraction pipeline is
   independent of how the file was obtained.

### `UNSAFE_LEGACY_RENEGOTIATION_DISABLED`

Seen when contacting `election.uk.gov.in` with a plain HTTP client. The project's
own client already handles this by enabling `OP_LEGACY_SERVER_CONNECT` for that
host. If you see it, you are bypassing `src/app/http_client.py`.

### A downloaded file is not a PDF

The client rejects responses whose content is not a real PDF rather than saving an
HTML error page. This usually means the URL pattern changed. Re-verify with:

```bash
python scripts/inspect_eci_api.py
```

### Unicode errors in the terminal on Windows

Hindi text can fail to print in a console using a legacy code page:

```bash
$env:PYTHONIOENCODING="utf-8"
```

---

## 32. Testing

### Framework

**pytest 9.1.1**, configured in `pytest.ini` (which puts `src` on the path
automatically, so tests need no environment setup).

### Running

```bash
python -m pytest -q            # quiet
python -m pytest -v            # verbose
python -m pytest tests/test_parsers.py
```

### Current status

```
65 passed
```

### Breakdown

| File | Tests | Covers |
|---|---:|---|
| `tests/test_parsers.py` | 18 | PDF inspection, roll parsing, PS list parsing, Form 20 parsing, OCR availability |
| `tests/test_validation.py` | 14 | Gender/age normalization, EPIC formats, validation rules, duplicates, stray serials |
| `tests/test_text_encoding.py` | 13 | Kruti Dev conversion, conjunct repair, reordering, confidence scoring |
| `tests/test_database.py` | 12 | Idempotent writes, provenance persistence, part mapping, analytics, contact separation |
| `tests/test_sources.py` | 8 | URL construction, no hard-coded state code, CAPTCHA endpoints absent |
| **Total** | **65** | |

### What is notable about the tests

* **No test contacts the network.** The whole suite runs in about one second.
* **Parser tests use real documents.** `tests/fixtures/` holds slices of the actual
  published PDFs, so parsers are tested against true layouts. A synthetic
  scanned-page fixture exercises the text-vs-scan detection.
* **Some tests enforce policy, not just behaviour.** One fails if a CAPTCHA-gated
  endpoint is ever called; another fails if a contact column is ever added to the
  `electors` table; another fails if the state code is ever hard-coded.
* **Database tests use in-memory SQLite**, so they need no PostgreSQL. The schema
  is the same SQLAlchemy definition in both cases.

---

## 33. Data quality example table

All values verified from the current database.

| Metric | Part 6 | Part 10 |
|---|---:|---:|
| Pages in source document | 12 | 45 |
| Official count | 342 | 1,275 |
| Extracted | 342 | 1,276 |
| Difference | 0 | +1 |
| Serial gaps | 0 | 0 |
| Stray serials | none | `[2220]` |
| Duplicates flagged | 0 | 45 |
| Validation failures | 0 | 46 |
| Rows below 95% confidence | 6 | 42 |
| Mean confidence | 99.87% | 99.60% |
| Records with EPIC | 212 | 594 |
| EPIC coverage | 62.0% | 46.6% |
| Male | 175 | 717 |
| Female | 167 | 558 |
| Other / unknown | 0 | 1 |

### All five ingested parts

| Part | Pages | Extracted | Official | Diff | Gaps | Dups | Invalid | Low-conf | Confidence | EPIC |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 6 | 12 | 342 | 342 | 0 | 0 | 0 | 0 | 6 | 99.87% | 212 |
| 7 | 38 | 1,061 | 1,072 | −11 | 11 | 3 | 4 | 23 | 99.71% | 694 |
| 8 | 27 | 758 | 760 | −2 | 3 | 1 | 1 | 20 | 99.71% | 581 |
| 9 | 78 | 2,107 | 2,105 | +2 | 1 | 3 | 11 | 85 | 99.60% | 621 |
| 10 | 45 | 1,276 | 1,275 | +1 | 0 | 45 | 46 | 42 | 99.60% | 594 |
| **Total** | **200** | **5,544** | **5,554** | **−10** | **15** | **52** | **62** | **176** | **99.65%** | **2,702** |

### What this comparison demonstrates

**Quality varies enormously between booths from the same source, on the same day,
parsed by the same code.** Part 6 is flawless; Part 10 has 45 duplicate
registrations and a stray serial. A single headline number would hide that.

This is why the POC measures quality **per booth** and always states the basis of
the comparison:

* **Negative differences (Parts 7, 8) are gaps in the source** — serial numbers
  the document does not print, i.e. deleted voters. Verified by scanning every word
  of every page.
* **Positive differences (Parts 9, 10) are repeats in the source** — a serial
  printed twice, or a stray serial.
* **Confidence stays above 99.6% everywhere**, so the variation is not an
  extraction problem.
* **Extraction itself is complete**: for all five parts, the count of serial
  numbers in the raw PDF equals the count of rows in the database, exactly.

The lesson carried into future phases: *a booth that reports oddities is not
necessarily a booth that was extracted badly.* Distinguishing a source anomaly from
a parser anomaly requires provenance, and the POC keeps enough of it to do so.

---

## 34. Glossary

| Term | Meaning |
|---|---|
| **AC (Assembly Constituency)** | A state-legislature seat. Uttarakhand has 70. Identified by number, name and delimitation. |
| **API (Application Programming Interface)** | A machine-readable endpoint returning structured data, used here for the geography hierarchy. |
| **ASD (Absent, Shifted and Dead)** | A CEO Uttarakhand list of voters removed from a draft roll. Its download is CAPTCHA-gated and not used; one unprotected endpoint on the same page supplies the part list. |
| **BLO (Booth Level Officer)** | The official responsible for a booth's electoral roll. Referenced in research only; no BLO data is ingested. |
| **Booth** | Informal synonym for a Part and its polling station. Not a separate database entity in this POC. |
| **CAPTCHA** | A challenge designed to prove a human is present. Several official endpoints use one; this POC never solves or bypasses any. |
| **CEO (Chief Electoral Officer)** | The state-level election authority. CEO Uttarakhand is this POC's principal data source. |
| **Confidence** | A 0.0–1.0 score for how much of a record's text decoded cleanly. Measures text decoding, not correctness. |
| **Delimitation** | The boundary-drawing exercise that assigns constituency numbers. Stored as `current` or `2003`; two constituencies can share a number across delimitations. |
| **Duplicate** | A record repeating another — either the same serial twice, or the same person under two serials. Flagged, never deleted. |
| **DB (Database)** | The PostgreSQL store holding all extracted data. |
| **ECI (Election Commission of India)** | The national election authority; source of the state, district and constituency hierarchy. |
| **Edition** | Which roll revision a polling station belongs to — `ROLL-2003` or `SIR-2026`. |
| **Elector** | A registered voter. One row in the `electors` table. |
| **Electoral Roll** | The official published list of voters for a Part. One PDF document. |
| **EPIC (Electors Photo Identity Card)** | The voter ID number, e.g. `MYC0239293`. Optional in the source; 48.7% coverage in this data. |
| **ERO (Electoral Registration Officer)** | The officer who publishes an electoral roll. |
| **ERONET (Electoral Roll Management Network)** | The ECI's internal roll-management system. **Never accessed by this POC**; named only to state that boundary. |
| **Extraction** | Reading structured records out of a document. |
| **Form 20** | The statutory Final Result Sheet giving vote counts for every polling station after an election. |
| **HTTP / HTTPS** | The protocol used to fetch all documents and API responses. |
| **Kruti Dev** | A legacy Hindi font encoding that maps Devanagari letters onto Latin character codes. Used by two of this POC's source documents. |
| **OCR (Optical Character Recognition)** | Reading text from images. Implemented here but not demonstrated on a real scan. |
| **Official count** | The number of voters a document implies a booth has. In this POC it is *derived* from the highest serial number, not published. |
| **Part** | The unit of an electoral roll — the numbered voter list for one polling station. |
| **PC (Parliamentary Constituency)** | A national-parliament seat; each AC sits inside one. |
| **POC (Proof of Concept)** | A small working system built to prove an approach, not a product. |
| **Polling Station** | The physical place voters vote — school, panchayat building, community hall. |
| **PostgreSQL** | The relational database used as the system of record. |
| **Provenance** | The recorded chain from a stored record back to the document, page and URL it came from. |
| **Reph** | The Devanagari mark for a preceding "r" sound. Typed after its syllable in legacy encodings and must be moved during repair. |
| **SC / ST / GEN** | Scheduled Caste / Scheduled Tribe / General — constituency reservation categories. |
| **Serial gap** | A serial number absent from a roll between 1 and the official count; normally a deleted voter. |
| **SHA-256** | The checksum algorithm used to prove a downloaded PDF is unchanged. |
| **SIR (Special Intensive Revision)** | A full door-to-door revision of the electoral roll. The current edition is labelled `SIR-2026`. |
| **SQL (Structured Query Language)** | The language used to query the database. |
| **Stray serial** | A serial number far beyond a booth's normal numbering — a source printing error. One exists in this data (2220 in Part 10). |
| **Tendered vote** | A vote cast by someone who finds their vote already cast; recorded separately on Form 20. |
| **UI (User Interface)** | The web dashboard. |
| **Validation** | Checking a record against rules for age, gender, EPIC format and consistency. Failures are flagged, never deleted. |
| **Vidhan Sabha** | The State Legislative Assembly. Stored as election type `VIDHAN_SABHA`. |

---

## 35. Architecture summary

```text
                          OFFICIAL SOURCES
                                 │
                ┌────────────────┴────────────────┐
                │                                 │
         ECI GATEWAY API                  CEO UTTARAKHAND
    states / districts / ACs      parts JSON · PS List PDF
        (public, no auth)         Legacy Roll 2003 PDFs
                │                 village mapping · Form 20
                │                                 │
                └────────────────┬────────────────┘
                                 ↓
                    HTTP CLIENT  (retries, backoff,
                    politeness delay, legacy TLS,
                    content-type validation, audit log)
                                 ↓
                         DATA DISCOVERY
              state → district → AC → polling station
                                 ↓
                      DOCUMENT ACQUISITION
             download · SHA-256 checksum · resume · store
                                 ↓
                       PDF INSPECTION
                  ┌──────────────┴──────────────┐
                  ↓                             ↓
          TEXT EXTRACTION                   OCR PATH
       geometric column parsing      Tesseract hin+eng
       + legacy encoding repair     (implemented, engine
        (used for all data)          not installed here)
                  └──────────────┬──────────────┘
                                 ↓
                      RECORD EXTRACTION
              8 fields + full raw text preserved
                                 ↓
                    VALIDATION + QUALITY
        rules · duplicates · stray serials · confidence
                 (flag, never delete)
                                 ↓
                         POSTGRESQL
      states · districts · ACs · polling_stations
      electoral_rolls · electors · elections
      election_results · part_mapping · source_fetches
      contact_records (consent-gated, empty)
                                 ↓
                     ANALYTICS  (booth stats,
                     quality report, comparisons)
                                 ↓
              ┌──────────────────┴──────────────────┐
              ↓                                     ↓
         CLI  (11 commands)              DASHBOARD  (4 routes)
                                    / · /district · /ac · /station
```

### Design principles this reflects

1. **One source, one adapter.** Adding a state means adding an adapter, not
   rewriting the database or pipeline.
2. **Extraction is independent of acquisition.** A PDF obtained any legitimate way
   goes through the same parser.
3. **Nothing is deleted.** Problem records are flagged with a reason and kept.
4. **Raw data is preserved alongside cleaned data**, always.
5. **Provenance is mandatory**, not optional metadata.
6. **Time context is part of every identifier** — constituency number plus
   delimitation, part number plus edition.
7. **Protected sources are documented, not circumvented.**

---

*This document describes the POC as it exists on 2026-09-14. Related documents:
`README.md` (quick start), `docs/DATA_SOURCE_RESEARCH.md` (source selection),
`docs/API_FINDINGS.md` (endpoint reference), `docs/UTTARAKHAND_ROLL_FORMAT.md`
(document layouts), `docs/PRIVACY_AND_COMPLIANCE.md` (boundaries),
`docs/POC_FINAL_REPORT.md` (build report), `docs/PHASE1_DATA_INTEGRITY_AUDIT.md`
(independent audit).*
