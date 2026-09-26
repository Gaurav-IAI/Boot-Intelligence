"""Booth Intelligence dashboard.

Server-rendered HTML, no build step, no JS framework. Every figure is computed
from the POC database by `analytics.intelligence` / `analytics.booth`; templates
only lay it out. The pages answer three questions — where, what do we know, what
can we compare — and say plainly when data is not available.

Every page belongs to one state. Pages about a district, constituency, booth or
result take the state of that record; the others use the state chosen in the
header (`?state=<code>`, remembered in a cookie), defaulting to Uttarakhand.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ..analytics.intelligence import (
    ac_level_results, ac_observations, ac_overview, all_availability, booth_detail, default_ac_id,
    mapping_summary_all, mapping_table, quality_overview, result_detail, results_for_ac, search,
    source_label, state_ac_results_quality, state_results_quality, state_summary, summarize_results,
)
from ..config import ROOT
from ..database.models import AssemblyConstituency, District, Elector, State
from ..database.session import active_backend, get_session
from ..states import DEFAULT_STATE, ordered_states, spec, state_by_code, state_by_name

app = FastAPI(title="Election Data Intelligence")
STATE_COOKIE = "state"
templates = Jinja2Templates(directory=str(ROOT / "src" / "app" / "web" / "templates"))


def _n(v) -> str:
    return "—" if v is None else f"{v:,}"


def _pct(v, digits: int = 1) -> str:
    return "—" if v is None else f"{v:.{digits}f}%"


def ac_label(ac: AssemblyConstituency) -> str:
    if ac.delimitation == "current":
        return f"AC {ac.ac_number} {ac.ac_name}"
    return f"AC {ac.ac_number} {ac.ac_name} ({ac.delimitation})"


templates.env.filters["n"] = _n
templates.env.filters["pct"] = _pct
templates.env.filters["basename"] = lambda p: (p or "").replace("\\", "/").rsplit("/", 1)[-1]
templates.env.globals["source_label"] = source_label
templates.env.globals["ac_label"] = ac_label


def _current_state(request: Request, db, district: District | None = None) -> State | None:
    """The page's state: the record's own state, else the header choice, else the default."""
    if district is not None:
        return db.get(State, district.state_id)
    return (state_by_code(db, request.query_params.get("state"))
            or state_by_code(db, request.cookies.get(STATE_COOKIE))
            or state_by_name(db, DEFAULT_STATE)
            or (ordered_states(db) or [None])[0])


def _crumbs(db, state: State | None, district: District | None = None,
            ac: AssemblyConstituency | None = None, tail: str | None = None) -> list[dict]:
    out = []
    if state:
        out.append({"label": state.state_name, "href": f"/?state={state.state_code}"})
    if district:
        out.append({"label": district.district_name, "href": f"/district/{district.id}"})
    if ac:
        out.append({"label": ac_label(ac), "href": f"/ac/{ac.id}"})
    if tail:
        out.append({"label": tail, "href": None})
    return out


def _render(request: Request, name: str, db, state: State | None, *, active: str, crumbs=None, **ctx):
    sp = spec(state.state_name if state else None)
    ctx.update(nav_ac=default_ac_id(db, state.id) if state else None, active=active,
               crumbs=crumbs or [], backend=active_backend(), current_state=state,
               states=ordered_states(db), booth_sources=bool(sp and sp.booth_sources),
               result_sources=bool(sp and sp.result_sources))
    resp = templates.TemplateResponse(request, name, ctx)
    # Remember the state so pages without a record of their own (search, quality, the
    # constituency list) stay on it.
    if state and request.cookies.get(STATE_COOKIE) != state.state_code:
        resp.set_cookie(STATE_COOKIE, state.state_code, max_age=365 * 24 * 3600, samesite="lax")
    return resp


def _404(what: str):
    raise HTTPException(status_code=404, detail=f"{what} not found")


# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with get_session() as db:
        state = _current_state(request, db)
        sid = state.id if state else None
        s = state_summary(db, sid)
        featured = default_ac_id(db, sid)
        return _render(request, "index.html", db, state, active="dashboard", s=s,
                       featured=db.get(AssemblyConstituency, featured) if featured else None,
                       mapping=mapping_summary_all(db, sid),
                       historical=[a for a in s["availability"] if not a.is_current],
                       result_acs=[a for a in s["availability"] if a.result_records],
                       rq=state_results_quality(db, sid) if s["results"] else None,
                       aq=state_ac_results_quality(db, sid) if s["acs_with_ac_results"] else None)


@app.get("/constituencies", response_class=HTMLResponse)
def constituencies(request: Request):
    with get_session() as db:
        state = _current_state(request, db)
        s = state_summary(db, state.id if state else None)
        by_district: dict[int, list] = {}
        for a in s["availability"]:
            if a.is_current:
                by_district.setdefault(a.district.id, []).append(a)
        return _render(request, "constituencies.html", db, state, active="constituencies",
                       crumbs=_crumbs(db, state, tail="Constituencies"), s=s, by_district=by_district,
                       historical=[a for a in s["availability"] if not a.is_current])


@app.get("/district/{district_id}", response_class=HTMLResponse)
def district(request: Request, district_id: int):
    with get_session() as db:
        d = db.get(District, district_id) or _404("district")
        state = _current_state(request, db, d)
        avs = [a for a in all_availability(db, state_id=d.state_id) if a.district.id == d.id]
        return _render(request, "district.html", db, state, active="constituencies",
                       crumbs=_crumbs(db, state, d), district=d,
                       current=[a for a in avs if a.is_current],
                       historical=[a for a in avs if not a.is_current])


@app.get("/ac/{ac_id}", response_class=HTMLResponse)
def ac_view(request: Request, ac_id: int):
    with get_session() as db:
        o = ac_overview(db, ac_id) or _404("constituency")
        av = all_availability(db, [ac_id])[0]
        roll_stats = [r.stats for r in o.rows if r.stats]
        state = _current_state(request, db, o.district)
        same_number = None if o.ac.delimitation == "current" else db.scalar(
            select(AssemblyConstituency)
            .join(District, AssemblyConstituency.district_id == District.id)
            .where(District.state_id == o.district.state_id,
                   AssemblyConstituency.ac_number == o.ac.ac_number,
                   AssemblyConstituency.delimitation == "current"))
        return _render(request, "ac.html", db, state, active="constituencies",
                       crumbs=_crumbs(db, state, o.district, o.ac), o=o, av=av, same_number=same_number,
                       observations=ac_observations(av, o.elections, roll_stats),
                       ac_elections=ac_level_results(db, o.ac))


@app.get("/ac/{ac_id}/performance", response_class=HTMLResponse)
def ac_performance(request: Request, ac_id: int):
    with get_session() as db:
        ac = db.get(AssemblyConstituency, ac_id) or _404("constituency")
        d = db.get(District, ac.district_id)
        results = results_for_ac(db, ac)
        state = _current_state(request, db, d)
        return _render(request, "performance.html", db, state, active="results",
                       crumbs=_crumbs(db, state, d, ac, tail="Historical results"),
                       ac=ac, district=d, results=results, summaries=summarize_results(results),
                       ac_elections=ac_level_results(db, ac))


@app.get("/ac/{ac_id}/changes", response_class=HTMLResponse)
def ac_mapping(request: Request, ac_id: int):
    with get_session() as db:
        found = mapping_table(db, ac_id) or _404("constituency")
        ac, d, rows, counts = found
        state = _current_state(request, db, d)
        return _render(request, "changes.html", db, state, active="mapping",
                       crumbs=_crumbs(db, state, d, ac, tail="Booth mapping"),
                       ac=ac, district=d, rows=rows, counts=counts)


@app.get("/result/{result_id}", response_class=HTMLResponse)
def result_view(request: Request, result_id: int):
    with get_session() as db:
        found = result_detail(db, result_id) or _404("result")
        r, ac = found
        d = db.get(District, ac.district_id) if ac else None
        state = _current_state(request, db, d)
        return _render(request, "result.html", db, state, active="results",
                       crumbs=_crumbs(db, state, d, ac, tail=f"Part {r.part_number} ({r.year})"),
                       r=r, ac=ac, district=d)


@app.get("/station/{station_id}", response_class=HTMLResponse)
def station_view(request: Request, station_id: int, page: int = 1, size: int = 100):
    with get_session() as db:
        det = booth_detail(db, station_id) or _404("polling station")
        st = det.station
        _, _, mrows, _ = mapping_table(db, det.ac.id)
        if st.edition == "SIR-2026":
            lineage = [m for m in mrows if m.current_station_id == st.id]
        else:
            lineage = [m for m in mrows if m.hist_station_id == st.id]
        page, size = max(page, 1), min(max(size, 10), 500)
        electors, total = [], 0
        if det.roll is not None:
            total = db.scalar(select(func.count()).select_from(Elector)
                              .where(Elector.electoral_roll_id == det.roll.id)) or 0
            electors = db.scalars(
                select(Elector).where(Elector.electoral_roll_id == det.roll.id)
                .order_by(Elector.serial_number)
                .offset((page - 1) * size).limit(size)).all()
        state = _current_state(request, db, det.district)
        return _render(request, "station.html", db, state, active="constituencies",
                       crumbs=_crumbs(db, state, det.district, det.ac, tail=f"Part {st.part_number}"),
                       d=det, r=det.row, av=all_availability(db, [det.ac.id])[0],
                       lineage=lineage, electors=electors, total=total, page=page, size=size,
                       pages=max(1, (total + size - 1) // size),
                       prov_open="page" in request.query_params)


@app.get("/quality", response_class=HTMLResponse)
def quality_view(request: Request):
    with get_session() as db:
        state = _current_state(request, db)
        data = quality_overview(db, active_backend(), state.id if state else None, include_results=False)
        clean = [r for r in data["rolls"]
                 if not r["stats"].invalid_rows and not r["stats"].serial_outliers]
        review = sorted((r for r in data["rolls"] if r not in clean),
                        key=lambda r: -r["stats"].invalid_rows)
        return _render(request, "quality.html", db, state, active="quality",
                       crumbs=_crumbs(db, state, tail="Data quality"),
                       clean=clean, review=review, **data)


@app.get("/search", response_class=HTMLResponse)
def search_view(request: Request, q: str = ""):
    with get_session() as db:
        state = _current_state(request, db)
        return _render(request, "search.html", db, state, active="search",
                       crumbs=_crumbs(db, state, tail="Search"),
                       hits=search(db, q, state_id=state.id if state else None), search_q=q)
