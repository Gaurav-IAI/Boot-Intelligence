"""The states this application covers, and how deep each one goes.

Every state's hierarchy (districts, constituencies) comes from the ECI gateway,
which works for any state; its code is discovered by name, never hard-coded.
Booth-level data (polling stations, rolls, part mapping) needs a state
election-office adapter, which exists only for Uttarakhand so far. Form 20 booth
results have adapters for Uttarakhand, Uttar Pradesh and Telangana.

Rows that are keyed by constituency *number* rather than by id — Form 20 results,
part mappings, pipeline notes — belong to one state. AC numbers repeat across
states (every state has an AC 19), so a number is only ever matched inside the
state that owns the row.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database.models import AssemblyConstituency, District, State


@dataclass(frozen=True)
class StateSpec:
    name: str                              # as the ECI gateway spells it
    booth_sources: bool                    # a CEO adapter supplies polling stations, rolls, mapping
    source_prefixes: tuple[str, ...] = ()  # SourceFetch.source ids owned by this state
    # Form 20 booth results can come from a CEO adapter even where polling-station
    # lists and rolls do not (UP publishes results as Excel; Telangana as scans).
    # Only post-2008-delimitation Vidhan Sabha years: earlier ones use other AC numbers.
    form20_years: tuple[int, ...] = ()

    @property
    def result_sources(self) -> bool:
        return bool(self.form20_years)


STATES: tuple[StateSpec, ...] = (
    StateSpec("Uttarakhand", booth_sources=True, source_prefixes=("ceo_uk_",), form20_years=(2012,)),
    StateSpec("Uttar Pradesh", booth_sources=False, source_prefixes=("ceo_up_",),
              form20_years=(2012, 2017, 2022)),
    StateSpec("Telangana", booth_sources=False, source_prefixes=("ceo_tg_",),
              form20_years=(2018, 2023)),
)
STATE_NAMES: tuple[str, ...] = tuple(s.name for s in STATES)
DEFAULT_STATE = "Uttarakhand"
# The only state with a CEO adapter: every booth-level pipeline stage runs for it alone.
BOOTH_STATE = "Uttarakhand"


def spec(name: str | None) -> StateSpec | None:
    return next((s for s in STATES if s.name == name), None)


def ordered_states(db: Session) -> list[State]:
    """Stored states, registry order first, then any others by name."""
    rank = {n: i for i, n in enumerate(STATE_NAMES)}
    return sorted(db.scalars(select(State)).all(),
                  key=lambda s: (rank.get(s.state_name, len(rank)), s.state_name))


def state_by_name(db: Session, name: str) -> State | None:
    return db.scalar(select(State).where(State.state_name == name).order_by(State.id))


def state_by_code(db: Session, code: str | None) -> State | None:
    if not code:
        return None
    return db.scalar(select(State).where(State.state_code == code))


def booth_state_id(db: Session) -> int | None:
    st = state_by_name(db, BOOTH_STATE)
    return st.id if st else None


def state_for_ac(db: Session, ac: AssemblyConstituency) -> State | None:
    return db.scalar(select(State).join(District, District.state_id == State.id)
                     .where(District.id == ac.district_id))


def mapping_state_id(state_id: int | None, booth_id: int | None) -> int | None:
    """A part mapping's state. Rows stored before the column existed were all ingested
    by the Uttarakhand adapter, so NULL means the booth state."""
    return state_id if state_id is not None else booth_id
