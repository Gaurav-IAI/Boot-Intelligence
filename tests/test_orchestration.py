"""One-command pipeline orchestration.

No network: an HTTP factory that raises proves which runs would have made a request.
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import repositories as repo
from app.database.models import Base, ElectionResult
from app.services import pipeline as pl


class NoNetwork(Exception):
    pass


def offline():
    raise NoNetwork("network must not be used")


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _populate(db, *, parts=(6, 7, 8, 9, 10), form20_note=True):
    state = repo.upsert_state(db, state_code="S28", state_name="Uttarakhand", state_name_local=None,
                              state_type="ST", external_id=27, source="t", source_url="u")
    d = repo.upsert_district(db, state=state, district_code="S2813", district_number=13,
                             district_name="Dehradun", district_name_local="देहरादून",
                             source="t", source_url="u")
    cur = repo.upsert_ac(db, district=d, ac_number=19, ac_name="Raipur", source="t")
    repo.upsert_polling_station(db, ac=cur, part_number=1, edition="SIR-2026", source="t",
                                polling_station_name="Asthal", polling_station_name_local="अस्थल")
    repo.record_fetch(db, source="t", url="u", ok=True, note=pl.ps_list_parse_note(19, 1))
    old = repo.upsert_ac(db, district=d, ac_number=15, ac_name="राजपुर", delimitation="2003", source="t")
    for p in parts:
        st = repo.upsert_polling_station(db, ac=old, part_number=p, edition="ROLL-2003", source="t")
        roll = repo.upsert_roll(db, station=st, roll_year=2003, roll_type="Final", language="HIN",
                                source="t", extraction_status="extracted", extracted_elector_count=2)
        repo.replace_electors(db, roll=roll, rows=[dict(serial_number=i, source="t") for i in (1, 2)])
    repo.upsert_part_mapping(db, from_edition="ROLL-2003", from_ac_number=15, from_part_number=6,
                             to_edition="ROLL-2025", to_ac_number=19, to_part_number=1,
                             area_name="अस्थल", mapping_method="m", confidence=0.95, source="t")
    repo.record_fetch(db, source="t", url="u", ok=True,
                      note=pl.ps_list_2024_note(19, "status=stored rows=12"))
    for p in parts:
        if p != 6:      # looked up; the source had no mapping for these parts
            repo.record_fetch(db, source="t", url="u", ok=True,
                              note=pl.mapping_attempt_note("राजपुर", p, 0))
    e = repo.upsert_election(db, election_year=2012, election_type="VIDHAN_SABHA",
                             state="Uttarakhand", source_url="u")
    repo.replace_results(db, election=e, ac_number=19, rows=[dict(
        part_number=1, total_valid_votes=10, total_votes=10, source="t",
        candidate_votes_json=json.dumps([6, 4]))])
    if form20_note:
        repo.record_fetch(db, source="t", url="u", ok=True,
                          note=pl.form20_note_prefix(2012, 19) + "status=stored booths=1")
    db.commit()
    return old


NETWORK_STAGES = [s for s in pl.PIPELINE_STAGES if s not in ("verify", "review")]
UK_ONLY = ("Uttarakhand",)        # the fixture holds one state; multi-state planning is tested separately


def _plans(db, cfg=None):
    return {p.name: p for p in pl.plan_pipeline(db, cfg or pl.PipelineConfig(states=UK_ONLY))}


@pytest.fixture
def cfg(tmp_path):
    return pl.PipelineConfig(review_dir=tmp_path / "review", states=UK_ONLY)


def test_empty_database_plans_every_stage(db):
    plans = _plans(db)
    assert list(plans) == list(pl.PIPELINE_STAGES)
    assert all(p.needed for p in plans.values())
    assert plans["rolls"].items == [6, 7, 8, 9, 10]


def test_complete_database_needs_no_request_but_still_verifies_and_reviews(db, cfg):
    _populate(db)
    plans = _plans(db)
    assert not any(plans[s].needed for s in NETWORK_STAGES)
    report = pl.PipelineReport()
    http = pl.run_pipeline(db, report, cfg, http_factory=offline)
    assert http is None
    assert [s.name for s in report.steps] == list(pl.PIPELINE_STAGES)
    assert all(s.ok for s in report.steps)
    assert all(s.detail.startswith("up to date") for s in report.steps if s.name in NETWORK_STAGES)
    assert db.query(ElectionResult).one().ac_id is not None          # verify linked it
    assert (cfg.review_dir / "quality_report.json").exists()


def test_incomplete_roll_is_listed_for_reextraction(db):
    old = _populate(db, parts=(6, 7, 8, 9))
    from app.database.models import ElectoralRoll, PollingStation
    roll = (db.query(ElectoralRoll).join(PollingStation)
            .filter(PollingStation.ac_id == old.id, PollingStation.part_number == 7).one())
    roll.extracted_elector_count = 3          # stored rows no longer match the extraction
    db.commit()
    plan = _plans(db)["rolls"]
    assert plan.needed and plan.items == [7, 10]


def test_parts_never_looked_up_are_planned_for_mapping(db):
    _populate(db)
    from app.database.models import SourceFetch
    db.query(SourceFetch).filter(SourceFetch.note.like("%part=10 %")).delete(synchronize_session=False)
    db.commit()
    plan = _plans(db)["mapping"]
    assert plan.needed and plan.items == [10]


def test_mapping_rows_in_review_plan_the_2024_list_until_it_is_read(db):
    _populate(db)
    from app.database.models import SourceFetch
    db.query(SourceFetch).filter(SourceFetch.note.like("ps-list-2024 %")).delete(synchronize_session=False)
    db.commit()
    plan = _plans(db)["ps-list-2024"]
    assert plan.needed and plan.items == [19]
    repo.record_fetch(db, source="t", url="u", ok=False,
                      note=pl.ps_list_2024_note(19, "status=blocked:not-published"))
    db.commit()
    assert not _plans(db)["ps-list-2024"].needed


def test_village_search_terms_come_from_the_roll_header():
    assert pl.village_search_terms("1 आमवाला तरला, 2 आमवाला शा˓ी पुरम, 3 तपोवन") == ["आमवाला तरला", "तपोवन"]
    assert pl.village_search_terms("1 डांडा खुदानेवाला, 2 डांडा लखोंड") == ["डांडा खुदानेवाला", "डांडा लखोंड"]
    assert pl.village_search_terms(None) == []


def test_results_from_an_older_parser_version_are_reprocessed(db):
    _populate(db, form20_note=False)            # rows exist, but no outcome for this parser version
    plan = _plans(db)["form20"]
    assert plan.needed and plan.items == [2012 * 1000 + 19]


def test_every_configured_constituency_is_planned_and_scope_can_be_limited(db):
    _populate(db)
    from app.database.models import AssemblyConstituency, District
    d = db.query(District).one()
    for n, name in ((20, "Rajpur Road"), (21, "Dehradun Cantonment")):
        repo.upsert_ac(db, district=d, ac_number=n, ac_name=name, source="t")
    db.commit()
    plans = _plans(db)
    assert plans["parts"].items == [20, 21]
    assert plans["form20"].items == [2012020, 2012021]
    limited = _plans(db, pl.PipelineConfig(station_acs=(19, 20), form20_acs=(19,)))
    assert limited["parts"].items == [20] and not limited["form20"].needed


def test_blocked_polling_station_list_is_not_retried_until_the_parser_changes(db):
    _populate(db)
    from app.database.models import District
    d = db.query(District).one()
    ac20 = repo.upsert_ac(db, district=d, ac_number=20, ac_name="Rajpur Road", source="t")
    repo.upsert_polling_station(db, ac=ac20, part_number=1, edition="SIR-2026", source="t")
    db.commit()
    assert _plans(db, pl.PipelineConfig(form20_acs=(19,)))["ps-list"].items == [20]
    repo.record_fetch(db, source="t", url="u", ok=False, note=pl.ps_list_blocked_note(20, "scanned"))
    db.commit()
    plan = _plans(db, pl.PipelineConfig(form20_acs=(19,)))["ps-list"]
    assert not plan.needed and "1 blocked" in plan.reason


def test_disabled_stages_never_run(db, tmp_path):
    cfg = pl.PipelineConfig(include_mapping=False, include_form20=False, review_dir=tmp_path)
    plans = _plans(db, cfg)
    assert plans["mapping"].mode == "disabled" and not plans["mapping"].needed
    report = pl.PipelineReport()
    pl.run_pipeline(db, report, cfg, http_factory=offline, refresh=True)
    by_name = {s.name: s for s in report.steps}
    assert by_name["mapping"].ok and by_name["form20"].ok


def test_a_failing_stage_does_not_stop_the_others(db, cfg):
    report = pl.PipelineReport()
    pl.run_pipeline(db, report, cfg, http_factory=offline)
    by_name = {s.name: s for s in report.steps}
    assert [s.name for s in report.steps] == list(pl.PIPELINE_STAGES)
    assert not any(by_name[s].ok for s in NETWORK_STAGES)
    assert by_name["verify"].ok and by_name["review"].ok       # offline stages still run


def test_blocked_scanned_roll_and_form20_year_are_not_retried(db):
    old = _populate(db)
    from app.database.models import ElectoralRoll, PollingStation
    roll = (db.query(ElectoralRoll).join(PollingStation)
            .filter(PollingStation.ac_id == old.id, PollingStation.part_number == 9).one())
    roll.extraction_status = "blocked_scanned_pdf"
    repo.record_fetch(db, source="t", url="u", ok=False,
                      note=pl.form20_note_prefix(2022, 19) + "status=blocked:scanned")
    db.commit()
    plans = _plans(db, pl.PipelineConfig(form20_years=(2012, 2022)))
    assert not plans["rolls"].needed and "blocked" in plans["rolls"].reason
    assert not plans["form20"].needed and "1 blocked" in plans["form20"].reason
    assert _plans(db, pl.PipelineConfig(form20_years=(2012, 2017)))["form20"].items == [2017019]


def test_refresh_runs_stages_even_when_data_is_complete(db, cfg):
    _populate(db)
    report = pl.PipelineReport()
    pl.run_pipeline(db, report, cfg, http_factory=offline, refresh=True)
    hierarchy = next(s for s in report.steps if s.name == "hierarchy")
    assert not hierarchy.ok and "NoNetwork" in hierarchy.detail
    assert db.query(ElectionResult).count() == 1       # failed refresh deleted nothing


# --- several states -------------------------------------------------------------
def _add_telangana(db):
    """A second state whose AC numbers collide with Uttarakhand's (both have an AC 19)."""
    state = repo.upsert_state(db, state_code="S29", state_name="Telangana", state_name_local=None,
                              state_type="ST", external_id=28, source="t", source_url="u")
    d = repo.upsert_district(db, state=state, district_code="S2901", district_number=1,
                             district_name="Adilabad", district_name_local=None, source="t", source_url="u")
    acs = [repo.upsert_ac(db, district=d, ac_number=n, ac_name=f"TS {n}", source="t") for n in (19, 20)]
    db.commit()
    return acs


