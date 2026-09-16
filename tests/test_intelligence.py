"""Booth Intelligence calculations.

Pure rules are tested directly; database-backed services run on in-memory
SQLite, like tests/test_database.py. The linkage tests encode the two integrity
rules the dashboard depends on: Form 20 results are never joined to a station
by part number, and official mappings are verified before being used as links.
"""
import json

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.analytics import intelligence as bi
from app.database import repositories as repo
from app.database.migrations import apply_additive_migrations
from app.database.models import Base

SIR1 = "राजकीय प्राथमिक विद्यालय 1- अस्थल 2- बझैत"
SIR20 = "रोड राजीव गांधी नवोदय 2 ननूरखेडा विद्यालय क-न- 1- आमवाला मझला 2 धोबी घाट मंगलूवाला"


# --------------------------------------------------------------------------
# Pure calculations
# --------------------------------------------------------------------------
class TestMargin:
    def test_top_two_columns_regardless_of_position(self):
        m = bi.compute_margin([84, 91, 1, 111, 0, 3], 290, True)
        assert (m.leader_votes, m.runner_up_votes, m.margin) == (111, 91, 20)
        assert m.margin_pct == pytest.approx(6.9, abs=0.01)
        assert m.leader_share == pytest.approx(38.28, abs=0.01)
        assert m.columns_with_votes == 5

    def test_no_margin_when_the_vote_sum_check_failed(self):
        assert bi.compute_margin([535, 271, 188], 0, False) is None
        assert bi.compute_margin([10, 5], 15, None) is None

    def test_no_margin_without_two_columns(self):
        assert bi.compute_margin([10], 10, True) is None
        assert bi.compute_margin(None, 10, True) is None

    def test_tied_booth_has_zero_margin(self):
        assert bi.compute_margin([50, 50, 1], 101, True).margin == 0


class TestChange:
    def test_observed_change(self):
        c = bi.compute_change(742, 1018)
        assert c.available and c.delta == 276 and c.delta_pct == pytest.approx(37.2, abs=0.01)

    def test_missing_side_is_unavailable_not_zero(self):
        c = bi.compute_change(342, None)
        assert not c.available and c.delta is None and c.delta_pct is None


class TestPlaceMatching:
    def test_village_listed_at_station(self):
        assert bi.place_found_in("2 बझैत", SIR1)

    def test_zero_width_joiner_variants_match(self):
        assert bi.place_found_in("3 अस्‍थल", SIR1)

    def test_every_token_must_match(self):
        # "आमवाला" is at the station but "तरला" is not: not a match
        assert not bi.place_found_in("4 आमवाला तरला", SIR20)

    def test_spacing_differences_are_ignored(self):
        assert bi.place_found_in("डांडा खुदानेवाला", "1- डांडा खुदाने वाला")

    def test_empty_place_never_matches(self):
        assert not bi.place_found_in("12", SIR1)
        assert not bi.place_found_in(None, SIR1)

    def test_spelling_variants_match(self):
        assert bi.place_found_in("2 कालागाव", "1- काला गांव 2- गुजरमी")
        assert bi.place_found_in("1 रैनी वाला", "2- पुस्ताड़ी 3- रैनीवाला")


