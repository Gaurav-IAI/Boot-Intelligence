"""Adapters for CEO Uttarakhand (`election.uk.gov.in`).

Three independent public services live here:

  * `SirPartsClient`      — SIR 2026 polling-station list (JSON, no CAPTCHA)
  * `LegacyRoll2003Client`— 2003 electoral roll: hierarchy, part mapping, roll PDFs
  * `Form20Client`        — Form 20 booth-level results indexes and PDFs

The CAPTCHA-gated operations on the same hosts (`SearchAdsEpic/SearchEpic`,
`DownloadAsdPdf`, `DownloadBlaMinutes`, the ASP.NET roll portal) are intentionally
absent. See docs/DATA_SOURCE_RESEARCH.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from ...http_client import HttpClient

HOST = "https://election.uk.gov.in"
SIR_PARTS_URL = f"{HOST}/asdlist/SearchAdsEpic/Parts"
PS_LIST_2026_INDEX = f"{HOST}/PSSIR2026/polling_station_2026.html"
PS_LIST_2026_PDF = f"{HOST}/PSSIR2026/{{ac_number}}.pdf"
# Polling Station List 2024 (Lok Sabha 2024), linked from the CEO home menu. Scanned PDFs;
# its part numbers match the 2025 roll used by the official 2003 -> 2025 village mapping.
PS_LIST_2024_INDEX = f"{HOST}/Pdf_Roll/PollingStation/PS-2024LS/Uttranchal_pdf_page.htm"
PS_LIST_2024_PDF = f"{HOST}/Pdf_Roll/PollingStation/PS-2024LS/{{ac_number}}.pdf"
LEGACY_API = f"{HOST}/search2003uk/api/uklegacydata"

SOURCE_SIR_PARTS = "ceo_uk_sir2026_parts"
SOURCE_PS_LIST = "ceo_uk_ps_list_2026"
SOURCE_PS_LIST_2024 = "ceo_uk_ps_list_2024"
SOURCE_LEGACY_2003 = "ceo_uk_legacy_roll_2003"
SOURCE_FORM20 = "ceo_uk_form20"


# --------------------------------------------------------------------------
# SIR 2026 polling stations
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PartRef:
    part_number: int
    part_name: str
    display_name: str | None


class SirPartsClient:
    """Current (SIR 2026) part / polling-station list for an AC."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def parts_url(self, ac_number: int) -> str:
        return f"{SIR_PARTS_URL}?acNo={ac_number}"

    def list_parts(self, ac_number: int) -> list[PartRef]:
        data = self.http.get_json(
            SIR_PARTS_URL,
            params={"acNo": ac_number},
            headers={"Referer": f"{HOST}/asdlist"},
        )
        out = []
        for row in data:
            no = row.get("partNo")
            if no is None:
                continue
            out.append(PartRef(
                part_number=int(no),
                part_name=(row.get("partName") or "").strip(),
                display_name=(row.get("displayName") or None),
            ))
        return sorted(out, key=lambda p: p.part_number)

    def ps_list_pdf_url(self, ac_number: int) -> str:
        return PS_LIST_2026_PDF.format(ac_number=ac_number)

    def fetch_ps_list_pdf(self, ac_number: int) -> bytes:
        return self.http.get_pdf(self.ps_list_pdf_url(ac_number))

    def ps_list_2024_pdf_url(self, ac_number: int) -> str:
        return PS_LIST_2024_PDF.format(ac_number=ac_number)

    def fetch_ps_list_2024_pdf(self, ac_number: int) -> bytes:
        return self.http.get_pdf(self.ps_list_2024_pdf_url(ac_number))

    def district_ac_master(self) -> dict[str, list[dict[str, Any]]]:
        """District -> ACs, read from the official index page's embedded master."""
        html = self.http.get_text(PS_LIST_2026_INDEX)
        m = re.search(r"const\s+DATA_AC\s*=\s*(\{.*?\});", html, re.S)
        if not m:
            raise ValueError("DATA_AC master not found on the PS-list index page")
        import json
        return json.loads(m.group(1))


# --------------------------------------------------------------------------
# Legacy Electoral Roll 2003
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class NamedRef:
    number: int | None
    name: str


@dataclass(frozen=True)
class VillageMapping:
    district_name: str | None
    ac2003_number: int | None
    ac2003_name: str | None
    part2003_number: int | None
    part2003_name: str | None
    village: str | None
    ac2025_number: int | None
    ac2025_name: str | None
    part2025_number: int | None
    part2025_name: str | None
    raw: dict[str, Any]


def _lead_int(s: str | None) -> int | None:
    if not s:
        return None
    m = re.match(r"\s*(\d+)", str(s))
    return int(m.group(1)) if m else None


