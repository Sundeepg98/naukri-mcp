"""``browser.page_pool`` before start() and after stop() - the absent-pool path.

Context (2026-08-22 live sweep): with the browser down, SIX tools
(naukri_company_intel, naukri_company_jobs, naukri_company_slug, naukri_debug,
naukri_profile_prompts, naukri_status_changes -- plus both sync tools) returned

    Internal error: AttributeError: 'NoneType' object has no attribute 'acquire'

``PagePool.acquire`` already fails cleanly with BrowserUnavailableError when its
circuit is open, but ``NaukriBrowser.page_pool`` was initialised to None and
reset to None by stop(), so all 27 ``browser.page_pool.acquire()`` call sites
bypassed that and hit attribute access on None. error_handler has no mapping for
AttributeError, so every one of them surfaced as INTERNAL_ERROR -- the code that
means "file a bug", not "your browser is down, run naukri_login".

Each test below either pins the typed failure or pins the falsiness the existing
truthiness guards depend on.
"""

import pytest

from naukri_server.browser import (
    BrowserUnavailableError,
    NaukriBrowser,
    _NO_POOL,
    _NoPagePool,
)


def test_fresh_browser_has_no_pool_but_is_not_none():
    """A never-started browser must not expose a bare None pool."""
    b = NaukriBrowser()
    assert b.page_pool is not None
    assert isinstance(b.page_pool, _NoPagePool)


def test_acquire_on_absent_pool_raises_browser_unavailable_not_attribute_error():
    """THE regression. Old code raised AttributeError here."""
    b = NaukriBrowser()
    with pytest.raises(BrowserUnavailableError) as ei:
        b.page_pool.acquire()
    msg = str(ei.value)
    assert "naukri_login" in msg, "the error must say what to do about it"


def test_absent_pool_is_falsy_so_existing_guards_still_see_no_pool():
    """health.py, probes/pool.py, probes/browser.py, browser_watchdog.py and
    stop() all branch on ``if browser.page_pool``. A truthy stand-in would send
    every one of them down the has-a-pool path."""
    b = NaukriBrowser()
    assert not b.page_pool
    assert (b.page_pool.get_stats() if b.page_pool else None) is None


def test_absent_pool_is_a_shared_singleton():
    """Identity is stable, so ``pool is browser.page_pool`` comparisons in
    PagePool.close_all cannot accidentally match a fresh stand-in."""
    assert NaukriBrowser().page_pool is _NO_POOL
    assert NaukriBrowser().page_pool is NaukriBrowser().page_pool


@pytest.mark.asyncio
async def test_absent_pool_maps_to_browser_error_not_internal_error():
    """End-to-end through the dispatcher every tool uses: the six tools that
    reported INTERNAL_ERROR during the sweep must report BROWSER_ERROR."""
    from naukri_server.error_handler import handle_tool_action

    b = NaukriBrowser()

    async def tool_body():
        async with b.page_pool.acquire() as page:  # noqa: F841
            return {"status": "success"}

    out = await handle_tool_action(tool_body, "test.absent_pool")
    assert out["status"] == "error"
    assert out["error_code"] == "BROWSER_ERROR", out
