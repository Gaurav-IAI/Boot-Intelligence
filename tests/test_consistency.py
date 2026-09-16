"""Regression tests: every dashboard count comes from one shared calculation and agrees with its rows."""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analytics import intelligence as bi
from app.database import repositories as repo
from app.database.models import Base, PartMapping

# AC 19, 2012 Form 20, as printed. Part 141's sheet has 22 candidate cells with one blank; the blank
# was dropped, so the sum still matches but every later column sits on the wrong candidate.
PART_87 = [5, 188, 6, 271, 4, 0, 1, 0, 0, 4, 24, 0, 2, 4, 0, 2, 4, 1, 3, 15, 1, 0]
PART_60 = [7, 140, 1, 140, 0, 0, 2, 0, 0, 1, 10, 0, 3, 0, 4, 0, 0, 1, 0, 29, 0, 1]
PART_141 = [7, 278, 0, 263, 1, 1, 0, 2, 0, 14, 38, 2, 3, 2, 4, 1, 2, 1, 25, 1, 1]


@pytest.fixture(autouse=True)
def no_ocr_cache(monkeypatch):
    monkeypatch.setattr(bi, "load_ps_list_2024_rows", lambda ac_number: None)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _row(part, valid, cols, flag=True):
    return dict(part_number=part, total_valid_votes=valid, total_votes=valid, source="t", vote_sum_matches=flag,
                candidate_votes_json=json.dumps(cols) if cols is not None else None)


def _sheet(db, extra=()):
    e = repo.upsert_election(db, election_year=2012, election_type="VIDHAN_SABHA", state="Uttarakhand",
                             source_url="f20")
    rows = [_row(87, 535, PART_87), _row(60, 339, PART_60), _row(141, 646, PART_141)]
    rows += [_row(200 + i, 22, [2] + [1] * 20 + [0]) for i in range(3)] + list(extra)   # untied fillers
    repo.replace_results(db, election=e, ac_number=19, rows=rows)
    db.commit()
    return {r.part_number: r for r in bi.all_result_views(db)}


# --- Form 20 ------------------------------------------------------------------
def test_row_check_rules():
    assert bi.form20_row_check(PART_87, 535, 22)[0] == "ok"
    status, note = bi.form20_row_check(PART_141, 646, 22)
    assert status == "check_failed" and "cannot be established safely" in note
    assert bi.form20_row_check([1, 2], 4, None)[0] == "check_failed"
    assert bi.form20_row_check(None, 5, 22)[0] == "not_backfilled"
    assert bi.expected_column_count([22, 22, 21]) == 22 and bi.expected_column_count([3, 2]) is None


def test_verified_plus_review_equals_total_and_part_141_is_flagged(db):
    views = _sheet(db)
    assert views[87].verified and views[87].margin is not None
    assert not views[141].verified and views[141].margin is None
    s = bi.summarize_results(list(views.values()))[0]
    assert (s.booths, s.sum_check_pass, s.needs_review, s.sum_check_fail) == (6, 5, 1, [141])
    q = bi.form20_quality_summary(list(views.values()))
    assert q["verified"] + q["review"] == q["rows"] == 6 and q["review_parts"] == [141]


def test_part_87_review_status_follows_its_own_arithmetic(db):
    views = _sheet(db, extra=[_row(88, 535, PART_87[:-1] + [1])])     # one misread digit
    assert views[87].verified
    assert not views[88].verified and views[88].reliability == "check_failed"


def test_part_60_zero_margin_is_counted_only_on_verified_rows(db):
    tied_but_broken = _row(300, 23, [11, 11] + [0] * 20)             # sums to 22, not 23
    views = _sheet(db, extra=[tied_but_broken])
    m = views[60].margin
    assert (m.leader_votes, m.runner_up_votes, m.margin) == (140, 140, 0)
    assert bi.summarize_results(list(views.values()))[0].zero_margin == 1


def test_stored_flag_is_not_trusted_and_null_is_handled(db):
    views = _sheet(db, extra=[_row(301, 23, [1] * 22, flag=True),      # flag says ok, arithmetic says no
                              _row(302, 22, [1] * 22, flag=None),      # no flag stored, arithmetic ok
                              _row(303, 5, None, flag=None)])          # no columns at all
    assert not views[301].verified and views[302].verified and not views[303].verified
    q = bi.form20_quality_summary(list(views.values()))
    assert q["verified"] + q["review"] == q["rows"] and q["review_parts"] == [141, 301, 303]


def test_result_detail_uses_the_same_rule_as_the_summary(db):
    views = _sheet(db)
    detail, _ = bi.result_detail(db, views[141].id)
    assert not detail.verified and detail.reliability_note == views[141].reliability_note


