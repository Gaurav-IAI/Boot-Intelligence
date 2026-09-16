"""Research utility: probe the public ECI + CEO Uttarakhand endpoints.

Prints the status and response shape of each endpoint and saves a sample under
`research/samples/`. Use it to tell a changed upstream API from a broken client.

    python scripts/inspect_eci_api.py
    python scripts/inspect_eci_api.py --state Uttarakhand --district Dehradun --ac 19

CAPTCHA-gated and encrypted-parameter endpoints are *probed for their guard
response only* — enough to confirm the gate is still there — and never solved,
submitted to, or worked around. No credential or token is read or written.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.http_client import HttpClient                     # noqa: E402
from app.sources.ceo_uttarakhand.client import (           # noqa: E402
    LegacyRoll2003Client, SirPartsClient,
)
from app.sources.eci_api.client import BASE, EciApiClient  # noqa: E402

SAMPLES = ROOT / "research" / "samples"
CEO_HOST = "https://election.uk.gov.in"


def save(name: str, payload) -> Path:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    p = SAMPLES / f"{name}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def shape(value, depth: int = 0) -> str:
    """One-line description of a JSON value's schema."""
    if isinstance(value, list):
        return f"array[{len(value)}] of " + (shape(value[0], depth + 1) if value else "?")
    if isinstance(value, dict):
        if depth > 1:
            return f"object({len(value)} keys)"
        return "object{" + ", ".join(list(value)[:12]) + ("…" if len(value) > 12 else "") + "}"
    return type(value).__name__


