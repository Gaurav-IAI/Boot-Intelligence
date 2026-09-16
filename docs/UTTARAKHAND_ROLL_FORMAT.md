# Uttarakhand electoral-roll and Form 20 document formats

Everything here was read off real PDFs downloaded from CEO Uttarakhand on
2026-09-14. Page coordinates are in PDF points (1/72"). Sample slices of each
document are committed under `tests/fixtures/` so the parsers can be tested
without the network.

---

## 1. Legacy Electoral Roll 2003 (the POC's elector source)

**Example:** `files/Roll2003/15-राजपुर/P0150006.pdf` — AC 15 Rajpur, part 6,
12 pages, 196 KB. Page size A4 portrait (595.5 x 842).

### 1.1 Header block

Repeated on every page. Page 1 carries two extra lines that the continuation
pages omit — which is why the table's top edge must be found per page, not fixed.

```
निर्वाचक नामावली-2003        S28 उत्तराँचल
पुनरीक्षण का वर्ष-2003        अर्हता तिथि :- 01.01.2003
भाग संख्या -6
विधान सभा निर्वाचन क्षेत्र की संख्या व नाम :- 15 - राजपुर
पुनरीक्षण का स्वरूप :- विशेष गहन पुनरीक्षण-2003              <- page 1 only
मतदेय स्थल की संख्या तथा नामः- 6 प्राथमिक स्कूल अस्थल        <- page 1 only
मतदेय स्थल में सम्मिलित ग्राम/गली/मुहल्ला आदि का नाम :-       <- page 1 only
    1 रैनी वाला, 2 बझैत, 3 अस्थल
```

Fields recovered: roll year, qualifying date, part number, AC number and name,
state code, polling-station number and name, the areas the station covers, and
the revision type.

### 1.2 Table columns

Eight columns, printed with a numbered header row `(1) … (8)`.

| # | Header | Meaning | x range (pt) | Notes |
|---|---|---|---|---|
| 1 | नि.क्र. संख्या | serial number | 40–88 | always present |
| 2 | मकान संख्या | house number | 88–125 | **empty throughout this edition** |
| 3 | निर्वाचक का नाम | elector name | 125–226 | may wrap to a second line |
| 4 | सम्बन्ध | relation type | 226–272 | पिता / माता / पति / अन्य |
| 5 | सम्बन्धी का नाम | relative name | 272–356 | may wrap |
| 6 | लिंग | gender | 356–396 | पुरुष / महिला |
| 7 | आयु | age | 396–428 | as at 01.01.2003 |
| 8 | फोटो पहचान पत्र संख्या | EPIC | 428–560 | **optional** |

Row pitch is ~19pt. The body is bounded:

* **top** — 12pt below the `(1) … (8)` marker row. On page 1 that row sits at
  y≈224; on continuation pages at y≈150. A fixed top edge silently drops the
  first four rows of every continuation page.
* **bottom** — 4pt above the footnote, identified by a word starting `कॉलम`
  **at x < 50**. The footnote sits left of the serial column (x=42.5 vs 53.7),
  so matching on text alone truncates pages where an elector name shares a
  prefix with a marker.

Footnote (not data):

```
कॉलम-4 सम्बन्ध कोड- पि.=पिता, मा.=माता, प.=पति, अ.=अन्य,
कॉलम-6 लिंग - पु. = पुरुष, म.= महिला,
कॉलम-7 आयु - 01.01.2003 को अनुमानित आयु
निर्वाचक रजिस्ट्रीकरण अधिकारी।
```

### 1.3 Why parsing is geometric, not line-based

Linear text extraction returns fields in order for simple rows, but longer names
wrap onto a second line and the `NULL` export artefact appears mid-cell:

```
1067  भारती उनियाल NULL   पति   शशि कान्त
                                 उनियाल NULL   महिला  26   NULL
```

A line-splitting parser mis-assigns those. The parser therefore buckets words
into columns by x and into rows by the y of the serial-number anchor.

### 1.4 Text encoding — conjuncts mangled into Latin-Extended

The embedded font's `ToUnicode` map sends conjunct glyphs to Latin Extended-B /
IPA codepoints. Base letters, digits and ASCII (EPIC numbers) are correct, so
**all numeric and EPIC data is unaffected**; only names need repair.

| Stray | Correct | Example |
|---|---|---|
| `ȅ` | त्त | उȅराँचल → उत्तराँचल |
| `Ŝ` | रु | पुŜष → पुरुष |
| `ƥ` | ख्य | संƥा → संख्या |
| `Ɨ` | क्ष | पुनरीƗण → पुनरीक्षण |
| `Ů` | प्र | Ůाथिमक → प्राथमिक |
| `˕` | स्थ | अ˕ल → अस्थल |
| `ˁ` | ष्ण | कृˁ → कृष्ण |
| `Ƚ` / `Ⱦ` | न्द / न्द्र | अरिवȽ → अरविन्द |
| `ɀ` / `ɾ` | न्ध / म्ब | सɾɀ → सम्बन्ध |
| `Ŋ` | र् (reph, typed **after** its cluster) | िनवाŊचक → निर्वाचक |
| `į` | ि (a bare short-i still needing reorder) | सįरता → सरिता |

Two reordering passes follow the substitution: the short-i vowel sign moves to
**after** its consonant cluster, and a reph moves to **before** it. The reph pass
must run exactly once — a second pass re-matches its own output and turns
निर्वाचन into र्निवाचन.

The full table is in `src/app/extraction/parsers/devanagari_fix.py`. Glyphs whose
intended conjunct is genuinely ambiguous are **left unmapped on purpose**: they
lower the row's confidence score instead of being guessed at.

Measured over three real parts (2,185 records): **mean confidence 99.7%**, with
~3% of rows carrying at least one still-unmapped glyph.

### 1.5 Quirks found in the real data

* **Literal `NULL`** is exported into empty text cells. Stripped, and the raw
  text is kept.
* **Stray combining marks** (U+0300–U+036F) attach to tokens — `62̻`, `पित̻`.
  A `str.isdigit()` test on the serial silently loses those rows, so the parser
  matches a leading run of digits instead.
* **Genuine serial gaps.** AC15 part 7 has no serials 354–357, 686, 757–758,
  822–823, 1007–1009 anywhere in the document: deleted electors, not extraction
  failures. Verified by scanning every word on every page.
* **A stray serial.** AC15 part 10 has 1,276 rows numbered 1–1275 and then
  prints **2220** on the final row. Taking the highest serial as the roll's
  length would invent ~900 missing electors, so such outliers are detected and
  excluded from the expected-count basis (and the exclusion is stated in the UI).
* **EPIC coverage is partial** — 62% in part 6. Absent EPICs are NULL.
* **House number is never populated** in this edition. The column exists and
  stays NULL rather than being back-filled.

### 1.6 Extraction results on the parts ingested

| Part | Pages | Rows | Expected | Diff | Gaps | Mean confidence |
|---|---|---|---|---|---|---|
| 6 | 12 | 342 | 342 | 0 | 0 | 99.87% |
| 7 | 38 | 1,061 | 1,072 | −11 | 11 (genuine) | 99.71% |
| 8 | 27 | 758 | 760 | −2 | 3 | 99.7% |
| 9 | — | 2,107 | 2,105 | +2 | 1 | 99.6% |
| 10 | 45 | 1,276 | 1,275 | +1 | 0 | 99.7% |

---

## 2. Polling Station List 2026

**Example:** `https://election.uk.gov.in/PSSIR2026/19.pdf` — 18 pages, 683 KB.

An Excel sheet printed to PDF. Five columns under a printed `1 2 3 4 5` header
row; column left-edges are read from that row per page because the widths differ
between ACs.

| # | Content |
|---|---|
| 1 | part / polling-station number |
| 2 | part name (village / locality) |
| 3 | polling station building |
| 4 | areas covered |
| 5 | who it serves (`सभी के लिये` = everyone) |

**Encoding: Kruti Dev**, e.g. `jktdh; izkFkfed fo|ky;` = `राजकीय प्राथमिक विद्यालय`.

**Known limitation.** The digits `1`–`5` also occur as part numbers in the body,
so the body's top edge must come from the header row's *y*, not from the last
word matching one of those digits. Even with that fixed, merged and wrapped
Excel cells mean the boundary between "locality" (col 2) and "building" (col 3)
is not reliably recoverable from geometry — so the two are **stored joined**
(`station_full`) rather than split into fields that would sometimes be wrong.
234 of ~209 printed stations are recovered for AC 19 (some parts appear on more
than one page; the richest row wins).

The **authoritative** part list is the JSON endpoint
`/asdlist/SearchAdsEpic/Parts?acNo=19`; this PDF is enrichment only.

---

## 3. Form 20 — Vidhan Sabha 2012

**Example:** `https://election.uk.gov.in/Vidhan_sabha2012/form20_2012_PDF/19-Raipur.pdf`
— 13 pages, landscape (1008 x 612), Kruti Dev text layer.

Row layout:

```
[serial] [station name] [votes c1] … [votes cN] [total valid] [tendered] [total]
   x≈80     x 100–250        x ≥ 250 …             x≈850      x≈884     x≈920
```

Header block on page 1 gives the AC (`…19 रायपुर…`) and
`निर्वाचकों की कुल संख्या (सर्विस निर्वाचकों सहित)` = 120,008.

**Names are printed above their serial**, so rows must be assembled by *nearest*
anchor; an "at-or-below" rule attributes each name's first line to the previous
booth.

**Candidate attribution is not possible.** The candidate names are rotated column
headers that PyMuPDF emits detached from their columns, and the number of vote
columns varies between booths (21/22/23) because a zero column is sometimes not
printed. The parser therefore stores the per-booth totals the document labels
directly and leaves `candidate_name` NULL rather than guessing an alignment.

**Self-check available.** The candidate votes must sum to the printed
`कुल विधिमान्य मत`. For AC 19 in 2012, **151 of 152 booths pass**; the one
failure is recorded as a warning rather than smoothed over.

### Other Form 20 editions

| Year | State | Why |
|---|---|---|
| 2022 | scanned images, 0 text characters | needs Devanagari OCR |
| 2017 | embedded OCR layer, corrupted beyond use | would need re-OCR from the images |
| 2012 | **text, parsed** | — |

This is the concrete evidence for the brief's warning that Form 20 layout and
quality differ per election year: three consecutive Vidhan Sabha elections
published in three materially different ways.