class TestMappingDecision:
    STATIONS = {1: SIR1, 6: "नागल राजकीय प्राथमिक विद्यालय 1- तरला नागल",
                8: "राजकीय प्राथमिक विद्यालय 1- काला गांव 2- गुजरमी",
                74: "दून वल्र्ड स्कूल रक्षा विहार 1- रक्षा विहार",
                80: "सिंह राजकीय प्राथमिक विद्यालय 1- खेरीमानसिंह 2- पुस्ताड़ी 3- रैनीवाला"}

    def test_village_and_building_agree_at_the_same_number(self):
        d = bi.decide_mapping("2 बझैत", 1, "राजकीय प्राथमिक विद्यालय अस्‍थल", self.STATIONS)
        assert (d.status, d.station_part) == ("verified", 1)

    def test_renumbered_booth_is_linked_to_its_current_number(self):
        d = bi.decide_mapping("1 रैनी वाला", 74, "राजकीय प्राथमिक विद्यालय खैरी मानसिंह", self.STATIONS)
        assert (d.status, d.station_part) == ("verified", 80)
        assert "Renumbered" in d.reason

    def test_generic_building_name_is_not_evidence(self):
        d = bi.decide_mapping("3 गुजरमी", 6, "राजकीय प्राथमिक विद्यालय काला गांव", self.STATIONS)
        assert d.status == "review" and d.candidates == [8] and "generic" in d.reason

    def test_one_signal_alone_never_verifies(self):
        # village is listed at Part 8 but the building name points elsewhere
        d = bi.decide_mapping("3 गुजरमी", 6, "राजकीय प्राथमिक विद्यालय अस्थल", self.STATIONS)
        assert d.status == "review" and d.station_part is None

    def test_ambiguous_matches_stay_in_review(self):
        stations = {19: "राजीव गांधी नवोदय 1 1- मंगलूवाला", 20: "राजीव गांधी नवोदय 2 1- मंगलूवाला"}
        d = bi.decide_mapping("8 मंगलूवाला", 17, "राजीव गांधी नवोदय विद्यालय", stations)
        assert d.status == "review" and d.candidates == [19, 20]

    def test_locality_phrase_of_the_building_name_counts_as_whole_words(self):
        names = {1: "अस्थल राजकीय प्राथमिक विद्यालय", 6: "तरला नागल राजकीय प्राथमिक विद्यालय क-न- 1",
                 8: "काला गांव राजकीय प्राथमिक विद्यालय"}
        d = bi.decide_mapping("3 गुजरमी", 6, "राजकीय प्राथमिक विद्यालय काला गांव", self.STATIONS, names)
        assert (d.status, d.station_part) == ("verified", 8)
        assert "Renumbered" in d.reason

    def test_locality_phrase_never_matches_inside_another_word(self):
        names = {8: "कालोनी गांव राजकीय प्राथमिक विद्यालय"}
        d = bi.decide_mapping("3 गुजरमी", 6, "राजकीय प्राथमिक विद्यालय काला गांव", self.STATIONS, names)
        assert d.status == "review"
        assert bi.building_phrase_in_name("राजकीय प्राथमिक विद्यालय", "x") is None

    def test_village_must_be_in_the_areas_served_not_the_station_name(self):
        # "नालापानी" is the locality of several booths; "तपोवन" is only in one booth's areas.
        texts = {16: "नालापानी राजकीय इण्टर कालेज क-न- 1 1- ननूर खेडा तपोवन 2- तपोवन रोड",
                 18: "नालापानी राजकीय इण्टर कालेज क-न- 2 1- सपेरा बस्ती 2- नालापानी"}
        names = {16: "नालापानी राजकीय इण्टर कालेज क-न- 1", 18: "नालापानी राजकीय इण्टर कालेज क-न- 2"}
        areas = {16: "1- ननूर खेडा तपोवन 2- तपोवन रोड", 18: "1- सपेरा बस्ती 2- नालापानी"}
        building = "राजकीय इण्टर कालेज नालापानी"
        loose = bi.decide_mapping("2 नालापानी तपोवन", 14, building, texts, names)
        strict = bi.decide_mapping("2 नालापानी तपोवन", 14, building, texts, names, areas)
        assert loose.status == "verified"          # what the looser rule would have claimed
        assert strict.status == "review" and strict.station_part is None

    def test_no_2025_part_is_unmapped(self):
        assert bi.decide_mapping("1 तपोवन", None, None, self.STATIONS).status == "unmapped"


