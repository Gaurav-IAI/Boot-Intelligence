# Phase-1 Data Integrity Audit

**Date:** 2026-09-14
**Scope:** existing Uttarakhand POC — code, schema, database contents, tests, dashboard
**Method:** read-only. No code, schema or data was modified during this audit.
**Purpose:** establish whether the current data is trustworthy enough to proceed to
2025/2026 electoral-roll ingestion.

> **Verdict: PROCEED, with two fixes first.**
> The extraction layer is sound — verified record-for-record against the source PDFs.
> The defects found are in *interpretation and presentation*, not extraction. One of
> them (F-1) is actively producing wrong output today and gets worse in Phase 2.

---

## 0. Baseline

```
python -m pytest -q          ->  65 passed
python -m app quality        ->  see below
python -m app analyze-booth --part 6, --part 10
```

| Metric | Baseline |
|---|---|
| Tests | 65 passed |
| Database backend | postgresql |
| States / districts / ACs(current) / ACs(2003) | 1 / 13 / 10 / 1 |
| Polling stations | 239 |
| Electoral rolls | 5 |
| Electors | 5,544 |
| Part mappings | 17 |
| Election results | 152 |
| Official electors (sum) | 5,554 |
| Extracted electors (sum) | 5,544 |
| Extraction rate | 99.82% |
| Records passing validation | 98.88% |
| Duplicates flagged | 52 |
| Records with EPIC | 2,702 |
| Mean extraction confidence | 99.65% |

Both `analyze-booth` outputs reproduced the figures given in the audit brief exactly.

---

## 1. Findings register

| # | Finding | Severity | Status |
|---|---|---|---|
| **F-1** | Dashboard joins Form 20 (2012) results to SIR-2026 polling stations by bare part number. **Wrong for 148 of 152 booths (97.4%).** | **HIGH** | must fix before Phase 2 |
| **F-2** | `electors.duplicate_of_id` FK exists but is **never populated** (0 of 52). The duplicate link survives only as prose in `validation_notes`. | **MEDIUM** | fix before Phase 2 |
| **F-3** | `stray` and `excluded` are not queryable concepts — "stray" lives only in the free-text `official_count_basis`. | MEDIUM | recommend small schema addition |
| **F-4** | `is_valid=False` conflates "duplicate" with "data-quality failure". 45 of 46 Part-10 failures are *only* duplicates, so "98.88% passing validation" mostly measures **source** duplication, not extraction quality. | MEDIUM | recommend separating |
| **F-5** | `election_results.ac_id` FK is NULL for all 152 rows; AC identity rests on the bare `ac_number`. | MEDIUM | fix with F-1 |
| **F-6** | AC-page Form 20 table shows **no election year**. 2012 results appear under a current-AC heading. | LOW | one-line template fix |
| **F-7** | `official_elector_count` is a *derived* expected count, not a published official figure. Documented, but the column name invites misreading. | LOW | rename or annotate |
| **F-8** | Duplicate-serial note reads "first seen at row 637" where 637 is a *serial*, not a row index. | COSMETIC | wording |
| **F-9** | `source_fetches` is append-only, so the quality report's HTTP success rate accumulates across runs (22 rows after 2 runs). | COSMETIC | note only |

**No finding requires deleting any record.** Nothing was deleted.

---

## 2. Part 6 — **GREEN, confirmed**

Every claimed figure verified independently against both the database and the source PDF.

| Check | Result |
|---|---|
| Rows in DB | 342 |
| Serials 1–342, each exactly once | **True** |
| Missing serials | none |
| Duplicated serials | none |
| `is_valid=False` | 0 |
| `duplicate_kind` not null | 0 |
| Null serial / null name | 0 / 0 |
| Mean confidence | 0.9987 |
| Rows below 0.95 confidence | 6 |
| `official_elector_count` / `extracted_elector_count` | 342 / 342 |

**Nothing silently discarded.** Three independent counts agree exactly:

```
raw PDF serial-column tokens : 342
parser output                : 342
database rows                : 342
sorted(raw) == sorted(db)    : True
```

The 6 low-confidence rows are individually identifiable, and each is low for a
single, honest reason — one unmapped Devanagari conjunct glyph:

| Serial | Confidence | Name | Unmapped glyph |
|---|---|---|---|
| 122 | 0.9444 | सुƀी देवी | `ƀ` U+0180 |
| 147 | 0.8750 | सुƀा | `ƀ` U+0180 |
| 184 | 0.9167 | रुūमणी | `ū` U+016B |
| 185 | 0.9167 | अनीता (relative रुūमणी) | `ū` U+016B |
| 212 | 0.9444 | गुǭी देवी | `ǭ` U+01ED |
| 298 | 0.9444 | रूपसी बिʼ | `ʼ` U+02BC |