# --- Mapping ------------------------------------------------------------------
@pytest.fixture
def world(db):
    state = repo.upsert_state(db, state_code="S28", state_name="Uttarakhand", state_name_local=None,
                              state_type="ST", external_id=27, source="t", source_url="u")
    d = repo.upsert_district(db, state=state, district_code="S2813", district_number=13,
                             district_name="Dehradun", district_name_local=None, source="t", source_url="u")
    cur = repo.upsert_ac(db, district=d, ac_number=19, ac_name="Raipur", source="t")
    old = repo.upsert_ac(db, district=d, ac_number=15, ac_name="राजपुर", delimitation="2003", source="t")
    repo.upsert_polling_station(db, ac=cur, part_number=1, edition="SIR-2026", source="t",
                                polling_station_name_local="अस्थल राजकीय प्राथमिक विद्यालय",
                                area_description="1- अस्थल 2- बझैत")
    repo.upsert_polling_station(db, ac=cur, part_number=8, edition="SIR-2026", source="t",
                                polling_station_name_local="काला गांव राजकीय प्राथमिक विद्यालय",
                                area_description="1- गुजरमी 2- जगतखाना")
    for p in (6, 7, 8, 9):
        repo.upsert_polling_station(db, ac=old, part_number=p, edition="ROLL-2003", source="t")
    for part, area, to_part, building in (
            (6, "2 बझैत", 1, "राजकीय प्राथमिक विद्यालय अस्थल"),
            (7, "3 गुजरमी", 6, "राजकीय प्राथमिक विद्यालय काला गांव"),
            (7, "9 अज्ञातपुर", 9, "पंचायत घर डांडा लखौंड"),
            (8, "1 डांडा खुदानेवाला", None, None)):
        repo.upsert_part_mapping(db, **_mapping(part, area, to_part, building))
    db.commit()
    return {"cur": cur, "old": old}


def _mapping(part, area, to_part, building):
    return dict(from_edition="ROLL-2003", from_ac_number=15, from_ac_name="राजपुर", from_part_number=part,
                to_edition="ROLL-2025", to_ac_number=19, to_ac_name="रायपुर", to_part_number=to_part,
                to_part_name=building, area_name=area, mapping_method="official_ceo_uk_village_mapping",
                confidence=0.95, source="t")


def test_mapping_summary_equals_table_rows(db, world):
    _, _, rows, counts = bi.mapping_table(db, world["cur"].id)
    assert counts == {"Verified": 2, "Review Required": 1, "Not Mapped": 2}   # part 9: no mapping ingested
    assert sum(counts.values()) == len(rows)
    assert counts == {k: sum(1 for r in rows if r.status == k) for k in bi.MAPPING_STATUSES}


def test_duplicate_mapping_record_does_not_inflate_counts(db, world):
    before = bi.mapping_table(db, world["cur"].id)[3]
    db.add(PartMapping(**_mapping(6, "2 बझैत", 1, "राजकीय प्राथमिक विद्यालय अस्थल")))
    db.add(PartMapping(**_mapping(6, "2  बझैत", 1, "राजकीय प्राथमिक विद्यालय अस्थल")))   # spacing variant
    db.commit()
    _, _, rows, after = bi.mapping_table(db, world["cur"].id)
    assert after == before and sum(after.values()) == len(rows)


def test_every_page_uses_the_same_mapping_counts(db, world):
    counts = bi.mapping_table(db, world["cur"].id)[3]
    assert bi.quality_overview(db, "t")["mapping"] == counts
    av = bi.all_availability(db, [world["cur"].id])[0]
    o = bi.ac_overview(db, world["cur"].id)
    assert (av.mapping_verified, av.mapping_review) == (counts["Verified"], counts["Review Required"])
    assert (o.mapping_verified, o.mapping_review) == (counts["Verified"], counts["Review Required"])


def test_null_or_unknown_mapping_status_is_review(db, world, monkeypatch):
    for s in (None, "", "review", "something-else"):
        assert bi.mapping_status_label(s) == "Review Required"
    assert bi.mapping_status_label("verified") == "Verified"
    assert bi.mapping_status_label("unmapped") == "Not Mapped"
    views = bi.evaluate_mappings(db)
    for v in views:
        if v.status == "verified":
            v.status = None                       # a verified row losing its status is not verified
    monkeypatch.setattr(bi, "evaluate_mappings", lambda _db: views)
    counts = bi.mapping_table(db, world["cur"].id)[3]
    assert counts["Verified"] == 0 and counts["Review Required"] == 3


def test_ocr_evidence_only_suggests(db, world, monkeypatch):
    monkeypatch.setattr(bi, "load_ps_list_2024_rows", lambda ac_number: [{"serial": 9}])
    monkeypatch.setattr(bi, "bridge_via_2024_list",
                        lambda *a, **k: bi.BridgeDecision(8, "Possible link from the 2024 list (OCR)."))
    row = next(m for m in bi.evaluate_mappings(db) if m.area_name == "9 अज्ञातपुर")
    assert row.status == "review" and [s["part_number"] for s in row.suggestions] == [8]


def test_historical_and_current_records_are_not_merged(db, world):
    e = repo.upsert_election(db, election_year=2012, election_type="VIDHAN_SABHA", state="Uttarakhand",
                             source_url="f20")
    repo.replace_results(db, election=e, ac_number=15, rows=[_row(6, 22, [1] * 22)])
    db.commit()
    assert bi.results_for_ac(db, world["old"]) == []
    assert bi.results_for_ac(db, world["cur"]) == []          # AC 15's rows never join AC 19
    _, _, rows, counts = bi.mapping_table(db, world["old"].id)
    assert sum(counts.values()) == len(rows) and all(r.hist_ac_number == 15 for r in rows)