def probe(label: str, fn) -> None:
    print(f"\n--- {label}")
    try:
        result = fn()
    except Exception as exc:
        print(f"    FAILED: {type(exc).__name__}: {str(exc)[:200]}")
        return
    print(f"    OK: {result}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="Uttarakhand")
    ap.add_argument("--district", default="Dehradun")
    ap.add_argument("--ac", type=int, default=19)
    ap.add_argument("--legacy-district", default="देहरादून")
    ap.add_argument("--legacy-ac", default="राजपुर")
    args = ap.parse_args()

    http = HttpClient()
    eci = EciApiClient(http)

    print("=" * 74)
    print("ECI gateway — public metadata endpoints")
    print("=" * 74)

    state = None

    def _states():
        nonlocal state
        raw = http.get_json(eci.states_url())
        save("states", raw)
        state = eci.find_state(args.state)
        return (f"{len(raw)} states; shape {shape(raw)}\n"
                f"    resolved {args.state} -> {state.state_code} "
                f"(stateId {state.external_id})  [saved research/samples/states.json]")
    probe(f"GET {BASE}/common/states", _states)

    district = None

    def _districts():
        nonlocal district
        raw = http.get_json(eci.districts_url(state.state_code))
        save(f"districts_{state.state_code}", raw)
        rows = eci.list_districts(state.state_code)
        district = next((d for d in rows
                         if d.district_name.casefold() == args.district.casefold()), None)
        return (f"{len(raw)} districts; shape {shape(raw)}\n"
                f"    {args.district} -> "
                f"{district.district_code if district else 'NOT FOUND'}")
    if state:
        probe(f"GET {BASE}/common/districts/{{stateCd}}", _districts)

    def _acs():
        raw = http.get_json(eci.acs_url(district.district_code))
        save(f"acs_{district.district_code}", raw)
        rows = eci.list_acs(district.district_code)
        names = ", ".join(f"{a.ac_number}-{a.ac_name}" for a in rows[:4])
        return f"{len(raw)} ACs; shape {shape(raw)}\n    {names} …"
    if district:
        probe(f"GET {BASE}/common/acs/{{districtCd}}", _acs)

    print("\n" + "=" * 74)
    print("ECI gateway — gated endpoints (guard response only, never bypassed)")
    print("=" * 74)

    def _eroll_type():
        r = http.request("GET", f"{BASE}/printing-publish/get-publish-eroll-type",
                         params={"stateCd": "S28", "year": "2026"})
        return r.text[:200]

    print(f"\n--- GET {BASE}/printing-publish/get-publish-eroll-type")
    try:
        r = http._client(False).get(
            f"{BASE}/printing-publish/get-publish-eroll-type",
            params={"stateCd": "S28", "year": "2026"})
        print(f"    HTTP {r.status_code}: {r.text[:220]}")
        print("    -> plaintext parameters are rejected; the portal sends encrypted")
        print("       values. NOT AUTOMATED (see docs/API_FINDINGS.md).")
    except Exception as exc:
        print(f"    FAILED: {exc}")

    print(f"\n--- GET {BASE}/captcha-service/getCaptcha/EROLL")
    print("    NOT PROBED. The e-roll download is CAPTCHA-gated; this POC never")
    print("    requests, reads, solves or submits a CAPTCHA.")

    print("\n" + "=" * 74)
    print("CEO Uttarakhand — public endpoints")
    print("=" * 74)

    sir = SirPartsClient(http)

    def _parts():
        raw = http.get_json(f"{CEO_HOST}/asdlist/SearchAdsEpic/Parts",
                            params={"acNo": args.ac},
                            headers={"Referer": f"{CEO_HOST}/asdlist"})
        save(f"uk_sir_parts_ac{args.ac}", raw)
        return (f"{len(raw)} parts for AC {args.ac}; shape {shape(raw)}\n"
                f"    e.g. {raw[0] if raw else '-'}")
    probe(f"GET {CEO_HOST}/asdlist/SearchAdsEpic/Parts?acNo={args.ac}", _parts)

    legacy = LegacyRoll2003Client(http)

    def _legacy_districts():
        rows = legacy.list_districts()
        save("uk_legacy2003_districts", [r.__dict__ for r in rows])
        return f"{len(rows)} districts (2003 delimitation); e.g. {rows[0].name}"
    probe("GET /search2003uk/api/uklegacydata/district-names", _legacy_districts)

    def _legacy_acs():
        rows = legacy.list_acs(args.legacy_district)
        save("uk_legacy2003_acs", [r.__dict__ for r in rows])
        return f"{len(rows)} ACs in {args.legacy_district}; " + \
               ", ".join(f"{r.number}-{r.name}" for r in rows[:4])
    probe("GET /search2003uk/api/uklegacydata/ac-names", _legacy_acs)

    def _legacy_parts():
        rows = legacy.list_parts(args.legacy_ac)
        save("uk_legacy2003_parts", [r.__dict__ for r in rows])
        return f"{len(rows)} parts in AC {args.legacy_ac}; e.g. {rows[0].number}-{rows[0].name}"
    probe("GET /search2003uk/api/uklegacydata/part-names", _legacy_parts)

    def _mapping():
        villages = legacy.list_villages("अस्थल")
        if not villages:
            return "no villages matched the probe term"
        rows = legacy.village_details(villages[0])
        save("uk_legacy2003_village_mapping", [r.raw for r in rows])
        first = rows[0] if rows else None
        return (f"{len(rows)} mappings for {villages[0]!r}\n"
                f"    2003 AC {first.ac2003_number} part {first.part2003_number} -> "
                f"2025 AC {first.ac2025_number} part {first.part2025_number}"
                if first else "no mapping rows")
    probe("GET /search2003uk/api/uklegacydata/village-details", _mapping)

    print("\n--- roll PDF URL construction (no download)")
    print(f"    {legacy.roll_pdf_url(15, 'राजपुर', 6)}")

    print("\n--- PS list PDF URL (no download)")
    print(f"    {sir.ps_list_pdf_url(args.ac)}")

    print("\n" + "=" * 74)
    print(f"HTTP: {http.stats.ok}/{http.stats.attempted} ok "
          f"({http.stats.success_rate:.1f}%), {http.stats.retries} retries, "
          f"statuses {http.stats.by_status}")
    print(f"Samples written to {SAMPLES}")
    print("No credential or token was read or written.")
    http.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