This is the confidence mechanism working as designed: ambiguous glyphs are left
unmapped and lower the score rather than being guessed.

**Classification: GREEN.**

---

## 3. Part 10 — deep audit, and serial 2220

### 3.1 The complete database record

```
id                    = 11088
electoral_roll_id     = 5          polling_station_id = 239
part_number           = 10         serial_number      = 2220
elector_name          = 'अनीता'     elector_name_raw   = 'अनीता'
relative_name         = 'राजेन्द्र'  relative_name_raw  = 'राजेȾ'
relative_type         = None       age  = 35      gender = 'F'
epic_number           = None       house_number       = None
source                = ceo_uk_legacy_roll_2003
source_document       = P0010.pdf  source_page        = 45
raw_text              = 'serial=2220 | name=अनीता | relative=राजेȾ | gender=मिहला | age=35'
parser_version        = roll_2003_geometric/1.0.0
extraction_method     = text       extraction_confidence = 1.0
is_valid              = False
validation_notes      = 'same name+relative+age as serial 220'
duplicate_kind        = 'same_name_relative_age'
duplicate_of_id       = None                      <-- see F-2
```

### 3.2 Is it a genuine elector, a parser artifact, or a source anomaly?

**It is a source anomaly — specifically, a duplicate re-print of elector 220.**

Evidence, in order of strength:

1. **The token is genuinely printed.** On page 45 the serial-column word is a single
   token `'2220'` at x=50.9–74.1, width 23.2pt — *identical geometry* to its
   neighbours `'1273'`, `'1274'`, `'1275'` (all x=50.9, width 23.2). It is not two
   tokens merged, and not a misread of `'220'` (which renders 17.4pt wide elsewhere).

2. **It duplicates serial 220 field-for-field.** Both rows extract to:
   `अनीता / राजेन्द्र / महिला / 35`, no EPIC, **and both are missing the relation
   column** — an unusual shared defect.

3. **The shared defect is in the source, not the parser.** Row 220 on page 9 prints:
   ```
   x= 53.7 '220'   x=131.9 'अनीता'   x=277.1 'राजेȾ'   x=360.2 'मिहला'   x=405.9 '35'
   ```
   There is no token at x≈230 (the relation column). Compare row 221, which has
   `x=230.0 'िपता'`. The relation is genuinely absent from the source for both rows.

4. **Serials 1–1275 are contiguous with zero gaps.** 2220 is the only value above
   1275. A roll that numbered 2,220 electors would not print exactly 1,276 rows.

### 3.3 Why the parser extracted it

Correctly. The parser anchors rows on numeric tokens in the serial column; `2220`
is such a token, with a complete row beside it. Rejecting it would require the
parser to decide a printed row is not real — precisely the silent discard the POC
is designed not to do.

### 3.4 Why the quality layer calls it stray

`find_serial_outliers()` (`src/app/services/validation.py`) flags a sparse tail
after a jump greater than 100 when that tail is ≤ max(3, 1% of rows). Here the tail
is `[2220]` after a jump of 945 — 1 row out of 1,276. It is excluded from the
*expected-count basis* only, so the booth reports 1,275 expected rather than
fabricating ~945 phantom missing electors.

### 3.5 Should it stay?

**Yes — retained, flagged, never deleted.** It already is:
`is_valid=False`, `duplicate_kind='same_name_relative_age'`, full provenance
(page 45, raw text, confidence, parser version, source URL) intact.

Deleting it to make the count read 1275 would destroy evidence of a real defect in
a published electoral roll. The only gap is F-2/F-3: the *reason* for exclusion is
prose, not a queryable field.

---

## 4. The 45 duplicates — classification

All 45 were resolved to their original row and compared field by field.

```
A. Exact duplicate of another extracted row : 23
B. Same serial repeated                     :  0
C. Same person under different serials      : 45   (the umbrella; A and D..F are subsets)
D. Duplicate created by parser/OCR          :  0   (ruled out — see below)
E. Legitimate source-level duplicate        : 45
F. Unknown                                  :  0
```

**Category A (23)** — raw text identical except the serial number.
**Category C, non-exact (22)** — same person, minor source-side differences:

