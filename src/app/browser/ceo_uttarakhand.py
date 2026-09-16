"""RESEARCH ONLY: confirm which CEO Uttarakhand pages are gated.

The pipeline uses CEO Uttarakhand's public JSON and PDF endpoints over plain
HTTP; no browser is needed for extraction. This module exists to re-verify, on
demand, that the *gated* parts of those same sites are still gated — so the
project's boundary is checked against reality rather than assumed from a note
written months earlier.

It records the presence of a CAPTCHA. It never requests, reads, solves or
submits one.

Run via:  python scripts/research_portals.py --ceo
"""
from __future__ import annotations

import logging

from .browser import describe_controls, research_page, screenshot

log = logging.getLogger(__name__)

PAGES = {
    "asd_list": "https://election.uk.gov.in/asdlist",
    "roll_portal_2018_2023": "https://election.uk.gov.in/",
    "ps_list_2026": "https://election.uk.gov.in/PSSIR2026/polling_station_2026.html",
    "legacy_roll_2003": "https://election.uk.gov.in/search2003uk/search.html",
}

# What we expect each page to be, from docs/DATA_SOURCE_RESEARCH.md.
EXPECTED_GATED = {
    "asd_list": True,                  # download + EPIC search need a CAPTCHA
    "roll_portal_2018_2023": True,     # Captcha1 in __VIEWSTATE
    "ps_list_2026": False,             # plain links to PDFs
    "legacy_roll_2003": False,         # plain API + PDF service
}


async def _viewstate_declares_captcha(page) -> bool:
    """Look for a CAPTCHA control inside an ASP.NET __VIEWSTATE.

    The 2018-2023 roll portal registers a `Captcha1` control in its control
    tree but only renders it after a roll is selected via postback, so scanning
    the initial HTML for the word "captcha" reports a false negative. The
    control tree is base64 inside __VIEWSTATE, which is present on first load.
    """
    import base64

    value = await page.evaluate(
        "() => (document.querySelector('#__VIEWSTATE') || {}).value || ''")
    if not value:
        return False
    try:
        decoded = base64.b64decode(value).decode("latin-1")
    except Exception:
        return False
    return "captcha" in decoded.lower()


async def verify_gates() -> dict:
    """Check each page's CAPTCHA status against what the docs claim."""
    results: dict = {}

    async with research_page() as page:
        for name, url in PAGES.items():
            try:
                await page.goto(url, wait_until="load", timeout=60_000)
                await page.wait_for_timeout(2500)
                controls = await describe_controls(page)
                in_viewstate = await _viewstate_declares_captcha(page)
                gated = bool(controls["captchaPresent"]) or in_viewstate
                expected = EXPECTED_GATED[name]
                results[name] = {
                    "url": url,
                    "captcha_present": gated,
                    "captcha_in_rendered_html": bool(controls["captchaPresent"]),
                    "captcha_in_aspnet_viewstate": in_viewstate,
                    "expected_gated": expected,
                    "matches_documentation": gated == expected,
                    "selects": len(controls["selects"]),
                    "buttons": controls["buttons"][:6],
                }
                await screenshot(page, f"ceo_{name}", full_page=False)
                log.info("%s: captcha=%s (expected %s)", name, gated, expected)
            except Exception as exc:
                results[name] = {"url": url, "error": str(exc)[:200]}
                log.warning("%s: %s", name, exc)

    drifted = [k for k, v in results.items()
               if v.get("matches_documentation") is False]
    results["_summary"] = {
        "checked": len(PAGES),
        "drifted_from_documentation": drifted,
        "note": ("A page that gained a CAPTCHA must be moved to 'manual only'; "
                 "a page that lost one is still only usable if its terms allow it."),
    }
    return results
