# Data Source Research — Uttarakhand Election Data POC

**Research performed:** 2026-09-14 (live probing, not from memory)
**Scope:** Uttarakhand (state code `S28`, discovered dynamically — never hard-coded)
**Method:** direct HTTP probing (`curl` / `httpx`), HTML + JS inspection, and Playwright
observation of the official portals' *own* network traffic.

> **Security posture.** No CAPTCHA was solved, read, submitted or bypassed. No
> authentication was defeated. No internal/ERONET endpoint was accessed. No voter
> search was brute-forced. Where a source is gated, it is recorded as
> **BLOCKED — manual only** and an official alternative is used instead.
> See [PRIVACY_AND_COMPLIANCE.md](PRIVACY_AND_COMPLIANCE.md).

---

## 0. Executive summary

| Question | Answer |
|---|---|
| Can we discover State -> District -> AC from an official API? | **Yes** — ECI gateway, public, no auth, no CAPTCHA |
| Can we discover Parts / Polling Stations? | **Yes** — CEO Uttarakhand JSON endpoint, public, no CAPTCHA |
| Can we download a **current** (SIR 2026) electoral-roll PDF automatically? | **No** — CAPTCHA-gated + encrypted request params. Blocked by design. |
| Can we download a **real official** electoral-roll PDF with elector records? | **Yes** — CEO Uttarakhand *Legacy Roll 2003* PDF service, public, no CAPTCHA |
| Can we get booth-level historical results? | **Yes** — Form 20 Vidhan Sabha 2012 (text PDF). 2017/2022 are scans -> OCR. |
| Is there an official old-part -> new-part mapping? | **Yes** — CEO Uttarakhand `village-details` endpoint |

The POC therefore runs **end-to-end on real official data** without touching any
protected mechanism.

---

## 1. ECI Gateway API — `gateway-voters.eci.gov.in`

**Owner:** Election Commission of India. **Base:** `https://gateway-voters.eci.gov.in/api/v1/`

### 1.1 `GET /common/states` — AUTOMATE

| Field | Value |
|---|---|
| URL | `https://gateway-voters.eci.gov.in/api/v1/common/states` |
| Method | GET |
| Auth | none |
| CAPTCHA | none |
| Response | JSON array, 36 objects |
| Fields | `stateId, countryCd, stateCd, stateType, stateName, stateNameHindi, effectiveFrom, effectiveTo, isActive, createdBy, createdDttm` |
| Bulk extraction | yes (single call returns all) |
| Rate limiting | none observed at our (very low) request volume |
| Fallback | CEO Uttarakhand site (state is implicit) |

**Uttarakhand discovered dynamically:** `stateCd = "S28"`, `stateId = 27`,
`stateType = "ST"`. Stored in `states.state_code`. Sample: `research/samples/states.json`.

### 1.2 `GET /common/districts/{stateCd}` — AUTOMATE

`.../common/districts/S28` -> 200, 13 districts.
Fields: `state, districtNo, districtValue, districtValueHindi, districtCd, isActive, ...`
District codes are `S28NN` (e.g. `S2813` = Dehradun). Sample: `research/samples/districts_S28.json`.

> Note: `districtNo` is **not** an alphabetical or geographic ordering, and the
> `stateCd` field inside each district row is `null` — the state must be carried
> from the request, not read from the response.

### 1.3 `GET /common/acs/{districtCd}` — AUTOMATE

`.../common/acs/S2813` -> 200, 10 ACs for Dehradun.
Fields: `asmblyNo, stateCd, districtCd, asmblyName, asmblyNameL1 (Hindi), category
(GEN/SC/ST), pcNo, acId, effectiveFrom/To (epoch ms strings), isActive`.
Sample: `research/samples/acs_S2813.json`.

`acId` is an internal ECI surrogate key; `asmblyNo` is the official AC number. Both stored.

### 1.4 `GET /printing-publish/get-publish-eroll-type` — PARTIAL

Plaintext probe returns `400`, demanding `stateCd`, then `year`, then `misKey`.

Playwright observation of the real portal shows the browser calls it as:

```
GET /printing-publish/get-publish-eroll-type
      ?stateCd=O04FZjEPYY4VzOGBoLBNhRe8MEhe
      &year=Ky0FaI4_H3wkXYVZvxpV11ojXQo
      &misKey=O1hlEV_wlB8Oe
```