| Difference | Count | Example pairs |
|---|---|---|
| whitespace only in name/relative | 3 | `बुȠिसंह` vs `बुȠ िसंह` (68→332) |
| one side carries an EPIC, other does not | 10 | 189→458, 218→678 |
| relation term present on one side only | 11 | 12→594 (`िपता` vs `-`) |

**Category D ruled out.** For a sample of pairs, both rows were located in the raw
PDF and confirmed to be separate printed rows on different pages with different
printed serials:

```
serial  39 (page  2)  and  serial 304 (page 11)  -> both printed, same person
serial 166 (page  7)  and  serial 435 (page 16)  -> identical ignoring serial
serial 189 (page  7)  and  serial 458 (page 17)  -> 458 additionally carries an EPIC
serial  12 (page  1)  and  serial 594 (page 21)  -> 594 has '-' where 12 has 'िपता'
```

Further, **no serial number is repeated in Part 10** and every duplicate sits on a
*later* page than its original (45/45). A parser that duplicated rows would produce
repeated serials or same-page collisions; neither occurs.

**Critically: no pair has two *different* EPICs.** In all 10 EPIC-differing pairs
one side is NULL. Two distinct people would be expected to hold distinct EPICs.

**Systematic offsets confirm block re-registration in the source:**

```
offset +269 : 9 pairs      offset +461 : 8 pairs      offset +637 : 6 pairs
offset +262..265 : 13 pairs                           others : 9 pairs (incl. +2000 = serial 2220)
```

A contiguous block of the roll (roughly serials 39–203) reappears at ~304–471; two
further blocks reappear at +461 and +637. This is a **source-level duplicate
registration problem in the 2003 roll**, faithfully reproduced by the extractor —
exactly the kind of thing ASD/de-duplication processes exist to find.

**No duplicate was deleted. The raw extraction remains complete and auditable.**

---

## 5. The 46 validation failures

Grouped by the rule that fired:

```
Invalid/missing serial   :  0
Invalid age              :  0
Invalid gender           :  1     (serial 725)
Malformed EPIC           :  0
Missing required field   :  0
Duplicate                : 45
Other                    :  0
```

Overlap with the duplicate set:

```
Total validation failures            : 46
Failures caused by duplicate flagging : 45
Independent validation failures       :  1      (serial 725, gender)
Duplicate-flagged but still valid     :  0
```

**This is F-4.** 45 of 46 "validation failures" are not extraction defects at all —
they are duplicates present in the published source. Reporting them under a single
`is_valid=False` flag makes "98.88% passing validation" read as an extraction-quality
number when it is mostly a source-quality number. The two should be separated.

---

## 6. The Other/Unknown gender record — serial 725

```
serial 725, page 26
name      : लक्ष्मी राणा
relation  : FATHER          (source column 4 = 'िपता')
relative  : कुलदीप सिंह राणा
gender    : UNKNOWN         (source column 6 = 'पित')
age       : 45
raw_text  : serial=725 | name=लƘी राणा | relation=िपता | relative=कुलदीप िसंह राणा | gender=पित | age=45
```

**Cause: the source printed a relation term in the gender column.** `पति`
("husband") is a column-4 value; column 6 must contain `पुरुष` or `महिला`.

Verified directly against page 26 of the source PDF:

```
serial 724:  RELATION x=230.0 'िपता'   GENDER x=362.9 'पुŜष'    <- normal
serial 725:  RELATION x=230.0 'िपता'   GENDER x=365.1 'पित'     <- relation term in gender column
serial 726:  RELATION x=230.0 'िपता'   GENDER x=360.2 'मिहला'   <- normal
```

x=365.1 sits squarely inside the gender column (356–396), so this is **not** a
parser column-misalignment. The word `पति` is genuinely printed there.

**Decision: leave as UNKNOWN. No correction applied.**

Circumstantial evidence points to female — the name लक्ष्मी, and the relative
कुलदीप सिंह राणा is serial 724 (male, 47), so 725 is very likely his wife, which
would make the intended row *relation = पति, gender = महिला*. But that is an
**inference from context, not a statement in the source**. The gender column does
not say महिला. Per the audit instruction, the record stays UNKNOWN and flagged.

*Recommendation (not applied):* have the gender normaliser emit a specific note —
`gender column contains a relation term 'पति' (source column-content error)` —
instead of the generic `not recognised`. That distinguishes a source defect from an
unreadable glyph without changing the stored value.

---

## 7. Count semantics

