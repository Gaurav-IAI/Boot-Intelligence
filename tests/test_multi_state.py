"""Several states in one database.

AC numbers repeat across states (Uttarakhand and Telangana both have an AC 19).
Form 20 results and part mappings are keyed by AC number, so these tests pin
the rule that a number is only ever matched inside the state that owns the row:
a constituency list loaded from ECI for a second state must show no results and
no mapping, and the first state's figures must not change.
"""
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.analytics import intelligence as bi
from app.database import repositories as repo
from app.database.models import Base
from app.states import STATE_NAMES, mapping_state_id
from tests.test_intelligence import world  # noqa: F401  (shared fixture)


@pytest.fixture
def db():
    # one connection shared across threads: the web tests serve requests from a threadpool
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def ts(db, world):
    """Telangana: one district and a current AC 19, with nothing but its identity."""
    state = repo.upsert_state(db, state_code="S29", state_name="Telangana", state_name_local=None,
                              state_type="ST", external_id=28, source="eci_gateway_api", source_url="u")
    d = repo.upsert_district(db, state=state, district_code="S2901", district_number=1,
                             district_name="Adilabad", district_name_local=None,
                             source="eci_gateway_api", source_url="u")
    ac19 = repo.upsert_ac(db, district=d, ac_number=19, ac_name="Nizamabad Urban",
                          source="eci_gateway_api")
    db.commit()
    return dict(state=state, district=d, ac19=ac19)


def test_registry_lists_the_three_states():
    assert STATE_NAMES == ("Uttarakhand", "Uttar Pradesh", "Telangana")
    assert mapping_state_id(None, 1) == 1 and mapping_state_id(2, 1) == 2


class TestNoCrossStateLeak:
    def test_results_stay_in_their_state(self, db, world, ts):
        assert bi.results_for_ac(db, ts["ac19"]) == []
        assert len(bi.results_for_ac(db, world["cur"])) == 2
        assert bi.all_result_views(db, ts["state"].id) == []

    def test_availability_of_the_same_number_in_another_state(self, db, world, ts):
        av = bi.all_availability(db, [ts["ac19"].id])[0]
        assert av.result_records == 0 and av.mapping_status == "none" and av.linked_acs == []
        uk = bi.all_availability(db, [world["cur"].id])[0]
        assert uk.result_records == 2 and uk.mapping_status != "none"

    def test_ac_pages_show_no_mapping_or_results(self, db, world, ts):
        o = bi.ac_overview(db, ts["ac19"].id)
        assert o.results == [] and o.related_acs == [] and o.mapping_verified == o.mapping_review == 0
        assert o.state.state_name == "Telangana"
        counts = bi.mapping_table(db, ts["ac19"].id)[3]
        assert sum(counts.values()) == 0
        assert bi.ac_changes(db, ts["ac19"].id)[3]["mapping_rows"] == 0

    def test_mapping_evaluation_is_per_state(self, db, world, ts):
        assert bi.evaluate_mappings(db, ts["state"].id) == []
        assert len(bi.evaluate_mappings(db)) == len(bi.evaluate_mappings(db, world["cur"].district.state_id)) == 2

    def test_a_mapping_stored_for_another_state_is_not_counted_here(self, db, world, ts):
        repo.upsert_part_mapping(
            db, from_edition="ROLL-2003", from_ac_number=15, from_part_number=6, to_edition="ROLL-2025",
            to_ac_number=19, to_part_number=9, area_name="elsewhere", mapping_method="m",
            confidence=0.9, source="t", state_id=ts["state"].id)
        db.commit()
        assert len(bi.evaluate_mappings(db)) == 2                   # Uttarakhand unchanged
        assert [m.area_name for m in bi.evaluate_mappings(db, ts["state"].id)] == ["elsewhere"]

    def test_summaries_are_scoped(self, db, world, ts):
        before = bi.mapping_summary_all(db)
        uk, tg = bi.state_summary(db), bi.state_summary(db, ts["state"].id)
        assert (uk["acs_current"], uk["mappings"], uk["results"], uk["booth_sources"]) == (2, 2, 2, True)
        assert (tg["districts"], tg["acs_current"], tg["stations_current"]) == (1, 1, 0)
        assert (tg["mappings"], tg["results"], tg["electors"], tg["booth_sources"]) == (0, 0, 0, False)
        assert sum(bi.mapping_summary_all(db, ts["state"].id).values()) == 0
        assert bi.mapping_summary_all(db) == before

    def test_navigation_default_and_search(self, db, world, ts):
        assert bi.default_ac_id(db) == world["cur"].id
        assert bi.default_ac_id(db, ts["state"].id) is None
        hits = bi.search(db, "19", state_id=ts["state"].id)
        assert [h.href for h in hits] == [f"/ac/{ts['ac19'].id}"]
        assert any(h.kind == "Historical result" for h in bi.search(db, "part 1"))
        assert bi.search(db, "part 1", state_id=ts["state"].id) == []

    def test_quality_is_scoped(self, db, world, ts):
        q = bi.quality_overview(db, "t", ts["state"].id)
        assert q["results"] == [] and q["rolls"] == [] and sum(q["mapping"].values()) == 0
        assert q["q"].districts == 1 and q["q"].electors == 0 and q["q"].part_mappings == 0
        uk = bi.quality_overview(db, "t")
        assert len(uk["results"]) == 2 and len(uk["rolls"]) == 1 and uk["q"].electors == 4


# --- web pages ----------------------------------------------------------------------
@pytest.fixture
def client(db, world, ts, monkeypatch):
    from fastapi.testclient import TestClient

    from app.web import app as web

    @contextmanager
    def session():
        yield db

    monkeypatch.setattr(web, "get_session", session)
    monkeypatch.setattr(web, "active_backend", lambda: "sqlite-test")
    ids = dict(uk_ac=world["cur"].id, ts_ac=ts["ac19"].id)
    return TestClient(web.app), ids


def test_dashboard_follows_the_state_choice(client):
    c, _ = client
    uk = c.get("/")
    assert uk.status_code == 200 and "UTTARAKHAND" in uk.text and 'value="S29"' in uk.text
    tg = c.get("/?state=S29")
    assert "TELANGANA" in tg.text and "Constituency list only" in tg.text
    assert tg.cookies.get("state") == "S29"
    # pages without a record of their own stay on the chosen state
    cons = c.get("/constituencies")
    assert "Nizamabad Urban" in cons.text and "Raipur" not in cons.text
    assert "Not yet available for this state" in cons.text          # results/mapping nav disabled


def test_record_pages_take_their_own_state(client):
    c, ids = client
    c.get("/?state=S29")
    uk = c.get(f"/ac/{ids['uk_ac']}")
    assert uk.status_code == 200 and 'href="/?state=S28">Uttarakhand' in uk.text
    tg = c.get(f"/ac/{ids['ts_ac']}")
    assert 'href="/?state=S29">Telangana' in tg.text and "Not available in current POC" in tg.text
    assert c.get("/quality?state=S29").status_code == 200
    assert c.get("/search?q=19").status_code == 200