**The query-parameter *values* are client-side encrypted** and rotate per page load
(two loads produced two different ciphertexts for the same `S28`/`2026`). The
decrypted response is plaintext and reveals the roll catalogue:

```json
{"status":"Success","statusCode":200,
 "payload":[{"id":"S28-2026-DR","stateCd":"S28","year":2026,"revisionNo":1,
             "rollType":"DraftRoll","rollTypeRefId":"SIR-DraftRoll",
             "pdfGenType":"EROLLGEN","displayName":"SIR DraftRoll - 2026",
             "publish":"Y","publishDate":"2026-07-14"}]}
```

**Verdict:** reproducing the parameter encryption would be reverse-engineering a
protection mechanism -> **not automated**. Recorded for documentation only.
Useful fact extracted: for Uttarakhand the only published 2026 roll is
**SIR DraftRoll – 2026, published 2026-07-14**.

### 1.5 `POST /printing-publish/get-ac-languages` and `POST /printing-publish/get-publish-part-list` — OBSERVED ONLY

Both return `200` when issued by the portal itself; a plaintext POST from outside
the browser returns `400` with an empty body. The request bodies are not plain JSON
(Playwright reports no readable `post_data`), consistent with the same client-side
encryption. `get-ac-languages` payload for Uttarakhand is `{"HIN":"HINDI"}` —
**Uttarakhand rolls are published in Hindi only**, which drives the OCR language choice.

**Verdict: not automated.** An equivalent, fully public part list is available from
CEO Uttarakhand (section 2.2) and is used instead.

### 1.6 `GET /captcha-service/getCaptcha/EROLL` — BLOCKED (by design)

The e-roll page loads a CAPTCHA (`getCaptcha/EROLL`, plus
`generateVoiceCaptcha/{id}` for accessibility) and renders a required
**`Captcha *`** field immediately above the **"Download Selected PDFs"** button.

**Automated download of the SIR 2026 roll PDF is therefore CAPTCHA-gated.**
We do not attempt it. Screenshot: `research/network/04_eroll_filled.png`.

### 1.7 `GET /authn-voter/validate-token` — NOT USED

Returns `401 "Please provide Auth token"` for anonymous users. Authenticated
ECI endpoints are out of scope and are never called.

---

## 2. CEO Uttarakhand — `ceo.uk.gov.in` and `election.uk.gov.in`

CEO Uttarakhand is treated as a **first-class source**, not a fallback. `ceo.uk.gov.in`
is the CMS shell; the machine-readable data lives on `election.uk.gov.in`.

> **TLS note (implementation-relevant):** `election.uk.gov.in` requires
> *legacy TLS renegotiation*. Stock Python/OpenSSL 3 refuses it with
> `[SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED]`. Our HTTP client sets
> `ssl.OP_LEGACY_SERVER_CONNECT (0x4)` for this host. `curl` works out of the box.

### 2.1 Polling Station List 2026 (PDF per AC) — AUTOMATE

| Field | Value |
|---|---|
| Index | `https://election.uk.gov.in/PSSIR2026/polling_station_2026.html` |
| PDF | `https://election.uk.gov.in/PSSIR2026/{acNumber}.pdf` |
| Method | GET, no auth, no CAPTCHA |
| Verified | AC 1 (209 KB), AC 19 (683 KB), AC 66 (5.6 MB) — all `application/pdf` |

The index page embeds the **complete district -> AC master for all 13 districts / 70 ACs**
as a `DATA_AC` JS literal, and builds PDF links as a plain relative path `{acNo}.pdf`.

Content: part number, polling station name, and the villages/streets covered.
**Text-based, but Hindi is in legacy Kruti Dev ASCII encoding** (see section 4).

### 2.2 SIR 2026 Part / Polling-Station list (JSON) — AUTOMATE

| Field | Value |
|---|---|
| URL | `https://election.uk.gov.in/asdlist/SearchAdsEpic/Parts?acNo={n}` |
| Method | GET, no auth, **no CAPTCHA** |
| Response | `[{"partNo":1,"partName":"Asthal","displayName":"1 - Asthal"}, ...]` |
| Verified | AC 19 -> 20.8 KB of parts, names in English |

This is the **preferred polling-station source**: authoritative, current (SIR 2026),
machine-readable, and free of the ECI encryption/CAPTCHA layer.

