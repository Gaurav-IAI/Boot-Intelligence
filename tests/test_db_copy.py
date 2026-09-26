"""`migrate-db`: moving loaded data to a new database (deployment without re-downloading)."""
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import repositories as repo
from app.database.models import AssemblyConstituency, Base, District, PollingStation, State
from app.services.db_copy import copy_database


@pytest.fixture
def source(tmp_path):
    url = f"sqlite:///{(tmp_path / 'src.sqlite3').as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        st = repo.upsert_state(s, state_code="S24", state_name="Uttar Pradesh", state_name_local=None,
                               state_type="ST", external_id=23, source="eci_gateway_api", source_url="u")
        d = repo.upsert_district(s, state=st, district_code="S2401", district_number=1, district_name="Agra",
                                 district_name_local=None, source="eci_gateway_api", source_url="u")
        ac = repo.upsert_ac(s, district=d, ac_number=86, ac_name="Etmadpur", source="eci_gateway_api")
        for n in (1, 2, 3):
            repo.upsert_polling_station(s, ac=ac, part_number=n, edition="SIR-2026", source="t",
                                        polling_station_name=f"School {n}", elector_count=700 + n)
        s.commit()
    return url


def test_everything_is_copied_with_its_ids(source, tmp_path):
    target = f"sqlite:///{(tmp_path / 'dst.sqlite3').as_posix()}"
    report = copy_database(source, target)
    assert report.rows["polling_stations"] == 3 and report.rows["states"] == 1
    with Session(create_engine(target)) as s:
        ac = s.scalar(select(AssemblyConstituency))
        assert (ac.ac_number, ac.ac_name) == (86, "Etmadpur")
        assert s.scalar(select(District.state_id)) == s.scalar(select(State.id))
        assert sorted(s.scalars(select(PollingStation.elector_count))) == [701, 702, 703]


def test_a_non_empty_target_is_refused_unless_replaced(source, tmp_path):
    target = f"sqlite:///{(tmp_path / 'dst.sqlite3').as_posix()}"
    copy_database(source, target)
    with pytest.raises(RuntimeError, match="not empty"):
        copy_database(source, target)
    copy_database(source, target, replace=True)             # emptied first: not doubled
    with Session(create_engine(target)) as s:
        assert s.scalar(select(func.count()).select_from(PollingStation)) == 3
