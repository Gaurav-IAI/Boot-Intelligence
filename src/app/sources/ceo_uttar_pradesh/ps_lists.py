"""District "List of Polling Stations" pages for Uttar Pradesh.

The CEO indexes one page per district (https://ceouttarpradesh.nic.in/Draft_List_PS_2025.aspx)
— the draft lists published on 04-07-2026, the latest official lists. Each District
Election Officer posts one or more PDFs per assembly constituency there, each district
laid out differently, so a link is attributed to a constituency only when its label
carries a number that is one of *that district's* AC numbers (labels also carry
serials: "5 90- Agra Rural").

Overrides replace the indexed page where it cannot be used. Ghaziabad's indexed page
refuses scripted requests and its 2026 PDFs are scans; its lists issued for the SIR
2026 roll (dated 06-11-2025) are text and are used instead, from the district's S3WaaS
host, which serves the same site.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ...http_client import HttpClient

SOURCE_UP_PS_LIST = "ceo_up_ps_list_2026"
CEO_INDEX = "https://ceouttarpradesh.nic.in/Draft_List_PS_2025.aspx"
INDEX_DATED = "2026-07-04"

# CEO index spelling -> ECI spelling (as stored)
DISTRICT_ALIASES = {"Bulandsahar": "Bulandshahr", "Pryagraj": "Prayagraj"}

DISTRICT_OVERRIDES: dict[str, dict[str, str]] = {
    "Ghaziabad": {
        "url": "https://s36da9003b743b65f4c0ccd295cc484e57.s3waas.gov.in/en/list-of-polling-station-2025/",
        "public_url": "https://ghaziabad.nic.in/en/list-of-polling-station-2025/",
        "dated": "2025-11-06",
    },
}
# kept for callers that list the checked pages
DISTRICT_PS_PAGES = DISTRICT_OVERRIDES


@dataclass(frozen=True)
class PsListEntry:
    district: str
    ac_number: int
    label: str
    url: str


def parse_ceo_index(html: str) -> dict[str, str]:
    """District (ECI spelling) -> the page the CEO index links for it."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        a = tr.find("a", href=True)
        if a and len(tds) >= 2:
            name = " ".join(tds[1].get_text(" ", strip=True).split())
            out[DISTRICT_ALIASES.get(name, name)] = a["href"].strip()
    return out


def parse_district_page(html: str, district: str, ac_numbers: set[int] | None = None,
                        page_url: str = "", state_acs: dict[int, list[str]] | None = None) -> list[PsListEntry]:
    """PDF links attributable to one AC. With `ac_numbers` (the district's ACs), a link counts
    when exactly one of them appears in its label (row text + link text); failing that, when
    exactly one "NN-Name" style number in the label is an AC of the state (`state_acs`: number ->
    names) *and* that AC's name is in the label too — a constituency split across districts is
    listed by both, while a bare "207-" may be a station number. Without `ac_numbers`, the label
    must start with the number ("055 – Sahibabad")."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().split("?")[0].endswith(".pdf"):
            continue
        url = urljoin(page_url, href)
        row = a.find_parent("tr") or a.find_parent("li") or a.find_parent("p")
        label = " ".join(((row.get_text(" ", strip=True) if row else "") + " " +
                          a.get_text(" ", strip=True)).split())
        if ac_numbers:
            hits = {int(n) for n in re.findall(r"(?<!\d)0*(\d{1,3})(?!\d)", label)} & ac_numbers
            if len(hits) != 1 and state_acs:
                low = label.casefold()
                hits = {n for n in (int(x) for x in re.findall(r"(?<!\d)0*(\d{1,3})\s*[–\-_]", label))
                        if any(len(nm) >= 3 and nm.casefold() in low for nm in state_acs.get(n, ()))}
            if len(hits) != 1:
                continue
            number = hits.pop()
        else:
            m = (re.match(r"^\s*0*(\d{1,3})\s*[–\-]", a.get_text(" ", strip=True))
                 or re.match(r"^\s*0*(\d{1,3})\s*[–\-]", label))
            if not m:
                continue
            number = int(m.group(1))
        if (number, url) not in seen:
            seen.add((number, url))
            out.append(PsListEntry(district, number, label[:120], url))
    return out


class UpPsListClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._index: dict[str, str] | None = None

    def index(self) -> dict[str, str]:
        if self._index is None:
            self._index = parse_ceo_index(self.http.get_text(CEO_INDEX))
        return self._index

    def districts(self) -> list[str]:
        return sorted(set(self.index()) | set(DISTRICT_OVERRIDES))

    def page(self, district: str) -> tuple[str, str]:
        """(url fetched, date of the lists) for a district."""
        if district in DISTRICT_OVERRIDES:
            o = DISTRICT_OVERRIDES[district]
            return o["url"], o["dated"]
        if district not in self.index():
            raise LookupError(f"{district} is not in the CEO's polling-station index")
        return self.index()[district], INDEX_DATED

    def list_acs(self, district: str, ac_numbers: set[int] | None = None,
                 state_acs: dict[int, list[str]] | None = None) -> list[PsListEntry]:
        url, _ = self.page(district)
        html = self.http.get_text(url)
        if "Blocked site" in html[:3000]:
            raise LookupError(f"{district} page refuses scripted requests: {url}")
        if district in DISTRICT_OVERRIDES:          # checked page: labels start with the AC number
            ac_numbers = None
        return parse_district_page(html, district, ac_numbers, page_url=url, state_acs=state_acs)