| Concept | Queryable today? | How |
|---|---|---|
| `source_records` | yes | `electoral_rolls.extracted_elector_count` (= rows in DB, verified equal) |
| `valid_records` | yes | `electors.is_valid = true` |
| `invalid_records` | yes | `electors.is_valid = false` |
| `duplicate_records` | yes | `electors.duplicate_kind is not null` |
| `excluded_records` | **no** | no column; nothing is excluded |
| `stray_records` | **no** | only as free text inside `official_count_basis` |

Verified for Part 10:

```
rows physically in DB        : 1276
extracted_elector_count      : 1276     <- agrees, no conflation
official_elector_count       : 1275
official_count_basis         : "highest serial number printed, excluding 1 stray serial(s) [2220]..."
is_valid = true              : 1230
is_valid = false             :   46
duplicate_kind not null      :   45
duplicate_of_id populated    :    0 / 45        <- F-2
```

**Conflations identified:**

* **F-7** — `official_elector_count` is *derived* (highest non-stray serial), not a
  published official figure. The 2003 roll prints no summary total. The basis string
  and the docs say this, but the column name does not.
* **F-4** — `is_valid` mixes duplicate-ness with data-quality.
* **F-3** — "stray" is a real analytical concept with no home in the schema; the
  2220 record is only discoverable as a duplicate, not as a stray.
* Extraction rate is computed as `extracted / official`, which for Part 10 yields
  **100.08%** — a rate above 100% is a symptom of dividing a *row count* by a
  *serial-derived expectation*. Not wrong, but it should be labelled as such.

### Recommended minimal schema change (not applied)

Two nullable columns on `electors`, no data migration required:

```sql
ALTER TABLE electors ADD COLUMN record_status   varchar(24) DEFAULT 'active';
-- values: active | duplicate | stray | excluded
ALTER TABLE electors ADD COLUMN exclusion_reason text;
```

plus populating the existing `duplicate_of_id` (F-2). Rationale: it makes
`source_records / valid_records / duplicate_records / stray_records / excluded_records`
each independently queryable without deleting anything, and separates
"this row failed a data check" from "this row is a duplicate of another".

I have **not** made this change — §7 of the brief asks for the explanation first.

---

## 8. The 239 vs 234 polling stations

**Fully explained. All 239 are legitimate. Nothing to delete.**

```
edition=SIR-2026   AC=19 Raipur  delim=current  src=ceo_uk_ps_list_2026        n=234  parts 1-234
edition=ROLL-2003  AC=15 राजपुर   delim=2003     src=ceo_uk_legacy_roll_2003    n=  5  parts 6-10
                                                                        TOTAL = 239
```

The five extra rows are the **2003-delimitation polling stations** that the five
ingested rolls belong to (ids 235–239, parts 6–10, each with exactly 1 roll):

| id | part | AC | station |
|---|---|---|---|
| 235 | 6 | 15 (2003) | प्राथमिक स्कूल अस्थल |
| 236 | 7 | 15 (2003) | प्राथमिक स्कूल गुजराडा |
| 237 | 8 | 15 (2003) | प्राथमिक स्कूल डांडा खुदानेवाला |
| 238 | 9 | 15 (2003) | जूनियर हाईस्कूल क0न01 मंगलूवाला |
| 239 | 10 | 15 (2003) | जूनियर हाईस्कूल क0न0 2 मंगलूवाला |

They are **not** historical duplicates of current stations, not test artifacts, and
not another source's copy of the same stations — they are a different AC in a
different delimitation.

**The discrepancy is a reporting-label problem, not a data problem:** the pipeline
step prints "234 polling stations (SIR 2026)" (edition-scoped) while `app quality`
prints a raw table count labelled "polling stations" (all editions). Recommend
splitting that row by edition in the quality report.

> **Phase-2 warning.** Part numbers 6–10 now exist in *both* editions. Today
> `analyze-booth --part 6` is unambiguous only because SIR-2026 stations have no
> rolls. Once 2026 rolls are ingested it will match two booths. `--edition` already
> exists; it should become required (or default to the newest) rather than optional.

---

## 9. Current vs historical AC identity

**The schema already handles this correctly.** No change recommended at the
hierarchy level.

```
assembly_constituencies : UNIQUE(district_id, ac_number, delimitation)
polling_stations        : UNIQUE(ac_id, part_number, edition)
electoral_rolls         : UNIQUE(polling_station_id, roll_year, roll_type, language)
```

The database contains live proof that the concern is real:

| id | ac_number | name | delimitation |
|---|---|---|---|
| 11 | **15** | राजपुर (Rajpur) | 2003 |
| 1 | **15** | Chakrata | current |

