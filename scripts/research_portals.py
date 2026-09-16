"""Re-run the browser-based portal research.

    python scripts/research_portals.py --eci    # observe the ECI e-roll page
    python scripts/research_portals.py --ceo    # re-verify CEO UK CAPTCHA gates
    python scripts/research_portals.py --all

RESEARCH ONLY. This observes the portals' own behaviour; it is not part of the
extraction pipeline and it never interacts with a CAPTCHA. Requires
`playwright install chromium`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.browser.ceo_uttarakhand import verify_gates          # noqa: E402
from app.browser.eci_portal import inspect_eroll_page         # noqa: E402


async def run(do_eci: bool, do_ceo: bool) -> int:
    if do_eci:
        print("=" * 74)
        print("ECI Electoral Roll page — observing its own API calls")
        print("=" * 74)
        findings = await inspect_eroll_page()
        print(f"\nRoll types offered : {findings['roll_types']}")
        print(f"Years offered      : {findings['years']}")
        print(f"ACs after district : {findings['acs'][:6]} …")
        print(f"Languages          : {findings['languages']}")
        print(f"CAPTCHA present    : {findings['captcha_present']}")
        print(f"Buttons            : {findings['buttons']}")
        print("\nAPI calls the page made:")
        for c in findings["captured_calls"]:
            print(f"  [{c['status']}] {c['method']:4s} {c['endpoint']}"
                  f"{'' if c['post_data_readable'] else '   (body not readable)'}")
        print("\nConclusions:")
        for line in findings["conclusions"]:
            print(f"  - {line}")
        print(f"\nFull capture: {findings['calls_file']}")

    if do_ceo:
        print("\n" + "=" * 74)
        print("CEO Uttarakhand — re-verifying which pages are gated")
        print("=" * 74)
        results = await verify_gates()
        summary = results.pop("_summary")
        for name, r in results.items():
            if "error" in r:
                print(f"  {name:24s} ERROR: {r['error']}")
                continue
            mark = "ok " if r["matches_documentation"] else "DRIFT"
            print(f"  [{mark}] {name:24s} captcha={str(r['captcha_present']):5s} "
                  f"expected={str(r['expected_gated']):5s}")
        if summary["drifted_from_documentation"]:
            print(f"\n  !! documentation is out of date for: "
                  f"{summary['drifted_from_documentation']}")
            print(f"  {summary['note']}")
        else:
            print("\n  All gates match docs/DATA_SOURCE_RESEARCH.md.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eci", action="store_true")
    ap.add_argument("--ceo", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s | %(message)s")

    do_eci = args.eci or args.all
    do_ceo = args.ceo or args.all
    if not (do_eci or do_ceo):
        ap.print_help()
        return 1
    return asyncio.run(run(do_eci, do_ceo))


if __name__ == "__main__":
    raise SystemExit(main())
