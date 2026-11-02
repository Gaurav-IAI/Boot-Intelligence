"""Whole-state calculations are reused between page views only while the data is unchanged."""
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analytics import intelligence as bi
from app.database.models import Base
from app.web import app as web


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    bi._MEMO.clear()
    with Session(engine) as s:
        yield s
    bi._MEMO.clear()


@pytest.fixture
def version(monkeypatch):
    """A database position the test moves by hand, as PostgreSQL's would move on a write."""
    now = {"v": "0/1"}
    monkeypatch.setattr(bi, "_data_version", lambda _db: now["v"])
    return now


def counter():
    calls = []

    def compute():
        calls.append(1)
        return len(calls)
    return compute, calls


def test_reused_while_the_database_has_not_moved(db, version):
    compute, calls = counter()
    assert [bi._memo(db, "x", 1, compute) for _ in range(3)] == [1, 1, 1]
    assert len(calls) == 1


def test_recalculated_as_soon_as_the_database_moves(db, version):
    compute, _ = counter()
    assert bi._memo(db, "x", 1, compute) == 1
    version["v"] = "0/2"
    assert bi._memo(db, "x", 1, compute) == 2


def test_recalculated_when_an_input_outside_the_database_changes(db, version):
    compute, _ = counter()
    assert bi._memo(db, "x", 1, compute, stamp="a") == 1
    assert bi._memo(db, "x", 1, compute, stamp="a") == 1
    assert bi._memo(db, "x", 1, compute, stamp="b") == 2


def test_each_key_has_its_own_result(db, version):
    compute, calls = counter()
    assert bi._memo(db, "x", "Uttarakhand", compute) == 1
    assert bi._memo(db, "x", "Telangana", compute) == 2
    assert bi._memo(db, "y", "Uttarakhand", compute) == 3
    assert bi._memo(db, "x", "Uttarakhand", compute) == 1
    assert len(calls) == 3


def test_a_write_during_the_calculation_is_not_mistaken_for_the_new_data(db, version):
    """The result was read from the older state, so the moved position must not serve it."""
    def compute():
        version["v"] = "0/2"
        return "stale"
    assert bi._memo(db, "x", 1, compute) == "stale"
    assert bi._memo(db, "x", 1, lambda: "fresh") == "fresh"


def test_sqlite_is_never_reused(db):
    """Without a cheap version to compare, every call reads the data."""
    assert bi._data_version(db) is None
    compute, _ = counter()
    assert [bi._memo(db, "x", 1, compute) for _ in range(2)] == [1, 2]
    assert not bi._MEMO


def test_concurrent_visitors_calculate_once(db, version):
    release, calls = threading.Event(), []

    def compute():
        calls.append(1)
        release.wait(5)
        return "value"
    results = []
    threads = [threading.Thread(target=lambda: results.append(bi._memo(db, "x", 1, compute)))
               for _ in range(4)]
    for t in threads:
        t.start()
    release.set()
    for t in threads:
        t.join(5)
    assert results == ["value"] * 4
    assert len(calls) == 1


def test_evaluate_mappings_is_reused_until_the_data_changes(db, version, monkeypatch):
    compute_calls = []
    monkeypatch.setattr(bi, "scope_state_id", lambda _db, state_id=None: state_id)
    monkeypatch.setattr(bi, "_ps_list_2024_stamp", lambda: ())
    monkeypatch.setattr(bi, "_evaluate_mappings", lambda _db, sid: compute_calls.append(sid) or [sid])
    assert bi.evaluate_mappings(db, 7) == bi.evaluate_mappings(db, 7) == [7]
    assert bi.evaluate_mappings(db, 8) == [8]
    assert compute_calls == [7, 8]
    version["v"] = "0/2"
    bi.evaluate_mappings(db, 7)
    assert compute_calls == [7, 8, 7]


def test_a_failed_warm_up_does_not_stop_the_dashboard(monkeypatch):
    def unreachable():
        raise RuntimeError("database is down")
    monkeypatch.setattr(web, "get_session", unreachable)
    web._warm_caches()
