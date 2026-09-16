# API findings — request/response reference

Companion to [DATA_SOURCE_RESEARCH.md](DATA_SOURCE_RESEARCH.md). This file
records the exact request shapes and response schemas observed on 2026-09-14, so
a future maintainer can tell a *changed* API from a *broken* client.

Captured samples: `research/samples/` (JSON) and `research/network/` (Playwright
traces and screenshots).

---

## 1. ECI gateway — `https://gateway-voters.eci.gov.in/api/v1`

### `GET /common/states` — 200, public

No headers required. Returns a bare JSON array (not an envelope).

```json
[{"stateId":27,"countryCd":"IN","stateCd":"S28","stateType":"ST",
  "stateName":"Uttarakhand","stateNameHindi":"उत्तराखंड",
  "effectiveFrom":"2022-01-01","effectiveTo":"2099-12-31","isActive":"Y",
  "createdBy":"CDAC","createdDttm":"2023-05-22T07:50:12.000+00:00",
  "modifiedBy":null,"modifiedDttm":null}]
```

36 rows. **Uttarakhand = `S28`, `stateId` 27.** Resolved by name at runtime.

### `GET /common/districts/{stateCd}` — 200, public

`/common/districts/S28` → 13 rows.

```json
[{"state":null,"districtNo":13,"districtValue":"Dehradun",
  "districtValueHindi":"देहरादून","districtCd":"S2813",
  "effectiveFrom":"...","isActive":"Y","expiresIn":null}]
```

Gotchas: `state` is `null`; `districtNo` is not a sort order; the code is
`{stateCd}{NN}`.

### `GET /common/acs/{districtCd}` — 200, public

`/common/acs/S2813` → 10 rows.

```json
[{"asmblyNo":19,"stateCd":"S28","districtCd":"S2813","asmblyName":"Raipur",
  "asmblyNameL1":"रायपुर","category":"GEN","pcNo":"1","acId":466,
  "effectiveFrom":"1640975400000","effectiveTo":"4102338600000","isActive":"Y",
  "languagePneumonicL1":"hi_in","prlmntNameL1":null}]
```

`effectiveFrom`/`To` are epoch-millisecond **strings**. `pcNo` is a string.
`acId` is an internal surrogate; `asmblyNo` is the official AC number.

### `GET /printing-publish/get-publish-eroll-type` — parameters encrypted

Plaintext probing walks through a chain of 400s naming the required parameters:

```
?                          -> 400 "Required request parameter 'stateCd' ... is not present"
?stateCd=S28               -> 400 "... 'year' ..."
?stateCd=S28&year=2026     -> 400 "... 'misKey' ..."
```

What the portal actually sends (captured with Playwright):

```
GET /printing-publish/get-publish-eroll-type
    ?stateCd=O04FZjEPYY4VzOGBoLBNhRe8MEhe
    &year=Ky0FaI4_H3wkXYVZvxpV11ojXQo
    &misKey=O1hlEV_wlB8Oe
```

Two page loads produced two different ciphertexts for the same `S28`/`2026`, so
the encryption is session- or nonce-keyed. Response (decrypted server-side):

```json
{"status":"Success","statusCode":200,"refId":null,
 "message":"Publish Eroll Data fetched Successfully!",
 "payload":[{"id":"S28-2026-DR","stateCd":"S28","year":2026,"revisionNo":1,
   "byElecAcList":null,"rollType":"DraftRoll","rollTypeRefId":"SIR-DraftRoll",
   "pdfGenType":"EROLLGEN","displayName":"SIR DraftRoll - 2026","publish":"Y",
   "publishDate":"2026-07-14","createdBy":"Backend","isActive":"Y",
   "nonPublishedAcs":null}],
 "file":null}
```

**Useful fact:** Uttarakhand's only published 2026 roll is *SIR DraftRoll – 2026*,
published **2026-07-14**. **Not automated** — reproducing the parameter
encryption would mean defeating a protection mechanism.

### `POST /printing-publish/get-ac-languages` — 200 for the portal

```json
{"status":"Success","statusCode":200,"payload":{"HIN":"HINDI"},"file":null}
```

**Uttarakhand rolls are published in Hindi only.** This is why the OCR
configuration must include `hin`; an English-only setup cannot read them.

### `POST /printing-publish/get-publish-part-list` — 200 for the portal

Returns the part list for the selected AC (the portal renders `1 - Sainj`,
`2 - Anu`, … for AC 15). A plaintext JSON POST from outside the browser returns
**400 with an empty body**, and Playwright reports no readable `post_data`,
consistent with the same client-side encryption. **Not automated** — the public
CEO Uttarakhand endpoint (section 2.1) provides the same data.

### CAPTCHA endpoints — never called

```
GET /captcha-service/getCaptcha/EROLL                  -> 200 (image)
GET /captcha-service/generateVoiceCaptcha/{captchaId}  -> 200 (audio, accessibility)
```

The e-roll page renders a required **`Captcha *`** field directly above
**"Download Selected PDFs"**. Automated roll-PDF download is therefore gated.
Screenshot: `research/network/04_eroll_filled.png`.

### `GET /authn-voter/validate-token` — 401 anonymous

```json
{"httpStatus":401,"error":"Unauthorized","message":"Please provide Auth token"}
```

Out of scope; never called by the pipeline.

### Observed portal call sequence

From `research/network/eci_parts_calls.json`, in order:

1. `GET authn-voter/validate-token` → 401 (anonymous)
2. `GET captcha-service/getCaptcha/EROLL` → 200
3. `GET printing-publish/get-publish-eroll-type?...` → 200 (encrypted params)
4. `GET captcha-service/generateVoiceCaptcha/{id}` → 200
5. `POST printing-publish/get-ac-languages` → 200
6. `POST printing-publish/get-publish-part-list` → 200