**AC 15 means Rajpur in 2003 and Chakrata today.** They are separate rows and
cannot collide, because `delimitation` is part of the uniqueness key. Roll year and
type live on `electoral_rolls`; `part_mapping` records `from_edition`/`to_edition`
explicitly with a method and confidence.

**However — one table does not carry this discipline (F-5):**

```
election_results : 152 rows, ac_id populated = 0, ac_id NULL = 152
                   no delimitation/edition column
                   AC identity = election_id (year) + bare ac_number
```

`ElectionResult.ac_id` exists as a FK but is never written, so results are linked to
a constituency only by an integer that is not stable across delimitations. Today
nothing breaks (only `ac_number=19` results exist, and AC 19 is current), but this
is the mechanism behind F-1.

**Smallest safe improvement:** populate `election_results.ac_id` at ingest time from
the AC row matching `(ac_number, delimitation appropriate to the election year)`,
and filter by `ac_id` rather than `ac_number` in the web layer. No new columns
needed — the field already exists.

---

## 10. Part 10 count handling — verified against the source

**The current interpretation is correct**, and was re-derived independently rather
than assumed.

An independent recount straight from the PDFs (re-implementing the page-bounds and
serial-anchor logic, not calling the parser) gives, for every part:

| Part | Raw PDF serial tokens | DB rows | Identical multiset | Repeated serials in source | Missing below expected | Max serial |
|---|---|---|---|---|---|---|
| 6 | 342 | 342 | **yes** | none | 0 | 342 |
| 7 | 1,061 | 1,061 | **yes** | none | 11 | 1,072 |
| 8 | 758 | 758 | **yes** | **637** | 3 | 760 |
| 9 | 2,107 | 2,107 | **yes** | **1794, 1859, 2018** | 1 | 2,105 |
| 10 | 1,276 | 1,276 | **yes** | none | **0** | 2,220 |

**The parser skips nothing and duplicates nothing, in any part.**

For Part 10 specifically:

* serials **1–1275 are contiguous with zero gaps** — strong evidence that 1,275 is
  the roll's intended length;
* `2220` is the only value above 1275, and it duplicates elector 220;
* therefore **1,275 is the intended official count** and **2220 is a source
  printing/data-entry anomaly**, as currently recorded.

**Other serial anomalies found (previously undocumented):**

* **Part 8** prints serial **637 twice** — correctly caught as `same_part_and_serial`.
* **Part 9** prints serials **1794, 1859, 2018** twice each — all three caught as
  `same_part_and_serial`.

These account for the +2 on Part 9 and confirm the duplicate-serial rule works. They
should be added to `docs/UTTARAKHAND_ROLL_FORMAT.md` alongside the Part-10 note.

A more precise phrasing for Part 10 would be: *1,276 rows printed; 1,275 unique
electors; 1 anomalous re-print* — rather than "extracted 1276 vs official 1275, +1".

---

## 11. Provenance audit — **PASS, zero gaps**

Across all **5,544** elector records:

| Field | Nulls |
|---|---|
| source, source_url, source_document, source_page | 0, 0, 0, 0 |
| serial_number, part_number | 0, 0 |
| raw_text | 0 |
| parser_version, extraction_method, extraction_confidence | 0, 0, 0 |
| extracted_at, is_valid | 0, 0 |
| electoral_roll_id, polling_station_id | 0, 0 |

Referential integrity: **0** electors with a missing roll; **0** with a missing station.

Roll-level provenance complete for all 5 rolls: `pdf_url`, `local_file_path`,
`checksum_sha256`, `file_size_bytes`, `page_count`, `is_text_pdf`,
`extraction_method`, `parser_version`, `roll_year`, `roll_type`,
`official_elector_count`, `official_count_basis`, `extracted_elector_count` —
**no nulls in any**.

**Raw data is never overwritten.** `elector_name_raw` is retained for 5,544/5,544
records; 3,303 rows have a repaired name that differs from the raw, and in every
case *both* are stored. `raw_text` is empty for 0 records.

`source_fetches` provides the fetch audit trail (F-9: append-only, so counts
accumulate across pipeline runs — correct as an audit log, but the quality report's
"HTTP success rate" should be scoped per run).

---

## 12. Dashboard audit

All routes return HTTP 200 with no server errors:

```
/                    200      /station/235          200      /ac/5        200
/district/3          200      /station/235?page=2   200      /ac/11       200
/district/13         200      /station/25           200      /station/1   200
```