class TestBridgeVia2024List:
    STATIONS = {
        17: ("नालापानी रायपुर ब्लाक सभागार", "1- नालापानी रोड 2- देवलोक कालौनी 3- दीप लोक कालौनी"),
        18: ("नालापानी राजकीय इण्टर कालेज क-न- 2", "1- सपेरा बस्ती 2- नालापानी"),
        22: ("ननूरखेडा राजकीय इण्टर कालेज क-न- 3", "1- हरचावाला 2- ननुरखेड़ा 3- अपर आमवाला"),
        159: ("रायपुर चक राजकीय प्राथमिक विद्यालय क-न- 2", "1- शिवलोक कालोनी"),
    }
    ROWS = [
        {"serial": 13, "locality": "नालापानी", "building": "रायपुर ब्लाक सभागार",
         "areas": "1. नालापानी रोड 2. देवलोक कालौनी 3. दीप लोक कालौनी"},
        {"serial": 18, "locality": "नालापानी", "building": "राजकीय इण्टर कालेज क.न. 3",
         "areas": "1. हरचावाला 2. ननुरखेड़ा 3. अपर आमवाला"},
    ]

    def test_row_with_official_building_links_to_the_unique_current_station(self):
        b = bi.bridge_via_2024_list(13, "रायपुर ब्लाक सभागार नालापानी", self.ROWS, self.STATIONS)
        assert b.station_part == 17 and "Polling Station List 2024" in b.reason

    def test_room_number_must_agree(self):
        b = bi.bridge_via_2024_list(18, "राजकीय इण्टर कालेज नालापानी", self.ROWS, self.STATIONS)
        assert b.station_part == 22                     # क.न. 3, never the क-न- 2 station

    def test_numbering_that_disagrees_with_the_official_building_is_not_used(self):
        # 2024 row 13 is a block hall; the mapping says 2025 Part 13 is a school -> no link
        b = bi.bridge_via_2024_list(13, "राजकीय प्राथमिक विद्यालय खैरी मानसिंह", self.ROWS, self.STATIONS)
        assert b.station_part is None

    def test_ambiguous_current_match_is_not_linked(self):
        twins = {17: self.STATIONS[17], 170: self.STATIONS[17]}
        b = bi.bridge_via_2024_list(13, "रायपुर ब्लाक सभागार नालापानी", self.ROWS, twins)
        assert b.station_part is None and "not clear" in b.reason

    def test_room_number_parsing(self):
        assert bi.room_number("राजकीय इण्टर कालेज क.न. 2") == "2"
        assert bi.room_number("नवोदय विद्यालय क-नं- 3") == "3"
        assert bi.room_number("क0न0 1 मानपुर") == "1"
        assert bi.room_number("रायपुर ब्लाक सभागार") is None


def _agg(**kw):
    base = dict(total=342, male=175, female=167, with_epic=212, invalid=0, duplicates=0,
                other_invalid=0, low_conf=6, mean_confidence=0.9987, provenance_complete=342)
    base.update(kw)
    return bi.RecordAggregate(**base)


class TestAggregate:
    def test_derived_properties(self):
        a = _agg()
        assert a.other_unknown == 0
        assert a.epic_pct == pytest.approx(61.99, abs=0.01)
        assert a.provenance_pct == 100.0

    def test_add_weights_mean_confidence_by_record_count(self):
        a = bi.RecordAggregate(total=100, male=50, female=50, mean_confidence=1.0)
        a.add(bi.RecordAggregate(total=300, male=100, female=199, mean_confidence=0.9))
        assert a.total == 400 and a.other_unknown == 1
        assert a.mean_confidence == pytest.approx(0.925)

    def test_empty_aggregate_has_no_percentages(self):
        a = bi.RecordAggregate()
        assert a.epic_pct is None and a.provenance_pct is None


