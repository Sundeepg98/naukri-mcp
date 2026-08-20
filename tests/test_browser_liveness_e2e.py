"""Liveness predicate against a REAL Chromium that is deliberately killed.

Marked e2e (deselected by default via pytest.ini addopts). Run explicitly:

    python -m pytest -m e2e tests/test_browser_liveness_e2e.py -v

This is the control the mock-based tests cannot be: every fake page in
test_browser_liveness.py encodes MY belief about what a dead Playwright page
does. This test asks the real library. It is the difference between "the check
fails against a fake corpse" and "the check fails against a corpse".

Safety: uses chromium.launch() with Playwright's own throwaway profile - it
never touches the persistent naukri chrome-profile, never takes the profile
lock, and never touches any browser the operator is using.
"""

import pytest
from playwright.async_api import async_playwright

from naukri_server.browser import is_page_alive

pytestmark = pytest.mark.e2e


async def test_predicate_against_a_real_closed_context():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("about:blank")

        # --- alive ---------------------------------------------------------
        assert await is_page_alive(page) is True

        # --- kill the context ----------------------------------------------
        await context.close()

        # THE BUG, reproduced against the real library: the old liveness check
        # still answers, with a plain string, on a page whose context is gone.
        cached_url = page.url
        assert isinstance(cached_url, str)

        # THE FIX: the new predicate returns the failing verdict.
        assert await is_page_alive(page) is False

        await browser.close()


async def test_predicate_against_a_real_closed_browser():
    """Closing the whole browser is the shape of the 2026-08-20 outage."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("about:blank")
        assert await is_page_alive(page) is True

        await browser.close()

        assert isinstance(page.url, str)          # old check: still green
        assert await is_page_alive(page) is False  # new check: red, correctly


async def test_pool_reports_browser_unavailable_on_a_real_dead_context():
    """PagePool must surface BrowserUnavailableError, not a raw Playwright
    error, when the real context behind it is gone."""
    from naukri_server.browser import BrowserUnavailableError, PagePool

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        pool = PagePool(context, max_pages=2)
        await pool.initialize(page)

        async with pool.acquire() as p:
            assert p is page  # healthy checkout first

        await context.close()

        with pytest.raises(BrowserUnavailableError):
            async with pool.acquire():
                pass

        assert pool.circuit_state == "open"
        await browser.close()