def test_every_registry_state_is_planned_for_discovery(db):
    reason = _plans(db, pl.PipelineConfig())["hierarchy"].reason
    assert all(name in reason for name in ("Uttarakhand", "Uttar Pradesh", "Telangana"))


def test_a_missing_state_plans_only_the_hierarchy(db):
    _populate(db)
    _add_telangana(db)
    plans = _plans(db, pl.PipelineConfig())
    assert plans["hierarchy"].needed and plans["hierarchy"].reason.startswith("Uttar Pradesh:")
    assert not any(plans[s].needed for s in NETWORK_STAGES if s != "hierarchy")


def test_booth_stages_never_target_another_states_constituencies(db, cfg):
    _populate(db)
    ts19, _ = _add_telangana(db)
    two = pl.PipelineConfig(states=("Uttarakhand", "Telangana"), review_dir=cfg.review_dir)
    plans = _plans(db, two)
    # Telangana AC 20 has no polling stations, but CEO Uttarakhand is never asked for it
    assert not plans["hierarchy"].needed and not plans["parts"].needed
    assert not plans["ps-list"].needed and not plans["form20"].needed
    assert "Telangana: 1 districts, 2 constituencies" in plans["hierarchy"].reason
    report = pl.PipelineReport()
    assert pl.run_pipeline(db, report, two, http_factory=offline) is None
    linked = db.query(ElectionResult).one().ac_id
    assert linked is not None and linked != ts19.id        # Form 20 joins Uttarakhand's AC 19
