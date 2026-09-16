"""SQLAlchemy models.

Design rules followed throughout:
  * Official codes are stored wherever the source provides them; names are never
    the only identifier.
  * Every row carries provenance (`source`, `source_url`, timestamps) and every
    extracted elector additionally carries the document, page, raw text, parser
    version and a confidence score.
  * Fields absent from the public source stay NULL. No invented columns.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


# --------------------------------------------------------------------------
# Administrative hierarchy
# --------------------------------------------------------------------------
class State(Base, TimestampMixin):
    __tablename__ = "states"

    id: Mapped[int] = mapped_column(primary_key=True)
    state_code: Mapped[str] = mapped_column(String(16), unique=True)   # e.g. "S28"
    state_name: Mapped[str] = mapped_column(String(128))
    state_name_local: Mapped[str | None] = mapped_column(String(128))
    state_type: Mapped[str | None] = mapped_column(String(8))          # ST / UT
    external_id: Mapped[int | None] = mapped_column(Integer)           # ECI stateId
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)

    districts: Mapped[list["District"]] = relationship(back_populates="state")


class District(Base, TimestampMixin):
    __tablename__ = "districts"
    __table_args__ = (UniqueConstraint("state_id", "district_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    state_id: Mapped[int] = mapped_column(ForeignKey("states.id"))
    district_code: Mapped[str] = mapped_column(String(16))             # e.g. "S2813"
    district_number: Mapped[int | None] = mapped_column(Integer)
    district_name: Mapped[str] = mapped_column(String(128))
    district_name_local: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)

    state: Mapped[State] = relationship(back_populates="districts")
    constituencies: Mapped[list["AssemblyConstituency"]] = relationship(back_populates="district")


class AssemblyConstituency(Base, TimestampMixin):
    """An AC in a specific delimitation.

    `delimitation` distinguishes the current (2008-onwards, 70 AC) frame from the
    2003 frame used by the legacy roll. AC numbers are NOT comparable across the two.
    """

    __tablename__ = "assembly_constituencies"
    __table_args__ = (UniqueConstraint("district_id", "ac_number", "delimitation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    district_id: Mapped[int] = mapped_column(ForeignKey("districts.id"))
    ac_number: Mapped[int] = mapped_column(Integer)
    ac_name: Mapped[str] = mapped_column(String(160))
    ac_name_local: Mapped[str | None] = mapped_column(String(160))
    official_code: Mapped[str | None] = mapped_column(String(32))      # ECI acId
    category: Mapped[str | None] = mapped_column(String(16))           # GEN / SC / ST
    pc_number: Mapped[int | None] = mapped_column(Integer)
    delimitation: Mapped[str] = mapped_column(String(16), default="current")
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)

    district: Mapped[District] = relationship(back_populates="constituencies")
    polling_stations: Mapped[list["PollingStation"]] = relationship(back_populates="constituency")


class PollingStation(Base, TimestampMixin):
    """A Part / polling station within an AC, for a given roll edition."""

    __tablename__ = "polling_stations"
    __table_args__ = (UniqueConstraint("ac_id", "part_number", "edition"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ac_id: Mapped[int] = mapped_column(ForeignKey("assembly_constituencies.id"))
    part_number: Mapped[int] = mapped_column(Integer)
    polling_station_number: Mapped[int | None] = mapped_column(Integer)
    polling_station_name: Mapped[str | None] = mapped_column(Text)
    polling_station_name_local: Mapped[str | None] = mapped_column(Text)
    polling_station_address: Mapped[str | None] = mapped_column(Text)
    part_name: Mapped[str | None] = mapped_column(Text)
    area_description: Mapped[str | None] = mapped_column(Text)
    edition: Mapped[str] = mapped_column(String(32), default="SIR-2026")
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)

    constituency: Mapped[AssemblyConstituency] = relationship(back_populates="polling_stations")
    rolls: Mapped[list["ElectoralRoll"]] = relationship(back_populates="polling_station")


# --------------------------------------------------------------------------
# Electoral rolls and electors
# --------------------------------------------------------------------------
class ElectoralRoll(Base, TimestampMixin):
    __tablename__ = "electoral_rolls"
    __table_args__ = (UniqueConstraint("polling_station_id", "roll_year", "roll_type", "language"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    polling_station_id: Mapped[int] = mapped_column(ForeignKey("polling_stations.id"))
    roll_year: Mapped[int] = mapped_column(Integer)
    roll_type: Mapped[str] = mapped_column(String(48))                 # Final / Draft / SIR-Draft
    language: Mapped[str | None] = mapped_column(String(16))
    qualifying_date: Mapped[str | None] = mapped_column(String(32))
    pdf_url: Mapped[str | None] = mapped_column(Text)
    local_file_path: Mapped[str | None] = mapped_column(Text)
    download_status: Mapped[str] = mapped_column(String(24), default="pending")
    extraction_status: Mapped[str] = mapped_column(String(24), default="pending")
    extraction_method: Mapped[str | None] = mapped_column(String(32))  # text / ocr
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    page_count: Mapped[int | None] = mapped_column(Integer)
    is_text_pdf: Mapped[bool | None] = mapped_column(Boolean)
    official_elector_count: Mapped[int | None] = mapped_column(Integer)
    official_count_basis: Mapped[str | None] = mapped_column(String(128))
    extracted_elector_count: Mapped[int | None] = mapped_column(Integer)
    parser_version: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64))

    polling_station: Mapped[PollingStation] = relationship(back_populates="rolls")
    electors: Mapped[list["Elector"]] = relationship(
        back_populates="roll", cascade="all, delete-orphan"
    )


class Elector(Base, TimestampMixin):
    """One elector row as published in the roll.

    Only fields that actually appear in the public Uttarakhand roll are modelled.
    There is deliberately no phone/contact column here — see `ContactRecord`.
    """

    __tablename__ = "electors"
    __table_args__ = (
        CheckConstraint("gender IN ('M','F','O','UNKNOWN')", name="ck_elector_gender"),
        Index("ix_electors_roll_serial", "electoral_roll_id", "serial_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    polling_station_id: Mapped[int] = mapped_column(ForeignKey("polling_stations.id"))
    electoral_roll_id: Mapped[int] = mapped_column(ForeignKey("electoral_rolls.id"))

    part_number: Mapped[int | None] = mapped_column(Integer)
    section_number: Mapped[int | None] = mapped_column(Integer)
    serial_number: Mapped[int | None] = mapped_column(Integer)

    elector_name: Mapped[str | None] = mapped_column(String(256))
    elector_name_raw: Mapped[str | None] = mapped_column(String(256))
    relative_name: Mapped[str | None] = mapped_column(String(256))
    relative_name_raw: Mapped[str | None] = mapped_column(String(256))
    relative_type: Mapped[str | None] = mapped_column(String(16))      # FATHER/MOTHER/HUSBAND/OTHER
    age: Mapped[int | None] = mapped_column(Integer)
    gender: Mapped[str | None] = mapped_column(String(8))
    epic_number: Mapped[str | None] = mapped_column(String(48))
    house_number: Mapped[str | None] = mapped_column(String(64))

    # provenance / audit
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)
    source_document: Mapped[str | None] = mapped_column(Text)
    source_page: Mapped[int | None] = mapped_column(Integer)
    raw_text: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str | None] = mapped_column(String(64))
    extraction_method: Mapped[str | None] = mapped_column(String(32))
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # validation outcome
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    validation_notes: Mapped[str | None] = mapped_column(Text)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("electors.id"))
    duplicate_kind: Mapped[str | None] = mapped_column(String(32))

    roll: Mapped[ElectoralRoll] = relationship(back_populates="electors")


# --------------------------------------------------------------------------
# Historical elections (Form 20)
# --------------------------------------------------------------------------
class Election(Base, TimestampMixin):
    __tablename__ = "elections"
    __table_args__ = (UniqueConstraint("election_year", "election_type", "state"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    election_year: Mapped[int] = mapped_column(Integer)
    election_type: Mapped[str] = mapped_column(String(48))             # VIDHAN_SABHA / LOK_SABHA
    state: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)

    results: Mapped[list["ElectionResult"]] = relationship(back_populates="election")


class ElectionResult(Base, TimestampMixin):
    """One candidate's votes at one polling station (or AC total when part is NULL)."""

    __tablename__ = "election_results"
    __table_args__ = (
        Index("ix_results_election_ac_part", "election_id", "ac_id", "part_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[int] = mapped_column(ForeignKey("elections.id"))
    ac_id: Mapped[int | None] = mapped_column(ForeignKey("assembly_constituencies.id"))
    ac_number: Mapped[int | None] = mapped_column(Integer)
    part_number: Mapped[int | None] = mapped_column(Integer)
    polling_station_name: Mapped[str | None] = mapped_column(Text)
    candidate_name: Mapped[str | None] = mapped_column(String(256))
    candidate_name_raw: Mapped[str | None] = mapped_column(String(256))
    party: Mapped[str | None] = mapped_column(String(160))
    votes: Mapped[int | None] = mapped_column(Integer)
    nota_votes: Mapped[int | None] = mapped_column(Integer)
    total_valid_votes: Mapped[int | None] = mapped_column(Integer)
    rejected_votes: Mapped[int | None] = mapped_column(Integer)
    tendered_votes: Mapped[int | None] = mapped_column(Integer)
    total_votes: Mapped[int | None] = mapped_column(Integer)
    # Per-candidate vote columns in printed order, as a JSON list. Candidate
    # NAMES are not attributable (rotated headers), but the column values are
    # exact wherever `vote_sum_matches` is true, which is enough for a booth's
    # leading/second vote counts and margin. Added by an additive migration.
    candidate_votes_json: Mapped[str | None] = mapped_column(Text)
    vote_sum_matches: Mapped[bool | None] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(64))
    source_file: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_page: Mapped[int | None] = mapped_column(Integer)
    parser_version: Mapped[str | None] = mapped_column(String(64))
    extraction_confidence: Mapped[float | None] = mapped_column(Float)

    election: Mapped[Election] = relationship(back_populates="results")


class PartMapping(Base, TimestampMixin):
    """Links a part number in one edition to a part number in another.

    Part numbers are not stable across revisions or delimitations, so a mapping is
    always (a) directional, (b) possibly one-to-many, and (c) carries the method
    and a confidence score. Never assume identity.
    """

    __tablename__ = "part_mapping"
    __table_args__ = (
        Index("ix_partmap_lookup", "from_edition", "from_ac_number", "from_part_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    from_edition: Mapped[str] = mapped_column(String(32))
    from_ac_number: Mapped[int] = mapped_column(Integer)
    from_ac_name: Mapped[str | None] = mapped_column(String(160))
    from_part_number: Mapped[int] = mapped_column(Integer)
    from_part_name: Mapped[str | None] = mapped_column(Text)
    to_edition: Mapped[str] = mapped_column(String(32))
    to_ac_number: Mapped[int | None] = mapped_column(Integer)
    to_ac_name: Mapped[str | None] = mapped_column(String(160))
    to_part_number: Mapped[int | None] = mapped_column(Integer)
    to_part_name: Mapped[str | None] = mapped_column(Text)
    area_name: Mapped[str | None] = mapped_column(Text)
    mapping_method: Mapped[str] = mapped_column(String(48))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------
# Operational / audit
# --------------------------------------------------------------------------
class SourceFetch(Base, TimestampMixin):
    """Audit trail: every remote fetch the pipeline performed."""

    __tablename__ = "source_fetches"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(8), default="GET")
    http_status: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    bytes_received: Mapped[int | None] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    local_path: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class ContactRecord(Base, TimestampMixin):
    """Consent-gated contact data. Kept structurally separate from roll data.

    NEVER populated from electoral rolls — public rolls do not contain phone
    numbers. Only test/mock or explicitly consented records belong here.
    """

    __tablename__ = "contact_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    booth_id: Mapped[int | None] = mapped_column(ForeignKey("polling_stations.id"))
    name: Mapped[str | None] = mapped_column(String(256))
    mobile: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(64))
    consent_status: Mapped[str] = mapped_column(String(24), default="none")
    consent_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
