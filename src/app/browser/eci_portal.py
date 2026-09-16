"""RESEARCH ONLY: observe the ECI Electoral Roll page's own API calls.

Drives the public State -> Year -> Roll Type -> District -> AC dropdowns exactly
as a person would, and records which endpoints the site itself calls and what
they return. It **stops at the CAPTCHA**: the CAPTCHA is never read, solved,
submitted or worked around, and no PDF is downloaded here.

Run via:  python scripts/research_portals.py --eci
"""
from __future__ import annotations

import json
import logging

from .browser import NetworkRecorder, describe_controls, research_page, screenshot

log = logging.getLogger(__name__)

EROLL_URL = "https://voters.eci.gov.in/download-eroll?stateCode={state_code}"

SELECT_IDS = {
    "state": "#stateCode",
    "year": "#revyear",
    "roll_type": "#roleType",
    "district": "#district",
    "ac": "#constituency",
    "language": "#langCd",
}


async def _options(page, selector: str) -> list[str]:
    try:
        return await page.eval_on_selector_all(
            f"{selector} option", "os => os.map(o => o.text.trim())")
    except Exception:
        return []


async def _select(page, selector: str, label: str) -> bool:
    try:
        await page.select_option(selector, label=label, timeout=15_000)
        await page.wait_for_timeout(3000)
        log.info("selected %s = %s", selector, label)
        return True
    except Exception as exc:
        log.warning("could not select %s = %s: %s", selector, label, str(exc)[:120])
        return False


async def inspect_eroll_page(state_code: str = "S28",
                             district: str = "Dehradun") -> dict:
    """Walk the public dropdowns and record the resulting API traffic."""
    recorder = NetworkRecorder(url_filter="/api/")
    findings: dict = {"state_code": state_code}

    async with research_page() as page:
        recorder.attach(page)

        await page.goto(EROLL_URL.format(state_code=state_code),
                        wait_until="load", timeout=90_000)
        await page.wait_for_timeout(9000)

        findings["roll_types"] = await _options(page, SELECT_IDS["roll_type"])
        findings["years"] = await _options(page, SELECT_IDS["year"])

        # The state arrives preselected from the query string; selecting it again
        # is rejected by the widget, so only the downstream controls are driven.
        if len(findings["years"]) > 1:
            await _select(page, SELECT_IDS["year"], findings["years"][0])
        if len(findings["roll_types"]) > 1:
            await _select(page, SELECT_IDS["roll_type"], findings["roll_types"][1])
        await _select(page, SELECT_IDS["district"], district)

        findings["acs"] = await _options(page, SELECT_IDS["ac"])
        if len(findings["acs"]) > 1:
            await _select(page, SELECT_IDS["ac"], findings["acs"][1])
        await page.wait_for_timeout(5000)

        controls = await describe_controls(page)
        findings["captcha_present"] = controls["captchaPresent"]
        findings["buttons"] = controls["buttons"]
        findings["languages"] = await _options(page, SELECT_IDS["language"])

        await screenshot(page, "eci_eroll_filled")
        calls_path = recorder.save("eci_eroll_calls")

    findings["captured_calls"] = [
        {"method": c.method, "status": c.status,
         "endpoint": c.url.split("/api/v1/")[-1][:120],
         "post_data_readable": c.post_data is not None}
        for c in recorder.calls
    ]
    findings["calls_file"] = str(calls_path)

    # The conclusions this research exists to establish.
    findings["conclusions"] = [
        "Roll-PDF download is CAPTCHA-gated: the page renders a required "
        "'Captcha *' field above 'Download Selected PDFs'. NOT AUTOMATED.",
        "printing-publish/* parameters are encrypted client-side and rotate per "
        "page load, so the endpoints are not reproducible outside the browser.",
        "The public metadata endpoints (common/states, common/districts, "
        "common/acs) are plain JSON and ARE automated by the pipeline.",
    ]
    return findings


def print_findings(findings: dict) -> None:
    print(json.dumps(findings, indent=2, ensure_ascii=False))
