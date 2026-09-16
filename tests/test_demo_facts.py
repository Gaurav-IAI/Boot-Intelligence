"""Frozen demo facts, checked against the configured database and the rendered pages.

Skipped when that database is not reachable or the AC 19 demo data is not loaded,
so the suite still runs on a fresh checkout.
"""
import html
import re

import pytest
from sqlalchemy import func, select

from app.analytics import intelligence as bi
from app.database.models import AssemblyConstituency, ElectionResult, PollingStation


@pytest.fixture(scope="module")
def live():
    from app.database.session import get_session
    try:
        with get_session() as db:
            ac = db.scalar(select(AssemblyConstituency).where(
                AssemblyConstituency.ac_number == 19, AssemblyConstituency.delimitation == "current"))
            n = db.scalar(select(func.count()).select_from(ElectionResult)
                          .where(ElectionResult.ac_id == ac.id)) if ac else 0
            if not n:
                pytest.skip("AC 19 demo data is not loaded")
            yield db, ac
    except pytest.skip.Exception:
        raise
    except Exception as exc:                      # no database available
        pytest.skip(f"demo database not reachable: {exc}")


@pytest.fixture(scope="module")
def client(live):
    from fastapi.testclient import TestClient
    from app.web.app import app
    return TestClient(app)


def _visible(page: str) -> str:
    page = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
    return html.unescape(" ".join(re.sub(r"<[^>]+>", " ", page).split()))


def _cards(page: str) -> dict[str, str]:
    return {html.unescape(re.sub(r"<[^>]+>", "", label)).strip(): re.sub(r"<[^>]+>", "", value).strip()
            for value, label in re.findall(r'<div class="v[^"]*">(.*?)</div>\s*<div class="l">(.*?)</div>', page, re.S)}


def test_form20_facts(live):
    db, ac = live
    views = {r.part_number: r for r in bi.results_for_ac(db, ac) if r.year == 2012}
    s = next(e for e in bi.summarize_results(list(views.values())) if e.year == 2012)
    assert (s.booths, s.sum_check_pass, s.needs_review, s.sum_check_fail) == (152, 151, 1, [141])
    assert views[87].verified
    assert not views[141].verified and views[141].margin is None
    m = views[60].margin
    assert views[60].verified and (m.leader_votes, m.runner_up_votes, m.margin) == (140, 140, 0)
    assert s.zero_margin == 1


def test_mapping_facts(live):
    db, ac = live
    _, _, rows, counts = bi.mapping_table(db, ac.id)
    assert len(rows) == 22
    assert counts == {"Verified": 11, "Review Required": 9, "Not Mapped": 2}
    assert all(r.current_station_id for r in rows if r.status == "Verified")


def test_part_search_lists_the_demo_constituency_first(live):
    db, ac = live
    hits = bi.search(db, "Part 141")
    r141 = db.scalar(select(ElectionResult.id).where(ElectionResult.ac_id == ac.id, ElectionResult.part_number == 141))
    assert hits and all(h.title.startswith("Part 141 ") for h in hits)
    assert f"/result/{r141}" in [h.href for h in hits[:5]]
    old10 = db.scalar(select(PollingStation.id).where(PollingStation.edition == "ROLL-2003",
                                                      PollingStation.part_number == 10))
    assert f"/station/{old10}" in [h.href for h in bi.search(db, "Part 10")[:5]]


def test_summary_cards_equal_table_rows(client, live):
    _, ac = live
    page = client.get(f"/ac/{ac.id}/performance").text
    cards = _cards(page)
    table = re.search(r'id="res2012".*?<tbody>(.*?)</tbody>', page, re.S).group(1)
    assert (cards["Result records"], cards["Vote-sum verified"], cards["Requires review"]) == ("152", "151", "1")
    assert len(re.findall(r"<tr", table)) == 152 and table.count("Review required") == 1

    page = client.get(f"/ac/{ac.id}/changes").text
    cards = _cards(page)
    table = re.search(r"<tbody>(.*?)</tbody>", page, re.S).group(1)
    shown = {label: len(re.findall(rf">{label}</span>\s*</td>", table))
             for label in ("Verified", "Review required", "Not mapped")}
    assert {k: int(cards[k]) for k in shown} == shown == {"Verified": 11, "Review required": 9, "Not mapped": 2}


def test_labels_and_unavailable_current_roll(client, live):
    db, ac = live
    perf = _visible(client.get(f"/ac/{ac.id}/performance").text)
    assert "HISTORICAL RESULT — 2012" in perf and "original 2012 polling-station numbering" in perf
    r141 = db.scalar(select(ElectionResult.id).where(ElectionResult.ac_id == ac.id, ElectionResult.part_number == 141))
    detail = _visible(client.get(f"/result/{r141}").text)
    assert "REVIEW REQUIRED" in detail and "Candidate / party interpretation: Withheld" in detail
    assert "One candidate cell is blank/missing" in detail
    assert "2012 booth numbering — not current (2026) Part 141" in detail

    current = db.scalar(select(PollingStation.id).where(PollingStation.ac_id == ac.id,
                                                        PollingStation.edition == "SIR-2026",
                                                        PollingStation.part_number == 1))
    station = _visible(client.get(f"/station/{current}").text)
    assert "Current polling station — 2026" in station
    assert "not available from the current public source" in station
    assert not re.search(r"\d[\d,]* electors", station)          # no fabricated elector count

    old = db.scalar(select(PollingStation.id).where(PollingStation.edition == "ROLL-2003",
                                                    PollingStation.part_number == 6))
    assert "Historical electoral roll — 2003" in _visible(client.get(f"/station/{old}").text)