The form controls are `#stateCode`, `#revyear`, `#roleType`, `#district`,
`#constituency`, `#langCd`, plus the captcha input.

---

## 2. CEO Uttarakhand — `https://election.uk.gov.in`

> **TLS:** this host requires legacy renegotiation. Python/OpenSSL 3 fails with
> `[SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED]` unless the context sets
> `OP_LEGACY_SERVER_CONNECT (0x4)`. `curl` works unmodified. Handled in
> `src/app/http_client.py`.

### 2.1 `GET /asdlist/SearchAdsEpic/Parts?acNo={n}` — 200, public, no CAPTCHA

```json
[{"partNo":1,"partName":"Asthal","displayName":"1 - Asthal"},
 {"partNo":2,"partName":"Gujrara R.No. 1","displayName":"2 - Gujrara R.No. 1"}]
```

AC 19 → 234 parts, 20.8 KB. Names in English. This is the POC's
polling-station source.

Sibling operations on the same page **do** require a CAPTCHA and are not
implemented: `SearchAdsEpic/Captcha`, `SearchAdsEpic/SearchEpic`,
`SearchAdsEpic/DownloadAsdPdf`, `SearchAdsEpic/DownloadBlaMinutes`.

### 2.2 `GET /PSSIR2026/{acNumber}.pdf` — 200 `application/pdf`

Verified: AC 1 (209 KB), AC 19 (683 KB), AC 66 (5.6 MB).

The index page `PSSIR2026/polling_station_2026.html` embeds the complete
district → AC master as a `DATA_AC` JS literal (13 districts, 70 ACs) and builds
links as the plain relative path `{acNo}.pdf`.

### 2.3 Legacy Roll 2003 API — `/search2003uk/api/uklegacydata`

All GET, all public, all without a CAPTCHA.

```
GET /district-names
{"success":true,"data":[{"name":"देहरादून","number":"3"}],"totalResults":13,
 "timestamp":"2026-09-14T04:05:50.270596Z"}

GET /ac-names?districtName=देहरादून
{"success":true,"data":[{"name":"राजपुर","number":"15"}],"totalResults":9}

GET /part-names?acName=राजपुर
{"success":true,"data":[{"name":"अस्‍थल","number":"6"}],"totalResults":98}

GET /villages?searchText=अस्थल
{"success":true,"data":["3 अस्थल"],"totalResults":1}
```

Names must be sent as UTF-8; the district/AC names are Hindi and are the API's
own keys.

**Roll PDF** (path taken from the portal's own JS):

```
files/Roll2003/{ac:02d}-{acNameHindi}/P{ac:03d}{part:04d}.pdf
GET /search2003uk/api/uklegacydata/pdf?filePath=<url-encoded>
```

Verified 200 `application/pdf`: `15-राजपुर/P0150006.pdf` (196 KB),
`P0150007.pdf` (432 KB), `14-देहरादून/P0140001.pdf` (331 KB).

**Not implemented, on purpose:** `POST /search` (accepts `districtName`,
`acName`, `partName`, `epicNo`, `firstName`, `relativeFirstName`, `age`,
`gender`, `relationType`, `limit`), `GET /epic-no`, `POST /export`. These return
individual elector rows; enumerating booths through them is the brute-force
voter search the brief prohibits.

### 2.4 `GET /village-details?villageName={"N Name"}` — official part mapping

```json
{"success":true,"totalResults":3,"data":[
 {"district_no":"3","ac2003":"15 - राजपुर","part2003":"6 प्राथमिक स्कूल अस्थल",
  "gram":"1 रैनी वाला","acName":"राजपुर","districtName":"देहरादून",
  "ac2025":"19","acName2025":"रायपुर","part2025":"74",
  "part2025Names":"राजकीय प्राथमिक विद्यालय खैरी मानसिंह"}]}
```

Village-level and **one-to-many**: 2003 part 6 splits across 2025 parts 1 and 74.

### 2.5 Form 20 indexes

| Year | Index | Files |
|---|---|---|
| 2022 | `/CEO-Website/VidhanSabha2022/Form20/form20_2022.htm` | 70 links, `19-RAIPUR.pdf` |
| 2017 | `/CEO-Website/VidhanSabha2017/Form_20/form20_2017.htm` | 70 links, `19-Raipur.pdf` |
| 2012 | `/Vidhan_sabha2012/form20_2012_PDF/form20_2012.htm` | 70 links, `19-Raipur.pdf` |

All 200, no auth. Links are relative to the index's directory; some filenames
contain spaces and need encoding.

### 2.6 Blocked or refusing

```
GET /                        -> ASP.NET WebForms, Captcha1 in __VIEWSTATE
                                (24 roll editions, 2018 Draft .. 2023-07-01 Final)
GET /Pdf_Roll/               -> 403
GET /Pdf_Roll/PollingStation/-> 403
GET /PSSIR2026/              -> 403
```

Directory listings are not circumvented; the official HTML index pages are used.

---

## 3. Client requirements these findings imply

1. **Legacy TLS renegotiation** for `election.uk.gov.in` / `ceo.uk.gov.in`.
2. **Content-type validation** — several hosts answer 200 with an HTML error
   page. `get_pdf()` verifies the `%PDF` magic bytes before writing.
3. **UTF-8 query parameters** for the legacy roll API (Hindi names as keys).
4. **No assumption that a 200 means data** — `{"success": false}` envelopes
   occur on the legacy API and are raised as errors.
5. **Conservative pacing.** No rate limiting, throttling or 429 was observed,
   but total volume was low tens of requests. Default: 3 concurrent, ≥0.4 s
   apart, exponential backoff with jitter, retry only on 429/5xx.
