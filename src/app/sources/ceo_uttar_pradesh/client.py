"""CEO Uttar Pradesh — Form 20 (polling-station-wise results) as Excel workbooks.

The page https://ceouttarpradesh.nic.in/rollpdf/form20.aspx is an ASP.NET form:
choosing a year posts back and fills the district list; choosing a district posts
back and renders a grid with one link per assembly constituency. Links are read
from that grid, never built from a guessed pattern (the file naming differs by
year: `Form20_22/AC86.xls`, `Form20_17/86.xls`, `Form20_12/86.xls`).

No captcha, no login. The 2003 roll page on the same site IS captcha-gated and is
not touched here.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ...http_client import HttpClient
from ..ceo_uttarakhand.client import Form20Entry

HOST = "https://ceouttarpradesh.nic.in"
FORM20_PAGE = f"{HOST}/rollpdf/form20.aspx"
SOURCE_UP_FORM20 = "ceo_up_form20"

YEAR_FIELD = "ctl00$ContentPlaceHolder1$DropDownList2"
DISTRICT_FIELD = "ctl00$ContentPlaceHolder1$DropDownList1"
_PLACEHOLDERS = ("select year", "select district", "select district name")


def _hidden_fields(soup: BeautifulSoup) -> dict[str, str]:
    """ASP.NET state (__VIEWSTATE, __EVENTVALIDATION, ...) that a postback must echo."""
    return {i["name"]: i.get("value", "") for i in soup.find_all("input", type="hidden")
            if i.get("name", "").startswith("__")}


def _options(soup: BeautifulSoup, field: str) -> list[str]:
    sel = soup.find("select", attrs={"name": field})
    if sel is None:
        return []
    return [o.get("value", "").strip() for o in sel.find_all("option")
            if o.get("value", "").strip() and o.get("value", "").strip().lower() not in _PLACEHOLDERS]


def parse_years(html: str) -> list[int]:
    return [int(v) for v in _options(BeautifulSoup(html, "lxml"), YEAR_FIELD) if v.isdigit()]


def parse_districts(html: str) -> list[str]:
    return _options(BeautifulSoup(html, "lxml"), DISTRICT_FIELD)


def parse_ac_links(html: str, page_url: str = FORM20_PAGE) -> list[Form20Entry]:
    """Rows of the results grid: District | AC No | AC Name | link."""
    soup = BeautifulSoup(html, "lxml")
    grid = soup.find("table", id=re.compile(r"GridView"))
    out: list[Form20Entry] = []
    if grid is None:
        return out
    for tr in grid.find_all("tr"):
        tds = tr.find_all("td")
        a = tr.find("a", href=True)
        if len(tds) < 3 or a is None or a["href"].strip().lower().startswith("javascript:"):
            continue            # the pager row ("1 2") links by postback, not to a file
        number = tds[1].get_text(strip=True)
        out.append(Form20Entry(
            ac_number=int(number) if number.isdigit() else None,
            ac_label=f"{number}-{tds[2].get_text(strip=True)}",
            url=urljoin(page_url, a["href"].strip().replace(" ", "%20")),
        ))
    return out


_PAGE = re.compile(r"__doPostBack\('([^']*GridView[^']*)','Page\$(\d+)'\)")


def parse_grid_pages(html: str) -> tuple[str | None, list[int]]:
    """(grid control name, other page numbers) when the results grid is paginated."""
    found = [(m.group(1), int(m.group(2))) for m in _PAGE.finditer(html.replace("&#39;", "'"))]
    if not found:
        return None, []
    return found[0][0], sorted({p for _, p in found})


class UpForm20Client:
    """Walks the year -> district -> constituency postbacks of the UP Form 20 page."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def _postback(self, html: str, target: str, fields: dict[str, str], argument: str = "") -> str:
        form = _hidden_fields(BeautifulSoup(html, "lxml"))
        form.update({"__EVENTTARGET": target, "__EVENTARGUMENT": argument, **fields})
        return self.http.post_form(FORM20_PAGE, form)

    def _district_links(self, year_page: str, year: int, district: str) -> list[Form20Entry]:
        """All grid rows for a district, following the grid's pager when there is one."""
        fields = {YEAR_FIELD: str(year), DISTRICT_FIELD: district}
        html = self._postback(year_page, DISTRICT_FIELD, fields)
        out = parse_ac_links(html)
        grid, pages = parse_grid_pages(html)
        for page in pages:
            if page == 1:
                continue
            out += parse_ac_links(self._postback(html, grid, fields, argument=f"Page${page}"))
        return out

    def years(self) -> list[int]:
        return parse_years(self.http.get_text(FORM20_PAGE))

    def _year_page(self, year: int) -> str:
        first = self.http.get_text(FORM20_PAGE)
        if year not in parse_years(first):
            raise LookupError(f"UP Form 20 lists no {year}; available: {parse_years(first)}")
        return self._postback(first, YEAR_FIELD, {YEAR_FIELD: str(year)})

    def districts(self, year: int) -> list[str]:
        return parse_districts(self._year_page(year))

    def list_acs(self, year: int, districts: list[str] | None = None) -> list[Form20Entry]:
        """Every constituency link for a year, optionally only for some districts
        (matched case-insensitively against the site's own district names)."""
        page = self._year_page(year)
        names = parse_districts(page)
        if districts:
            wanted = {d.strip().casefold() for d in districts}
            unknown = wanted - {n.casefold() for n in names}
            if unknown:
                raise LookupError(f"district(s) {sorted(unknown)} not in the UP {year} list")
            names = [n for n in names if n.casefold() in wanted]
        out: list[Form20Entry] = []
        for name in names:
            out += [e for e in self._district_links(page, year, name) if link_matches_year(e.url, year)]
        return out


def link_matches_year(url: str, year: int) -> bool:
    """The site keeps each year's files in Form20_<yy>/. Its grid pager is buggy: page 2 of
    a 2022 or 2017 grid lists the 2012 files. A link from another year's folder is dropped,
    never stored under the requested year."""
    m = re.search(r"/Form20_(\d{2})/", url)
    return m is None or int(m.group(1)) == year % 100