class TestFlags:
    def _flags(self, agg, **kw):
        args = dict(edition="ROLL-2003", mapping_status="none", mapping_note="n",
                    historical_status="none", historical_note="h")
        args.update(kw)
        return bi.booth_flags(agg, **args)

    def test_clean_booth(self):
        codes = [f.code for f in self._flags(_agg())]
        assert "CLEAN" in codes and "DATA_QUALITY" not in codes

    def test_duplicates_raise_data_quality_with_reason(self):
        flags = self._flags(_agg(total=1276, invalid=46, duplicates=45, other_invalid=1, low_conf=42),
                            part_number=10, serial_outliers=[2220])
        dq = next(f for f in flags if f.code == "DATA_QUALITY")
        assert "45 duplicate" in dq.reason and "Part 10" in dq.reason
        assert "CLEAN" not in [f.code for f in flags]
        assert "SOURCE_ANOMALY" in [f.code for f in flags]

    def test_low_confidence(self):
        codes = [f.code for f in self._flags(_agg(mean_confidence=0.95, low_conf=100))]
        assert "LOW_CONFIDENCE" in codes and "CLEAN" not in codes

    def test_no_records_on_current_edition(self):
        flags = self._flags(None, edition="SIR-2026", historical_status="unlinked")
        codes = [f.code for f in flags]
        assert "NO_ELECTORATE" in codes and "HISTORICAL_UNLINKED" in codes
        assert "not available from the current public source" in flags[0].reason


class TestHealth:
    def test_clean_booth_with_verified_mapping(self):
        h = bi.booth_health(_agg(), station_has_provenance=True, mapping_status="verified",
                            historical_linked=False)
        scores = {c.name: c.score for c in h.components}
        assert scores["Electorate data"] == 20 and scores["Record validation"] == 20
        assert scores["Extraction confidence"] == pytest.approx(13.7, abs=0.05)
        assert scores["Historical result link"] == 0
        assert h.total == 84 and h.band == "Strong"
        assert sum(c.max for c in h.components) == 100

    def test_flags_reduce_the_validation_component(self):
        h = bi.booth_health(_agg(total=1276, invalid=46, low_conf=42, provenance_complete=1276),
                            station_has_provenance=True, mapping_status="none",
                            historical_linked=False)
        q = next(c for c in h.components if c.name == "Record validation")
        assert q.score == pytest.approx(20 * (1 - 10 * 46 / 1276), abs=0.05)
        assert h.band == "Partial"

    def test_booth_without_records_cannot_score_high(self):
        h = bi.booth_health(None, station_has_provenance=True, mapping_status="verified",
                            historical_linked=False)
        assert h.total == 30 and h.band == "Limited"

    def test_score_bounds(self):
        h = bi.booth_health(_agg(invalid=342, low_conf=342, provenance_complete=0),
                            station_has_provenance=False, mapping_status="none",
                            historical_linked=False)
        assert 0 <= h.total <= 100
        assert all(0 <= c.score <= c.max for c in h.components)


def test_margin_histogram_counts_every_booth():
    bins = bi.margin_histogram([0.5, 4.9, 5.0, 12, 49.9, 50, 100])
    assert sum(b["count"] for b in bins) == 7
    assert bins[0]["count"] == 2 and bins[-1]["count"] == 2


