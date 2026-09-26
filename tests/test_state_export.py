"""export-state / import-state: moving one state's data into a deployment that has others."""
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.analytics import intelligence as bi
from app.database import repositories as repo
from app.database.models import (
    AssemblyConstituency, Base, District, Election, ElectionResult, PollingStation, SourceFetch, State,
)
from app.services.state_export import export_states, import_states


def _state(s, code, name, district, acs, stations=0, results=0):
    st = repo.upsert_state(s, state_code=code, state_name=name, state_name_local=None, state_type="ST",
                           external_id=1, source="eci_gateway_api", source_url="u")
    d = repo.upsert_district(s, state=st, district_code=code + "01", district_number=1, district_name=district,
                             district_name_local=None, source="eci_gateway_api", source_url="u")
    for n in acs:
        ac = repo.upsert_ac(s, district=d, ac_number=n, ac_name=f"{name} AC {n}", source="eci_gateway_api")
        for p in range(1, stations + 1):
            repo.upsert_polling_station(s, ac=ac, part_number=p, edition="SIR-2026", source="t",
                                        polling_station_name=f"{name} {n}/{p}", elector_count=500 + p)
        if results:
            e = repo.upsert_election(s, election_year=2022, election_type="VIDHAN_SABHA", state=name, source_url="u")
            repo.replace_results(s, election=e, ac_number=n, rows=[
                dict(ac_id=ac.id, part_number=p, candidate_votes_json=json.dumps([p, 1]), total_valid_votes=p + 1,
                     source="t", candidates_json=json.dumps([{"name": "A"}, {"name": "B"}]))
                for p in range(1, results + 1)])
    s.commit()


@pytest.fixture
def dbs(tmp_path):
    """source: Uttarakhand + UP (the workstation). production: Uttarakhand only, other ids."""
    src = f"sqlite:///{(tmp_path / 'src.sqlite3').as_posix()}"
    prod = f"sqlite:///{(tmp_path / 'prod.sqlite3').as_posix()}"
    for url in (src, prod):
        Base.metadata.create_all(create_engine(url))
    with Session(create_engine(src)) as s:
        _state(s, "S24", "Uttar Pradesh", "Agra", [86, 90], stations=3, results=4)
        _state(s, "S28", "Uttarakhand", "Dehradun", [19], stations=2)
        repo.record_fetch(s, source="ceo_up_form20", url="u", ok=True, note="form20 parser=x state=S24 year=2022 ac=86 status=stored")
        repo.record_fetch(s, source="ceo_uk_form20", url="u", ok=True, note="uk note")
        s.commit()
    with Session(create_engine(prod)) as s:
        _state(s, "S28", "Uttarakhand", "Dehradun", [19, 20], stations=5, results=2)   # ids 1.. collide
        s.commit()
    return src, prod, tmp_path


def _count(url, model, *where):
    with Session(create_engine(url)) as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def test_export_holds_only_the_chosen_state(dbs):
    src, _, tmp = dbs
    report = export_states(src, tmp / "up.sqlite3.gz", ["Uttar Pradesh"])
    assert report.rows["states"] == 1 and report.rows["polling_stations"] == 6
    assert report.rows["election_results"] == 8 and report.rows["source_fetches"] == 1


def test_import_adds_the_state_and_leaves_the_others(dbs):
    src, prod, tmp = dbs
    export_states(src, tmp / "up.sqlite3.gz", ["Uttar Pradesh"])
    import_states(tmp / "up.sqlite3.gz", prod)

    # Uttarakhand in production untouched
    assert _count(prod, PollingStation, PollingStation.polling_station_name.like("Uttarakhand%")) == 10
    assert _count(prod, ElectionResult, ElectionResult.source == "t") == 4 + 8     # 2 UK ACs x 2 + UP 8
    # Uttar Pradesh arrived, every link pointing at its own rows
    with Session(create_engine(prod)) as s:
        up = s.scalar(select(State).where(State.state_name == "Uttar Pradesh"))
        acs = s.scalars(select(AssemblyConstituency).join(District).where(District.state_id == up.id)).all()
        assert sorted(a.ac_number for a in acs) == [86, 90]
        ac86 = next(a for a in acs if a.ac_number == 86)
        stations = s.scalars(select(PollingStation).where(PollingStation.ac_id == ac86.id)).all()
        assert sorted(p.polling_station_name for p in stations) == ["Uttar Pradesh 86/1", "Uttar Pradesh 86/2",
                                                                     "Uttar Pradesh 86/3"]
        views = bi.results_for_ac(s, ac86)
        assert len(views) == 4 and all(v.verified for v in views) and views[0].candidates
        assert s.scalar(select(func.count()).select_from(SourceFetch)
                        .where(SourceFetch.source == "ceo_up_form20")) == 1


def test_reimport_replaces_only_that_state(dbs):
    src, prod, tmp = dbs
    export_states(src, tmp / "up.sqlite3", ["Uttar Pradesh"])
    import_states(tmp / "up.sqlite3", prod)
    import_states(tmp / "up.sqlite3", prod)
    assert _count(prod, State) == 2
    assert _count(prod, PollingStation) == 10 + 6
    assert _count(prod, Election, Election.state == "Uttar Pradesh") == 1
    assert _count(prod, ElectionResult) == 4 + 8


def test_unknown_state_is_refused(dbs):
    src, _, tmp = dbs
    with pytest.raises(LookupError, match="Telangana"):
        export_states(src, tmp / "x.sqlite3", ["Telangana"])