> The sibling operations on the same page — `SearchAdsEpic/SearchEpic`,
> `DownloadAsdPdf`, `DownloadBlaMinutes` — **do** require a CAPTCHA
> (`SearchAdsEpic/Captcha`). Those are **BLOCKED — manual only** and are not automated.

### 2.3 Legacy Electoral Roll 2003 — AUTOMATE *(primary elector source)*

Portal: `https://election.uk.gov.in/search2003uk/search.html`
API base: `https://election.uk.gov.in/search2003uk/api/uklegacydata`

| Endpoint | Method | CAPTCHA | Purpose |
|---|---|---|---|
| `/district-names` | GET | none | 13 districts (2003 delimitation), Hindi |
| `/ac-names?districtName=` | GET | none | ACs within a district |
| `/part-names?acName=` | GET | none | Parts/polling stations within an AC |
| `/village-details?villageName=` | GET | none | **official 2003 -> 2025 part mapping** |
| `/villages?searchText=` | GET | none | village lookup |
| `/pdf?filePath=` | GET | none | **the electoral-roll PDF itself** |
| `/search` | POST | none | per-elector search — *deliberately NOT used, see below* |
| `/epic-no`, `/export` | GET/POST | none | per-elector lookup / export — *not used* |

**Roll PDF path (from the portal's own JS):**

```
files/Roll2003/{acNo:02d}-{acNameHindi}/P{acNo:03d}{partNo:04d}.pdf
-> /search2003uk/api/uklegacydata/pdf?filePath=<url-encoded path>
```

Verified downloads (`application/pdf`):
`15-राजपुर/P0150006.pdf` (196 KB, 12 pp), `P0150007.pdf` (432 KB, 38 pp),
`14-देहरादून/P0140001.pdf` (331 KB).

**This is the POC's electoral-roll source:** a genuine, officially published
Uttarakhand electoral roll, with full elector records, obtainable by ordinary
HTTP GET with no protection mechanism involved.

> **Why `/search` is not used.** It accepts `firstName`, `age`, `gender`,
> `relativeFirstName`, ... and returns individual elector rows. Enumerating booths
> through it would be exactly the "brute-force voter search" the project brief
> prohibits. We take the **published PDF** instead — the same data, as the
> document the ERO actually published. `/search`, `/epic-no` and `/export`
> are documented here and left unimplemented.

### 2.4 Official part mapping 2003 -> 2025 — AUTOMATE

`GET /search2003uk/api/uklegacydata/village-details?villageName={"N Name"}` returns:

```json
{"district_no":"3","ac2003":"15 - राजपुर","part2003":"6 प्राथमिक स्कूल अस्थल",
 "gram":"1 रैनी वाला","districtName":"देहरादून",
 "ac2025":"19","acName2025":"रायपुर",
 "part2025":"74","part2025Names":"राजकीय प्राथमिक विद्यालय खैरी मानसिंह"}
```

This is an **official, publisher-supplied mapping** — it populates `part_mapping`
with `mapping_method = 'official_ceo_uk_village_mapping'`. Note it is **village-level
and one-to-many**: 2003 Part 6 splits across 2025 Parts 1 and 74. Part numbers are
**not** stable across delimitations, exactly as the brief warns.

### 2.5 Historic roll portal `election.uk.gov.in/` (2018–2023) — BLOCKED

ASP.NET WebForms app listing 24 roll editions (Draft/Final 2018 -> Final 01-07-2023).
`__VIEWSTATE` contains a `Captcha1` control. **CAPTCHA-gated -> manual only.**

### 2.6 Directory listing — REFUSED, not circumvented

`/Pdf_Roll/`, `/Pdf_Roll/PollingStation/`, `/PSSIR2026/` etc. all return **403**.
Indexes are obtained from the official HTML index pages instead.

---

## 3. Form 20 (booth-level results) — `election.uk.gov.in`

| Election | Index | AC 19 file | Extractability |
|---|---|---|---|
| Vidhan Sabha 2022 | `/CEO-Website/VidhanSabha2022/Form20/form20_2022.htm` | `19-RAIPUR.pdf` (6.4 MB, 17 pp) | **scanned image, 0 text chars -> OCR required** |
| Vidhan Sabha 2017 | `/CEO-Website/VidhanSabha2017/Form_20/form20_2017.htm` | `19-Raipur.pdf` (674 KB, 20 pp) | embedded OCR layer, **badly corrupted** -> unusable as-is |
| Vidhan Sabha 2012 | `/Vidhan_sabha2012/form20_2012_PDF/form20_2012.htm` | `19-Raipur.pdf` (556 KB, 13 pp) | **text-based** (Kruti Dev) — **parsed by this POC** |
| VS 2007 / 2002, LS 2004–2024 | CEO CMS `document-category/...` | S3 (`cdnbbsr.s3waas.gov.in`) | mixed, not covered in POC |

Each index lists all 70 ACs, so Form 20 is enumerable per election with no auth.

**Form 20 2012 structure (verified):** per polling station — station serial, station
name, then one integer per candidate, then `कुल वैध मत` (total valid), `निविदत्त` (tendered),
`योग` (total), `प्रतिक्षेपित` (rejected). Candidate names appear in a separate header
block. Confirms the brief's warning: **Form 20 layout differs per election year**.

---

## 4. Text-encoding findings (critical, and easy to get wrong)

Two *different* legacy Hindi encodings appear, and both must be handled:

**(a) Kruti Dev / Chanakya ASCII remapping** — Polling Station List 2026, Form 20 2012.
Extracted text looks like ASCII gibberish: `jktdh; izkFkfed fo|ky;` is actually
`राजकीय प्राथमिक विद्यालय`. Requires a Kruti Dev -> Unicode transliteration table
(`src/app/extraction/parsers/krutidev.py`).

**(b) Conjunct-glyph remapping into Latin-Extended** — Legacy Roll 2003 PDFs.
Text extracts as *real Unicode Devanagari* except that conjuncts are mapped to
Latin Extended / IPA codepoints: `ȅ`->`त्त`, `Ŝ`->`रु`, `ƥ`->`ख्य`, `Ɨ`->`क्ष`,
`Ů`->`प्र`, `˕`->`स्थ`, `ˁ`->`ष्ण`, `Ƚ`->`न्द`, `ɀ`->`न्ध`, `Ŋ`->`र्` (reph), ...
Handled by `devanagari_fix.py`, which also **scores confidence** from the share of
unmapped suspicious codepoints remaining.

**Both are lossy-by-nature.** The pipeline therefore always stores `raw_text`
alongside the normalised value.

---

## 5. Open-source projects reviewed

| Project | Reusable idea | Deliberately not reused |
|---|---|---|
| `krishnakr0119/Eroll_Downloader` | ECI state->district->AC->part enumeration order; per-part PDF naming; resume-by-existing-file | its CAPTCHA handling; hard-coded state codes |
| `in-rolls/electoral_rolls` | one downloader adapter per state/source — drove our `sources/<owner>/` adapter split; keep the raw PDF forever | scraping patterns aimed at now-dead endpoints |
| `Senavinod/voter-roll-ocr-pipeline` | detect text-vs-scan before OCR; page-level confidence; validate before insert | assumption that every roll needs OCR — Uttarakhand's 2003 rolls do not |
| `DeekshaR06/electoral-roll-ocr` | render-at-300dpi then OCR; structured spreadsheet output | English-only OCR assumption — Uttarakhand rolls are Hindi-only (section 1.5) |

No code was copied; only architecture and failure-mode lessons.

---

## 6. Source decision matrix

| Need | Chosen source | Why |
|---|---|---|
| State code | ECI `/common/states` | authoritative, dynamic |
| Districts | ECI `/common/districts/S28` | authoritative |
| ACs | ECI `/common/acs/{districtCd}` | authoritative, gives `category`/`pcNo` |
| Parts (current) | CEO UK `/asdlist/SearchAdsEpic/Parts` | no CAPTCHA, current SIR 2026 |
| PS name/address/areas | CEO UK `PSSIR2026/{ac}.pdf` | official published list |
| Electoral roll + electors | CEO UK Legacy Roll 2003 PDF service | **only CAPTCHA-free real roll** |
| Booth results | CEO UK Form 20 2012 | only text-extractable Form 20 |
| Part mapping | CEO UK `village-details` | official publisher mapping |
| SIR 2026 roll PDFs | *none — manual download* | CAPTCHA-gated; see section 1.6 |

---

## 7. Rate limiting & politeness

No rate limiting, throttling, WAF challenge or `429` was observed — but our total
volume was small (low tens of requests). The client therefore ships with a
**conservative default of 3 concurrent requests and a 0.4 s inter-request delay**,
both configurable (`--concurrency`). Retries use exponential backoff with jitter and
never retry a `4xx` other than `429`.