# --------------------------------------------------------------------------
# Database-backed services
# --------------------------------------------------------------------------
@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(db):
    """AC 19 (current) with SIR-2026 booths, AC 15 (2003) with one rolled part,
    an official mapping, and 2012 Form 20 results for AC 19."""
    state = repo.upsert_state(db, state_code="S28", state_name="Uttarakhand",
                              state_name_local=None, state_type="ST", external_id=27,
                              source="test", source_url="u")
    dist = repo.upsert_district(db, state=state, district_code="S2813", district_number=13,
                                district_name="Dehradun", district_name_local="देहरादून",
                                source="test", source_url="u")
    cur = repo.upsert_ac(db, district=dist, ac_number=19, ac_name="Raipur",
                         delimitation="current", source="test")
    chakrata = repo.upsert_ac(db, district=dist, ac_number=15, ac_name="Chakrata",
                              delimitation="current", source="test")
    old = repo.upsert_ac(db, district=dist, ac_number=15, ac_name="राजपुर",
                         delimitation="2003", source="test")
    s1 = repo.upsert_polling_station(db, ac=cur, part_number=1, edition="SIR-2026", source="test",
                                     source_url="u", polling_station_name="Asthal",
                                     polling_station_name_local=SIR1,
                                     area_description="1- अस्थल 2- बझैत")
    s6 = repo.upsert_polling_station(db, ac=cur, part_number=6, edition="SIR-2026", source="test",
                                     source_url="u", polling_station_name="Tarla Nagal",
                                     polling_station_name_local="नागल 1- तरला नागल",
                                     area_description="1- तरला नागल")
    s8 = repo.upsert_polling_station(db, ac=cur, part_number=8, edition="SIR-2026", source="test",
                                     source_url="u", polling_station_name="Kala Gaon",
                                     polling_station_name_local="1- गुजरमी 2- जगतखाना",
                                     area_description="1- गुजरमी 2- जगतखाना")
    p6 = repo.upsert_polling_station(db, ac=old, part_number=6, edition="ROLL-2003",
                                     source="test", polling_station_name="प्राथमिक स्कूल अस्थल")
    roll = repo.upsert_roll(db, station=p6, roll_year=2003, roll_type="Final", language="HIN",
                            source="test", official_elector_count=4, extracted_elector_count=4,
                            extraction_status="extracted")
    rows = []
    for i, (g, epic, valid, dup) in enumerate(
            [("M", "ABC1234567", True, None), ("F", None, True, None),
             ("F", "ABC7654321", False, "same_name_relative_age"), ("UNKNOWN", None, False, None)], 1):
        rows.append(dict(serial_number=i, elector_name=f"x{i}", gender=g, epic_number=epic,
                         age=30, is_valid=valid, duplicate_kind=dup, source="test",
                         source_url="u", source_document="P0006.pdf", source_page=1,
                         raw_text="r", parser_version="p", extraction_method="text",
                         extraction_confidence=1.0 if i < 4 else 0.5))
    repo.replace_electors(db, roll=roll, rows=rows)
    for to_part, area, building in ((1, "2 बझैत", "राजकीय प्राथमिक विद्यालय अस्थल"),
                                    (6, "3 गुजरमी", "राजकीय प्राथमिक विद्यालय काला गांव")):
        repo.upsert_part_mapping(
            db, from_edition="ROLL-2003", from_ac_number=15, from_ac_name="राजपुर",
            from_part_number=6, to_edition="ROLL-2025", to_ac_number=19, to_ac_name="रायपुर",
            to_part_number=to_part, to_part_name=building, area_name=area,
            mapping_method="official_ceo_uk_village_mapping", confidence=0.95, source="test")
    election = repo.upsert_election(db, election_year=2012, election_type="VIDHAN_SABHA",
                                    state="Uttarakhand", source_url="f20")
    repo.replace_results(db, election=election, ac_number=19, rows=[
        dict(part_number=1, polling_station_name="अस्थल", total_valid_votes=290, total_votes=290,
             tendered_votes=0, candidate_votes_json=json.dumps([84, 91, 115]),
             vote_sum_matches=True, source="test"),
        dict(part_number=6, polling_station_name="डांडा लखौण्ड", total_valid_votes=0, total_votes=1,
             tendered_votes=535, candidate_votes_json=json.dumps([5, 535]),
             vote_sum_matches=False, source="test"),
    ])
    db.commit()
    return dict(cur=cur, chakrata=chakrata, old=old, s1=s1, s6=s6, s8=s8, p6=p6)


class TestRecordAggregates:
    def test_gender_epic_validation_counts(self, db, world):
        a = bi.record_aggregates(db)[world["p6"].id]
        assert (a.total, a.male, a.female, a.other_unknown) == (4, 1, 2, 1)
        assert a.with_epic == 2 and a.epic_pct == 50.0
        assert (a.invalid, a.duplicates, a.other_invalid) == (2, 1, 1)
        assert a.low_conf == 1 and a.mean_confidence == pytest.approx(0.875)
        assert a.provenance_complete == 4