**Displayed correctly** on `/station/235`: AC, part, polling station, roll year
(`Booth overview — Final 2003`), elector count, male/female, age distribution, EPIC
count, extraction confidence, serial range/gaps, validation and duplicate counts,
source document + URL, and the official 2003→2025 part mapping. Pagination works
(342 records, 4 pages).

**Historical is not mislabelled as current** at the hierarchy level. `/district/13`
renders both AC 15 rows with a Delimitation column distinguishing them:

```
15  राजपुर      राजपुर    —    2003     5 stations
15  Chakrata   चकराता   ST    current  0 stations
```

`/station/25` and `/station/1` correctly show `edition SIR-2026` with 0 elector
records (no current roll ingested).

### F-1 — the one serious defect

`/station/1` is a **SIR-2026** polling station. It displays a **Form 20 booth
result** obtained by:

```python
# src/app/web/app.py
select(ElectionResult).where(ElectionResult.part_number == st.part_number,
                             ElectionResult.ac_number == ac.ac_number)
```

That matches on part number alone, across a 14-year gap — the exact assumption the
project's own documentation forbids and `part_mapping` exists to avoid.

**Measured impact.** Comparing the SIR-2026 station name against the Form 20 2012
station name for the 152 overlapping part numbers in AC 19, ignoring
building-type words and matching on place tokens:

```
overlapping part numbers : 152
place-token agreement    :   4  ( 2.6%)
place-token disagreement : 148  (97.4%)
```

| Part | SIR-2026 station | Form 20 2012 station | Same place? |
|---|---|---|---|
| 1 | राजकीय प्राथमिक विद्यालय — अस्थल | रा0प्रा0 विद्यालय अस्थल | yes |
| 2 | बहादुर सिंह नेगी … गुजराडा | बहादुर सिंह नेगी इ0का0 क0नं0 1 गुजराडा | yes |
| 5 | नरेन्द्र रोज लीन पब्लिक स्कूल — सौंधोवाली | रा0प्रा0 विद्यालय डाडा खुदानेवाला | **no** |
| 6 | नागल राजकीय प्राथमिक विद्यालय — तरला नागल | पंचायतघर डांडा लखौण्ड | **no** |
| 7 | नागल राजकीय प्राथमिक विद्यालय क-न-2 | रा0इ0का0 क0नं0 1 नालापानी | **no** |

Only the first three parts align — numbering diverges immediately after. So the
station page attributes 2012 votes to the **wrong polling station for roughly 97% of
booths**. The Year column ("2012") is shown, which limits the damage to a reader who
checks it, but the *association itself* is unsupported.

**Recommended fix (smallest safe):** drop the part-number join on the station page.
Show a Form 20 result on a polling station only when the result's election year
corresponds to that station's edition, or when a `part_mapping` row links them.
Otherwise present Form 20 only on the AC page, where the part number is simply a row
label from that document.

### F-6 — AC page omits the year

`/ac/5` renders `Booth-level results — Form 20` with columns
`Part | Polling station | Valid votes | Tendered | Total | Page` — **no year**, on a
page headed by the *current* AC. The station page does include a Year column. A
one-line template addition fixes it.

---

## 13. Recommendation

**Proceed to 2025/2026 ingestion after fixing F-1 and F-2.**

The extraction layer is verified sound: raw PDF token counts equal database row
counts exactly for all five parts, provenance is complete with zero nulls, nothing
has been silently discarded, and every anomaly found (stray serial 2220, 52
duplicates, the serial-725 gender defect, repeated serials in parts 8 and 9) traces
to the **published source**, not to this code.

Before Phase 2, in order:

1. **F-1** — remove the part-number join between Form 20 and polling stations.
   This is producing wrong output *today*, and Phase 2 adds a second edition that
   will collide on part numbers 1–234.
2. **F-2** — populate `duplicate_of_id`; `pipeline.py` currently drops
   `duplicate_of_serial` when writing rows.
3. **F-5** — populate `election_results.ac_id`; filter by it in the web layer.
4. **F-3 / F-4** — add `record_status` + `exclusion_reason`, and split
   "duplicate" from "data-quality failure" in the validation rate.
5. **F-6, F-7, F-8, F-9** — labelling and wording.
6. Make `--edition` required (or newest-default) on `analyze-booth`, and split the
   quality report's polling-station count by edition.
7. Document the Part-8 and Part-9 repeated serials in
   `docs/UTTARAKHAND_ROLL_FORMAT.md`.

None of these require reprocessing the existing extraction.
