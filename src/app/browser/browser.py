"""Playwright helpers for RESEARCH ONLY.

Browser automation is the fifth choice in this project's source hierarchy
(official API -> direct HTTP -> published document -> HTML -> browser -> OCR),
and it is not part of the extraction pipeline. It exists so that a portal's
*own* network calls can be observed and documented when direct HTTP is not
enough to understand the contract.

Hard rules, enforced by what this module does and does not contain:

  * It never requests, reads, solves or submits a CAPTCHA.
  * It never spoofs fingerprints or attempts anti-bot evasion.
  * It never authenticates.
  * It only drives controls a human would drive, and records what happens.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright

from ..config import settings

log = logging.getLogger(__name__)

RESEARCH_DIR = settings.research_dir / "network"

# Header names that must never be written to a research artefact.
_REDACT = {"cookie", "authorization", "set-cookie", "x-api-key", "token"}


@dataclass
class CapturedCall:
    url: str
    method: str
    status: int
    request_headers: dict[str, str] = field(default_factory=dict)
    post_data: str | None = None
    response_sample: Any = None

    def to_dict(self) -> dict:
        return {
            "url": self.url, "method": self.method, "status": self.status,
            "request_headers": self.request_headers, "post_data": self.post_data,
            "response_sample": self.response_sample,
        }


class NetworkRecorder:
    """Records API calls a page makes, with credentials stripped."""

    def __init__(self, url_filter: str = "/api/") -> None:
        self.url_filter = url_filter
        self.calls: list[CapturedCall] = []

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    async def _on_response(self, response) -> None:
        if self.url_filter not in response.url:
            return
        headers = {k: v for k, v in response.request.headers.items()
                   if k.lower() not in _REDACT}
        call = CapturedCall(
            url=response.url, method=response.request.method,
            status=response.status, request_headers=headers,
            post_data=response.request.post_data,
        )
        try:
            if "json" in (response.headers.get("content-type") or ""):
                body = await response.json()
                call.response_sample = json.loads(json.dumps(body)[:2000])
        except Exception:
            pass
        self.calls.append(call)
        log.info("[%d] %s %s", call.status, call.method, call.url[:120])

    def save(self, name: str) -> Path:
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        path = RESEARCH_DIR / f"{name}.json"
        path.write_text(
            json.dumps([c.to_dict() for c in self.calls], indent=2, ensure_ascii=False),
            encoding="utf-8")
        return path


@asynccontextmanager
async def research_page(*, headless: bool = True, viewport=(1440, 1100)):
    """A plain Chromium page. No stealth, no fingerprint spoofing."""
    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]},
            locale="en-IN",
        )
        page = await context.new_page()
        try:
            yield page
        finally:
            await browser.close()


async def screenshot(page: Page, name: str, full_page: bool = True) -> Path:
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    path = RESEARCH_DIR / f"{name}.png"
    await page.screenshot(path=str(path), full_page=full_page)
    log.info("screenshot -> %s", path)
    return path


async def describe_controls(page: Page) -> dict:
    """Snapshot the form controls a human would have to fill in.

    Used to record *that* a CAPTCHA field exists — never to interact with one.
    """
    return await page.evaluate("""() => ({
        selects: [...document.querySelectorAll('select')].map(s => ({
            id: s.id, name: s.name,
            options: [...s.options].slice(0, 8).map(o => o.text.trim())
        })),
        inputs: [...document.querySelectorAll('input')].map(i => ({
            id: i.id, name: i.name, type: i.type, placeholder: i.placeholder
        })),
        buttons: [...document.querySelectorAll('button')]
            .map(b => b.innerText.trim()).filter(Boolean),
        captchaPresent: /captcha/i.test(document.body.innerHTML),
        captchaImages: [...document.querySelectorAll('img')]
            .filter(i => /captcha/i.test(i.src + i.id + i.className))
            .map(i => i.src.slice(0, 120)),
        text: document.body.innerText.slice(0, 2500)
    })""")
