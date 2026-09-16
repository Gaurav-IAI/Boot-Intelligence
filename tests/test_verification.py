"""Offline verification and review outputs produced by the single pipeline."""
import csv
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import repositories as repo
from app.database.models import Base, ElectionResult, Elector
from app.services.review import write_review
from app.services.verification import check_roll_counts, verify_all


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(db):
    state = repo.upsert_state(db, state_code="S28", state_name="Uttarakhand", state_name_local=None,
                              state_type="ST", external_id=27, source="t", source_url="u")
    d = repo.upsert_district(db, state=state, district_code="S2813", district_number=13,
                             district_name="Dehradun", district_name_local=None,
                             source="t", source_url="u")
    repo.upsert_ac(db, district=d, ac_number=19, ac_name="Raipur", source="t")
    old = repo.upsert_ac(db, district=d, ac_number=15, ac_name="राजपुर", delimitation="2003", source="t")
    st = repo.upsert_polling_station(db, ac=old, part_number=10, edition="ROLL-2003", source="t")
    roll = repo.upsert_roll(db, station=st, roll_year=2003, roll_type="Final", language="HIN",
                            source="t", extraction_status="extracted", extracted_elector_count=5)
    base = dict(source="t", source_document="P0010.pdf", source_page=3, elector_name="नाम",
                extraction_confidence=1.0)
    repo.replace_electors(db, roll=roll, rows=[
        dict(serial_number=1, **base),
        dict(serial_number=2, is_valid=False, duplicate_kind="same_name_relative_age",
             validation_notes="same name+relative+age as serial 1", **base),
        dict(serial_number=2, is_valid=False, duplicate_kind="same_part_and_serial",
             validation_notes="duplicate serial (first seen at row 2)", **base),
        dict(serial_number=3, is_valid=False, duplicate_kind="same_name_relative_age",
             validation_notes="same name+relative+age as serial 99", **base),
        dict(serial_number=4, **{**base, "extraction_confidence": 0.8}),
    ])
    e = repo.upsert_election(db, election_year=2012, election_type="VIDHAN_SABHA",
                             state="Uttarakhand", source_url="u")
    repo.replace_results(db, election=e, ac_number=19, rows=[
        dict(part_number=1, total_valid_votes=10, total_votes=10, source="t",
             candidate_votes_json="[6, 4]", vote_sum_matches=True),
        dict(part_number=87, total_valid_votes=0, total_votes=1, source="t",
             candidate_votes_json="[5, 535]", vote_sum_matches=False)])
    repo.replace_results(db, election=e, ac_number=55, rows=[
        dict(part_number=1, total_valid_votes=5, total_votes=5, source="t")])
    db.commit()
    return roll


def test_duplicates_are_linked_to_the_record_they_repeat(db, world):
    v = verify_all(db)
    assert (v.duplicates_linked, v.duplicates_unresolved) == (2, 1)
    by_serial = {}
    for e in db.query(Elector).order_by(Elector.id):
        by_serial.setdefault(e.serial_number, []).append(e)
    first, dup_fuzzy, dup_exact = by_serial[1][0], *by_serial[2]
    assert dup_fuzzy.duplicate_of_id == first.id
    assert dup_exact.duplicate_of_id == dup_fuzzy.id
    assert by_serial[3][0].duplicate_of_id is None
    again = verify_all(db)                                       # idempotent
    assert (again.duplicates_linked, again.duplicates_unresolved) == (0, 1)


def test_results_are_linked_only_to_an_existing_current_ac(db, world):
    v = verify_all(db)
    assert (v.results_linked, v.results_unlinked) == (2, 1)
    assert db.query(ElectionResult).filter(ElectionResult.ac_number == 55).one().ac_id is None


def test_roll_count_mismatch_is_reported_not_repaired(db, world):
    world.extracted_elector_count = 9
    db.commit()
    assert check_roll_counts(db) == ["part 10: 5 rows stored but 9 extracted"]
    assert db.query(Elector).count() == 5


def test_review_files_list_what_needs_a_decision_without_names(db, world, tmp_path):
    verify_all(db)
    res = write_review(db, tmp_path, backend="sqlite")
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {"mapping_verification.csv", "record_review.csv", "result_review.csv",
                     "quality_report.json"}
    with (tmp_path / "record_review.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert not {"name", "elector_name", "relative_name"} & set(rows[0].keys())
    assert [(r["serial"], r["issue"]) for r in rows] == [
        ("2", "duplicate"), ("2", "duplicate"), ("3", "duplicate"), ("4", "low_confidence")]
    assert rows[0]["duplicate_of_serial"] == "1" and rows[0]["source_page"] == "3"
    with (tmp_path / "result_review.csv").open(encoding="utf-8-sig") as f:
        results = list(csv.DictReader(f))
    assert {(r["ac"], r["part"]) for r in results} == {("19", "87"), ("55", "1")}
    report = json.loads((tmp_path / "quality_report.json").read_text(encoding="utf-8"))
    assert report["review_queue"] == {"mapping_rows": 0, "records": 4, "results": 2}
    assert report["records"]["duplicates"] == 3 and report["rolls"][0]["status"] == "review"
    assert res["queue"]["records"] == 4