class LegacyRoll2003Client:
    """CEO Uttarakhand's published 2003 electoral roll.

    This is the POC's elector source: the roll PDFs are served by plain GET with
    no CAPTCHA and no authentication. The per-elector `/search`, `/epic-no` and
    `/export` operations on the same API are deliberately NOT wrapped — see the
    research doc for why.
    """

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def _get(self, path: str, **params: Any) -> list[Any]:
        payload = self.http.get_json(f"{LEGACY_API}/{path}", params=params or None)
        if not payload.get("success"):
            raise ValueError(f"{path} returned success=false: {payload.get('message')}")
        return payload.get("data") or []

    def list_districts(self) -> list[NamedRef]:
        return [NamedRef(_lead_int(r.get("number")), r["name"].strip())
                for r in self._get("district-names")]

    def list_acs(self, district_name: str) -> list[NamedRef]:
        return [NamedRef(_lead_int(r.get("number")), r["name"].strip())
                for r in self._get("ac-names", districtName=district_name)]

    def list_parts(self, ac_name: str) -> list[NamedRef]:
        return [NamedRef(_lead_int(r.get("number")), r["name"].strip())
                for r in self._get("part-names", acName=ac_name)]

    def list_villages(self, search_text: str) -> list[str]:
        return [v for v in self._get("villages", searchText=search_text) if isinstance(v, str)]

    def village_details(self, village_name: str) -> list[VillageMapping]:
        """Official 2003 -> 2025 part mapping for a village."""
        out = []
        for r in self._get("village-details", villageName=village_name):
            out.append(VillageMapping(
                district_name=r.get("districtName"),
                ac2003_number=_lead_int(r.get("ac2003")),
                ac2003_name=(r.get("acName") or "").strip() or None,
                part2003_number=_lead_int(r.get("part2003")),
                part2003_name=(r.get("part2003") or "").strip() or None,
                village=r.get("gram"),
                ac2025_number=_lead_int(r.get("ac2025")),
                ac2025_name=(r.get("acName2025") or "").strip() or None,
                part2025_number=_lead_int(r.get("part2025")),
                part2025_name=(r.get("part2025Names") or "").strip() or None,
                raw=r,
            ))
        return out

    # -- the roll PDF ----------------------------------------------------
    @staticmethod
    def roll_relative_path(ac_number: int, ac_name: str, part_number: int) -> str:
        """Path layout taken from the portal's own JS.

        files/Roll2003/{ac:02d}-{acNameHindi}/P{ac:03d}{part:04d}.pdf
        """
        folder = f"{ac_number:02d}-{ac_name.strip()}"
        filename = f"P{ac_number:03d}{part_number:04d}.pdf"
        return f"files/Roll2003/{folder}/{filename}"

    def roll_pdf_url(self, ac_number: int, ac_name: str, part_number: int) -> str:
        from urllib.parse import quote
        rel = self.roll_relative_path(ac_number, ac_name, part_number)
        return f"{LEGACY_API}/pdf?filePath={quote(rel, safe='')}"

    def fetch_roll_pdf(self, ac_number: int, ac_name: str, part_number: int) -> tuple[bytes, str]:
        rel = self.roll_relative_path(ac_number, ac_name, part_number)
        url = self.roll_pdf_url(ac_number, ac_name, part_number)
        content = self.http.get_pdf(f"{LEGACY_API}/pdf", params={"filePath": rel})
        return content, url


# --------------------------------------------------------------------------
# Form 20
# --------------------------------------------------------------------------
FORM20_INDEXES: dict[int, str] = {
    2022: f"{HOST}/CEO-Website/VidhanSabha2022/Form20/form20_2022.htm",
    2017: f"{HOST}/CEO-Website/VidhanSabha2017/Form_20/form20_2017.htm",
    2012: f"{HOST}/Vidhan_sabha2012/form20_2012_PDF/form20_2012.htm",
}


@dataclass(frozen=True)
class Form20Entry:
    ac_number: int | None
    ac_label: str
    url: str


class Form20Client:
    """Form 20 (booth-level results) indexes and PDFs for Vidhan Sabha elections."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def index_url(self, year: int) -> str:
        try:
            return FORM20_INDEXES[year]
        except KeyError:
            raise LookupError(
                f"no Form 20 index configured for {year}; known years: "
                f"{sorted(FORM20_INDEXES)}"
            ) from None

    def list_acs(self, year: int) -> list[Form20Entry]:
        idx = self.index_url(year)
        html = self.http.get_text(idx)
        soup = BeautifulSoup(html, "lxml")
        base = idx.rsplit("/", 1)[0] + "/"
        out = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.lower().endswith(".pdf"):
                continue
            label = " ".join(a.get_text(strip=True).split())
            out.append(Form20Entry(
                ac_number=_lead_int(href),
                ac_label=label,
                url=base + href.replace(" ", "%20"),
            ))
        return out

    def fetch(self, year: int, ac_number: int) -> tuple[bytes, str]:
        for e in self.list_acs(year):
            if e.ac_number == ac_number:
                return self.http.get_pdf(e.url), e.url
        raise LookupError(f"AC {ac_number} not present in the Form 20 {year} index")
