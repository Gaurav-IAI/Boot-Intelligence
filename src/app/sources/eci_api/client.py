"""Adapter for the public ECI voter gateway.

Only the genuinely public, unauthenticated, CAPTCHA-free metadata endpoints are
implemented. The roll-publish endpoints are documented in
docs/DATA_SOURCE_RESEARCH.md as encrypted/CAPTCHA-gated and are deliberately
NOT implemented here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...http_client import HttpClient

BASE = "https://gateway-voters.eci.gov.in/api/v1"
SOURCE = "eci_gateway_api"


@dataclass(frozen=True)
class StateRef:
    state_code: str
    state_name: str
    state_name_local: str | None
    state_type: str | None
    external_id: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class DistrictRef:
    district_code: str
    district_number: int | None
    district_name: str
    district_name_local: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class AcRef:
    ac_number: int
    ac_name: str
    ac_name_local: str | None
    category: str | None
    pc_number: int | None
    official_code: str | None
    district_code: str
    raw: dict[str, Any]


def _as_int(v: Any) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


class EciApiClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    # -- states ----------------------------------------------------------
    def states_url(self) -> str:
        return f"{BASE}/common/states"

    def list_states(self) -> list[StateRef]:
        data = self.http.get_json(self.states_url())
        if not isinstance(data, list):
            raise ValueError(f"/common/states returned {type(data).__name__}, expected a list")
        out = []
        for row in data:
            if not row.get("stateCd") or not row.get("stateName"):
                continue
            out.append(StateRef(
                state_code=row["stateCd"].strip(),
                state_name=row["stateName"].strip(),
                state_name_local=(row.get("stateNameHindi") or None),
                state_type=row.get("stateType"),
                external_id=_as_int(row.get("stateId")),
                raw=row,
            ))
        return out

    def find_state(self, name: str) -> StateRef:
        """Resolve a state by name. The code is never hard-coded."""
        target = name.strip().casefold()
        states = self.list_states()
        for s in states:
            if s.state_name.casefold() == target:
                return s
        for s in states:
            if target in s.state_name.casefold():
                return s
        raise LookupError(
            f"state {name!r} not found in /common/states "
            f"({len(states)} states returned)"
        )

    # -- districts -------------------------------------------------------
    def districts_url(self, state_code: str) -> str:
        return f"{BASE}/common/districts/{state_code}"

    def list_districts(self, state_code: str) -> list[DistrictRef]:
        data = self.http.get_json(self.districts_url(state_code))
        out = []
        for row in data:
            code = row.get("districtCd")
            name = row.get("districtValue")
            if not code or not name:
                continue
            out.append(DistrictRef(
                district_code=code.strip(),
                district_number=_as_int(row.get("districtNo")),
                district_name=name.strip(),
                district_name_local=(row.get("districtValueHindi") or None),
                raw=row,
            ))
        return sorted(out, key=lambda d: d.district_code)

    # -- assembly constituencies -----------------------------------------
    def acs_url(self, district_code: str) -> str:
        return f"{BASE}/common/acs/{district_code}"

    def list_acs(self, district_code: str) -> list[AcRef]:
        data = self.http.get_json(self.acs_url(district_code))
        out = []
        for row in data:
            no = _as_int(row.get("asmblyNo"))
            if no is None or not row.get("asmblyName"):
                continue
            out.append(AcRef(
                ac_number=no,
                ac_name=row["asmblyName"].strip(),
                ac_name_local=(row.get("asmblyNameL1") or None),
                category=row.get("category"),
                pc_number=_as_int(row.get("pcNo")),
                official_code=(str(row["acId"]) if row.get("acId") is not None else None),
                district_code=(row.get("districtCd") or district_code),
                raw=row,
            ))
        return sorted(out, key=lambda a: a.ac_number)