class TestMappingVerification:
    def test_rows_are_verified_or_flagged_with_name_suggestions(self, db, world):
        maps = {m.area_name: m for m in bi.evaluate_mappings(db)}
        ok, bad = maps["2 बझैत"], maps["3 गुजरमी"]
        assert ok.status == "verified" and ok.linked_station_id == world["s1"].id
        assert bad.status == "review" and bad.linked_station_id is None
        assert bad.same_number_station_id == world["s6"].id
        assert [s["station_id"] for s in bad.suggestions] == [world["s8"].id]
        assert ok.from_station_id == world["p6"].id


class TestHistoricalLinkage:
    def test_form20_is_never_attached_to_a_2003_ac_with_a_shared_number(self, db, world):
        assert bi.results_for_ac(db, world["old"]) == []
        assert bi.results_for_ac(db, world["chakrata"]) == []
        assert len(bi.results_for_ac(db, world["cur"])) == 2

    def test_station_is_not_given_a_result_by_part_number(self, db, world):
        d = bi.booth_detail(db, world["s1"].id)
        assert d.row.historical_status == "unlinked"
        assert "part number" in d.row.historical_note
        assert not d.row.health.components[4].score

    def test_margin_only_where_arithmetic_verifies(self, db, world):
        results = {r.part_number: r for r in bi.results_for_ac(db, world["cur"])}
        assert results[1].margin.margin == 24 and results[1].reliability == "ok"
        assert results[6].margin is None and results[6].reliability == "check_failed"
        summary = bi.summarize_results(list(results.values()))[0]
        assert summary.sum_check_fail == [6] and summary.booths_with_margin == 1


class TestServices:
    def test_ac_overview_for_current_ac_without_rolls(self, db, world):
        o = bi.ac_overview(db, world["cur"].id)
        assert o.stations == 3 and o.totals.total == 0 and o.stations_with_roll == 0
        assert o.coverage_pct == 0.0
        assert o.mapping_verified == 1 and o.mapping_review == 1
        s1 = next(r for r in o.rows if r.station_id == world["s1"].id)
        s8 = next(r for r in o.rows if r.station_id == world["s8"].id)
        assert s1.mapping_status == "verified" and s8.mapping_status == "review"

    def test_ac_overview_for_2003_ac_aggregates_electorate(self, db, world):
        o = bi.ac_overview(db, world["old"].id)
        assert o.totals.total == 4 and o.stations_with_roll == 1
        assert o.elections == []
        assert o.rows[0].mapping_status == "verified"

    def test_booth_detail_predecessors_only_through_verified_rows(self, db, world):
        d = bi.booth_detail(db, world["s1"].id)
        assert [p["part_number"] for p in d.predecessors] == [6]
        assert d.predecessors[0]["agg"].total == 4
        assert bi.booth_detail(db, world["s6"].id).predecessors == []

    def test_changes_group_by_2003_part(self, db, world):
        ac, _, groups, summary = bi.ac_changes(db, world["cur"].id)
        assert len(groups) == 1 and groups[0].split
        assert groups[0].electorate.before == 4 and not groups[0].electorate.available
        assert summary["verified"] == 1 and summary["renumbered"] == 1

    def test_search_by_part_number_and_name(self, db, world):
        assert any(h.href == f"/station/{world['s1'].id}" for h in bi.search(db, "1"))
        assert any(h.kind == "Constituency" for h in bi.search(db, "Raipur"))
        assert bi.search(db, "") == []

    def test_quality_overview_alerts(self, db, world):
        data = bi.quality_overview(db, "sqlite-test")
        titles = [a.title for a in data["alerts"]]
        assert "1 result row requires review" in titles
        assert data["totals"].duplicates == 1 and data["mapping"]["Review Required"] == 1
        assert data["form20"]["verified"] + data["form20"]["review"] == data["form20"]["rows"] == 2

    def test_missing_ids_return_none(self, db, world):
        assert bi.ac_overview(db, 9999) is None
        assert bi.booth_detail(db, 9999) is None
        assert bi.result_detail(db, 9999) is None


