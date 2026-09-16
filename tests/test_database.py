"""Database tests.

These run against an in-memory SQLite database so they need neither PostgreSQL
nor the network. The production target is PostgreSQL; the schema is the same
SQLAlchemy metadata in both cases.
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.analytics.booth import booth_stats, quality_report
from app.database import repositories as repo
from app.database.models import Base, Elector, ElectoralRoll


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def station(db):
    state = repo.upsert_state(
        db, state_code="S28", state_name="Uttarakhand", state_name_local="उत्तराखंड",
        state_type="ST", external_id=27, source="test", source_url="http://example")
    district = repo.upsert_district(
        db, state=state, district_code="S2813", district_number=13,
        district_name="Dehradun", district_name_local="देहरादून",
        source="test", source_url="http://example")
    ac = repo.upsert_ac(db, district=district, ac_number=19, ac_name="Raipur",
                        source="test")
    return repo.upsert_polling_station(
        db, ac=ac, part_number=6, edition="ROLL-2003", source="test",
        polling_station_name="प्राथमिक स्कूल अस्थल")


def _roll(db, station, **kw):
    return repo.upsert_roll(db, station=station, roll_year=2003, roll_type="Final",
                            language="HIN", source="test", **kw)


def _rows(n, start=1):
    return [dict(serial_number=i, elector_name=f"व्यक्ति{i}",
                 relative_name="पिताजी", relative_type="FATHER",
                 age=20 + (i % 50), gender="M" if i % 2 else "F",
                 epic_number=f"ABC{i:07d}" if i % 2 else None,
                 source="test", source_page=1 + i // 30, raw_text=f"row {i}",
                 extraction_confidence=1.0, extraction_method="text")
            for i in range(start, start + n)]


class TestUpserts:
    def test_state_upsert_is_idempotent(self, db):
        from app.database.models import State
        for _ in range(3):
            repo.upsert_state(db, state_code="S28", state_name="Uttarakhand",
                              state_name_local=None, state_type="ST",
                              external_id=27, source="t", source_url="u")
        assert len(db.scalars(select(State)).all()) == 1

    def test_same_part_in_two_editions_is_two_rows(self, db, station):
        ac = station.constituency
        other = repo.upsert_polling_station(
            db, ac=ac, part_number=6, edition="SIR-2026", source="test")
        assert other.id != station.id

    def test_electors_insert_and_are_linked_to_the_roll(self, db, station):
        roll = _roll(db, station)
        n = repo.replace_electors(db, roll=roll, rows=_rows(50))
        db.commit()
        assert n == 50
        stored = db.scalars(select(Elector)
                            .where(Elector.electoral_roll_id == roll.id)).all()
        assert len(stored) == 50
        assert all(e.polling_station_id == station.id for e in stored)

    def test_reextraction_replaces_rather_than_duplicates(self, db, station):
        roll = _roll(db, station)
        repo.replace_electors(db, roll=roll, rows=_rows(50))
        repo.replace_electors(db, roll=roll, rows=_rows(50))
        db.commit()
        assert db.scalar(select(ElectoralRoll)) is not None
        assert len(db.scalars(select(Elector)).all()) == 50

    def test_provenance_is_persisted(self, db, station):
        roll = _roll(db, station, pdf_url="http://example/p.pdf",
                     local_file_path="/tmp/p.pdf", checksum_sha256="a" * 64,
                     page_count=12, is_text_pdf=True, extraction_method="text",
                     parser_version="roll_2003_geometric/1.0.0")
        repo.replace_electors(db, roll=roll, rows=_rows(5))
        db.commit()
        e = db.scalar(select(Elector))
        assert e.raw_text and e.source_page and e.source == "test"
        assert roll.checksum_sha256 and roll.pdf_url and roll.parser_version


class TestPartMapping:
    def test_one_part_can_map_to_several(self, db):
        from app.database.models import PartMapping
        for to_part, area in ((74, "1 रैनी वाला"), (1, "2 बझैत"), (1, "3 अस्थल")):
            repo.upsert_part_mapping(
                db, from_edition="ROLL-2003", from_ac_number=15,
                from_ac_name="राजपुर", from_part_number=6, from_part_name="6 ...",
                to_edition="ROLL-2025", to_ac_number=19, to_ac_name="रायपुर",
                to_part_number=to_part, to_part_name="x", area_name=area,
                mapping_method="official_ceo_uk_village_mapping", confidence=0.95,
                source="test")
        db.commit()
        rows = db.scalars(select(PartMapping)).all()
        assert len(rows) == 3
        assert {r.to_part_number for r in rows} == {1, 74}

    def test_mapping_always_records_method_and_confidence(self, db):
        m = repo.upsert_part_mapping(
            db, from_edition="ROLL-2003", from_ac_number=15, from_part_number=6,
            to_edition="ROLL-2025", to_part_number=1, area_name="a",
            mapping_method="official_ceo_uk_village_mapping", confidence=0.95,
            source="test")
        assert m.mapping_method and 0 <= m.confidence <= 1


class TestAnalytics:
    def test_booth_stats_report_the_difference_openly(self, db, station):
        roll = _roll(db, station, official_elector_count=60,
                     official_count_basis="highest serial printed",
                     extracted_elector_count=50, extraction_method="text",
                     page_count=12)
        repo.replace_electors(db, roll=roll, rows=_rows(50))
        db.commit()
        s = booth_stats(db, roll)
        assert s.total_electors == 50
        assert s.official_count == 60
        assert s.difference == -10
        assert s.difference_pct == pytest.approx(-16.67, abs=0.01)
        assert s.male + s.female + s.other_unknown == 50
        assert sum(s.age_bands.values()) + s.age_missing == 50

    def test_age_bands_cover_every_record(self, db, station):
        roll = _roll(db, station)
        repo.replace_electors(db, roll=roll, rows=_rows(200))
        db.commit()
        s = booth_stats(db, roll)
        assert set(s.age_bands) == {"18-25", "26-40", "41-60", "61+"}
        assert sum(s.age_bands.values()) + s.age_missing == 200

    def test_quality_report_aggregates(self, db, station):
        roll = _roll(db, station, official_elector_count=50,
                     extracted_elector_count=50)
        repo.replace_electors(db, roll=roll, rows=_rows(50))
        db.commit()
        q = quality_report(db, "sqlite-test")
        assert q.electors == 50
        assert q.extraction_rate == 100.0
        assert q.polling_stations >= 1


class TestContactSeparation:
    def test_electors_table_has_no_contact_columns(self):
        cols = set(Elector.__table__.columns.keys())
        for forbidden in ("mobile", "phone", "mobile_number", "email", "contact"):
            assert forbidden not in cols, (
                "public electoral rolls contain no contact details; such a column "
                "would invite populating it from somewhere it must not come from")

    def test_contact_records_require_consent_fields(self):
        from app.database.models import ContactRecord
        cols = set(ContactRecord.__table__.columns.keys())
        assert {"consent_status", "consent_timestamp", "source"} <= cols
