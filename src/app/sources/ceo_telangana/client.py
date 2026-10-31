"""CEO Telangana — Form 20 (Final Result Sheet) index pages.

Each year's INDEX.html lists one PDF per constituency, named "<n>-<Name>.pdf"
(2018: 001-Sirpur.pdf; 2023: 1-Sirpur.pdf). Links are read from the page, never guessed.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from ...http_client import HttpClient
from ..ceo_uttarakhand.client import Form20Entry

SOURCE_TG_FORM20 = "ceo_tg_form20"
TG_FORM20_INDEXES = {
    2018: "https://ceotelangana.nic.in/GE_2018/Form-20/INDEX.html",
    2023: "https://ceotelangana.nic.in/GE_2023/FORM-20/INDEX.html",
}
_HREF = re.compile(r'href\s*=\s*"([^"]+\.pdf)"', re.I)


def parse_index(html: str, base_url: str) -> list[Form20Entry]:
    out: dict[int, Form20Entry] = {}
    for href in _HREF.findall(html):
        name = href.rsplit("/", 1)[-1]
        m = re.match(r"\s*(\d{1,3})\s*-\s*(.*?)\s*\.pdf$", name, re.I)
        if not m:
            continue
        n = int(m.group(1))
        url = urljoin(base_url, quote(href.strip(), safe="/:"))
        out.setdefault(n, Form20Entry(ac_number=n, ac_label=m.group(2).strip(), url=url))
    return [out[k] for k in sorted(out)]


class TgForm20Client:
    def __init__(self, http: HttpClient):
        self.http = http

    def list_acs(self, year: int) -> list[Form20Entry]:
        url = TG_FORM20_INDEXES[year]
        return parse_index(self.http.request("GET", url).text, url)

    def get_pdf(self, url: str) -> bytes:
        return self.http.request("GET", url).content