class TestAvailability:
    def test_current_ac_matrix(self, db, world):
        av = {a.ac.id: a for a in bi.all_availability(db)}
        cur = av[world["cur"].id]
        assert (cur.current_stations, cur.current_roll_records) == (3, 0)
        assert cur.result_records == 2 and cur.result_years == [2012]
        assert cur.mapping_status == "partial"
        assert cur.linked_acs[0]["id"] == world["old"].id
        states = {m["label"]: m["state"] for m in cur.matrix()}
        assert states["Current electoral roll"] == "no" and states["Historical results"] == "yes"

    def test_same_number_ac_in_other_delimitation_gets_nothing(self, db, world):
        av = {a.ac.id: a for a in bi.all_availability(db)}
        assert av[world["chakrata"].id].result_records == 0
        old = av[world["old"].id]
        assert not old.is_current and old.result_records == 0
        assert old.historical_roll_records == 4 and old.roll_years == [2003]
        assert old.historical_parts_with_roll == 1

    def test_state_summary_counts(self, db, world):
        s = bi.state_summary(db)
        assert s["districts"] == 1 and s["acs_current"] == 2 and s["acs_historical"] == 1
        assert s["stations_current"] == 3 and s["acs_with_stations"] == 1
        assert s["district_rows"][0]["has_results"]

    def test_observations_are_computed_and_capped(self, db, world):
        av = next(a for a in bi.all_availability(db) if a.ac.id == world["cur"].id)
        obs = bi.ac_observations(av, bi.summarize_results(bi.results_for_ac(db, world["cur"])))
        assert len(obs) <= 4
        assert obs[0].startswith("2 polling-station result records")
        assert any("1 of 2 result rows" in o for o in obs)
        assert any("not available from the current public source" in o for o in obs)

    def test_zero_margin_is_counted(self):
        r = bi.ResultView(id=1, year=2012, election_type="VS", ac_number=19, part_number=60,
                          station_name=None, total_votes=339, total_valid_votes=339,
                          tendered_votes=0, rejected_votes=None, nota_votes=None,
                          columns=[140, 140, 59], sum_matches=True,
                          margin=bi.compute_margin([140, 140, 59], 339, True), reliability="ok",
                          reliability_note="", source="t", source_url=None, source_file=None,
                          source_page=1, parser_version=None, confidence=1.0)
        s = bi.summarize_results([r])[0]
        assert s.zero_margin == 1 and s.needs_review == 0


class TestMappingTable:
    def test_rows_statuses_and_unmapped_parts(self, db, world):
        repo.upsert_polling_station(db, ac=world["old"], part_number=7, edition="ROLL-2003",
                                    source="test", area_description="गुजराडा")
        db.commit()
        ac, _, rows, counts = bi.mapping_table(db, world["cur"].id)
        assert counts == {"Verified": 1, "Review Required": 1, "Not Mapped": 1}
        verified = next(r for r in rows if r.status == "Verified")
        assert verified.current_station_id == world["s1"].id and verified.current_part == "Part 1"
        review = next(r for r in rows if r.status == "Review Required")
        assert review.current_part == "Possible: Part 8"
        assert next(r for r in rows if r.status == "Not Mapped").hist_part == 7

    def test_missing_ac(self, db, world):
        assert bi.mapping_table(db, 9999) is None


def test_additive_migration_adds_columns_to_an_old_table():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE election_results (id INTEGER PRIMARY KEY, votes INTEGER)"))
    added = apply_additive_migrations(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("election_results")}
    assert {"candidate_votes_json", "vote_sum_matches", "votes"} <= cols
    assert len(added) == 2
    assert apply_additive_migrations(engine) == []       # idempotent
