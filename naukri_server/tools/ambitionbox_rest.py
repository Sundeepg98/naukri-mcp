"""AmbitionBox REST bridge — direct API calls using browser cookies.

Provides 7 REST helper functions for AmbitionBox service gateways:
  - ab_get_benefits, ab_get_work_culture, ab_get_interview_questions
  - ab_get_competitors, ab_get_locations, ab_get_applied_jobs_insights
  - ab_get_salary_rest

Every request carries AB_REST_HEADERS (appid + systemid); the AB service
gateway answers 400 "AppId or SystemId not present" without them, which is
what made all seven helpers dead on arrival. Cookies are extracted from the
persistent Chrome browser context, cached with 30-minute TTL and
auto-refreshed on 401/403.

Used by:
  - ambitionbox.py (_enrich_with_rest) for supplementary data
  - research.py for parallel data fetching
  - daily_brief.py for applied-job salary insights

DDD: every read from an AmbitionBox response routes through ``safe_get`` so
the anti-corruption layer logs missing fields. Cookie management + the
``_ab_rest_get`` retry loop stay here because tests patch
``tools.ambitionbox_rest._ab_rest_get`` directly.
"""

import asyncio
from typing import Optional
import logging
import time

import aiohttp

from naukri_server.browser import browser, page_goto
from naukri_server.config import (
    AMBITIONBOX_BASE,
    AB_SALARY_API, AB_BENEFITS_API, AB_REVIEW_DIST_API, AB_INTERVIEW_QS_API,
    AB_COMPANY_COMPARE_API, AB_COMPANY_LOCATIONS_API, AB_INSIGHTS_APPLIED_API,
    AB_COOKIE_TTL,
)
from naukri_server.domain import safe_get

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gateway auth
# ---------------------------------------------------------------------------
#
# THE AB SERVICE GATEWAY REJECTS A REQUEST THAT CARRIES NEITHER HEADER, AND
# `_ab_rest_get` USED TO SEND NO HEADERS AT ALL -- so all seven helpers below
# were structurally incapable of a 200. Measured 2026-08-24 against the same
# review-distribution url ab_get_work_culture builds:
#
#   no headers            -> HTTP 400, 29 bytes, "AppId or SystemId not present"
#   appid + systemid      -> HTTP 200, 687 bytes, JSON carrying reviewsCount /
#                            workMonitorLabels / workMonitorSeries, i.e. the
#                            exact fields the helper reads.
#
# PROVENANCE, STATED PLAINLY: these are NOT discovered constants. No
# AmbitionBox appid exists anywhere in this repo, and the gateway was measured
# to check only that the two headers are PRESENT -- it accepted these values
# with no cookies at all. If AmbitionBox ever starts validating the values,
# the failure now arrives as AbRestGatewayError with the gateway's own message
# rather than as an empty enrichment, which is the point of raising it.
AB_REST_HEADERS = {
    "appid": "613",
    "systemid": "ambitionbox",
}


class AbRestGatewayError(RuntimeError):
    """The AB gateway refused the request itself (not the resource).

    Kept distinct from ``aiohttp.ClientError`` on purpose: a gateway rejection
    is NOT a cookie problem, so it must not fall into the retry branch below
    and spend a real browser navigation re-fetching cookies that were never
    the cause.
    """

# ═══════════════════════════════════════════════════════════════════════
# Cookie management
# ═══════════════════════════════════════════════════════════════════════

_ab_cookies: dict = {}
_ab_cookies_ts: float = 0.0
_ab_cookies_lock = asyncio.Lock()


async def _get_ab_cookies() -> dict:
    """Extract AmbitionBox cookies from the browser context.

    Navigates to an AB page if cookies are stale (older than AB_COOKIE_TTL).
    Returns a dict of cookie name->value pairs.
    """
    global _ab_cookies, _ab_cookies_ts
    now = time.time()
    if _ab_cookies and (now - _ab_cookies_ts) < AB_COOKIE_TTL:
        return _ab_cookies
    async with _ab_cookies_lock:
        # Double-check after acquiring lock
        now = time.time()
        if _ab_cookies and (now - _ab_cookies_ts) < AB_COOKIE_TTL:
            return _ab_cookies
        async with browser.page_pool.acquire() as page:
            await page_goto(page, f"{AMBITIONBOX_BASE}/list-of-companies")
            cookies_list = await page.context.cookies(AMBITIONBOX_BASE)
            _ab_cookies = {c["name"]: c["value"] for c in cookies_list}
            _ab_cookies_ts = time.time()
    return _ab_cookies


async def _ab_rest_get(url: str, params: Optional[dict] = None) -> dict:
    """GET request to an AmbitionBox REST endpoint using cached cookies.

    Sends AB_REST_HEADERS -- without them the gateway answers 400 "AppId or
    SystemId not present" and no helper in this module can ever succeed.

    Retries once with fresh cookies on 401/403. A 400 is NOT retried: it is a
    gateway rejection, cookies cannot fix it, and the retry costs a real
    browser navigation. It raises AbRestGatewayError carrying the gateway's
    own message instead.
    """
    global _ab_cookies, _ab_cookies_ts
    cookies = await _get_ab_cookies()

    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(cookies=cookies) as session:
                async with session.get(url, params=params,
                                       headers=dict(AB_REST_HEADERS),
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 400:
                        detail = ""
                        try:
                            detail = (await resp.text())[:200]
                        except Exception:
                            pass
                        logger.warning(
                            "AB REST gateway rejected %s: HTTP 400 %s", url, detail)
                        raise AbRestGatewayError(
                            "AmbitionBox gateway rejected the request: HTTP 400 "
                            "%s (url=%s)" % (detail, url))
                    if resp.status in (401, 403) and attempt == 0:
                        # Invalidate cookies and retry
                        _ab_cookies = {}
                        _ab_cookies_ts = 0.0
                        cookies = await _get_ab_cookies()
                        continue
                    resp.raise_for_status()
                    return await resp.json()
        except aiohttp.ClientError as e:
            if attempt == 0:
                _ab_cookies = {}
                _ab_cookies_ts = 0.0
                cookies = await _get_ab_cookies()
                continue
            raise
    return {}


# ═══════════════════════════════════════════════════════════════════════
# Public REST helpers
# ═══════════════════════════════════════════════════════════════════════


async def ab_get_benefits(company_id: str) -> dict:
    """Fetch 30 benefit categories for a company via REST."""
    logger.info("AB REST: fetching benefits for company %s", company_id)
    url = f"{AB_BENEFITS_API}/{company_id}/benefits-stats"
    data = await _ab_rest_get(url)
    benefits_data = safe_get(data, "data", field_name="data", warn=False, default={})
    return {
        "status": "success",
        "total_benefits": safe_get(benefits_data, "totalBenefits", field_name="totalBenefits", warn=False, default=0),
        "benefits": safe_get(benefits_data, "benefits", field_name="benefits", warn=False, default=[]),
    }


async def ab_get_work_culture(company_id: str) -> dict:
    """Fetch work culture distribution (timing, travel, work days, shifts)."""
    logger.info("AB REST: fetching work culture for company %s", company_id)
    url = f"{AB_REVIEW_DIST_API}/{company_id}"
    data = await _ab_rest_get(url)
    dist = safe_get(data, "data", field_name="data", warn=False, default={})
    return {
        "status": "success",
        "reviews_count": safe_get(dist, "reviewsCount", field_name="reviewsCount", warn=False, default=0),
        "work_timing": {
            "labels": safe_get(dist, "workMonitorLabels", field_name="workMonitorLabels", warn=False, default=[]),
            "values": safe_get(dist, "workMonitorSeries", field_name="workMonitorSeries", warn=False, default=[]),
            "insight": safe_get(dist, "workMonitorInsight", field_name="workMonitorInsight", warn=False, default=""),
        },
        "travel": {
            "labels": safe_get(dist, "travelTagsLabels", field_name="travelTagsLabels", warn=False, default=[]),
            "values": safe_get(dist, "travelTagsSeries", field_name="travelTagsSeries", warn=False, default=[]),
        },
        "work_days": {
            "labels": safe_get(dist, "workDaysLabels", field_name="workDaysLabels", warn=False, default=[]),
            "values": safe_get(dist, "workDaysSeries", field_name="workDaysSeries", warn=False, default=[]),
        },
        "shifts": {
            "labels": safe_get(dist, "shiftsLabels", field_name="shiftsLabels", warn=False, default=[]),
            "values": safe_get(dist, "shiftsSeries", field_name="shiftsSeries", warn=False, default=[]),
        },
    }


async def ab_get_interview_questions(company_id: str, designation_id: Optional[str] = None, limit: int = 7) -> dict:
    """Fetch actual interview questions by company and role."""
    logger.info("AB REST: fetching interview questions for company %s", company_id)
    params = {"companyId": company_id, "limit": str(limit)}
    if designation_id:
        params["designation"] = designation_id
    data = await _ab_rest_get(AB_INTERVIEW_QS_API, params=params)
    meta = safe_get(data, "meta", field_name="meta", warn=False, default={})
    return {
        "status": "success",
        "total_interviews": safe_get(data, "totalInterviewExperiencesLive", field_name="totalInterviewExperiencesLive", warn=False, default=0),
        "count": safe_get(meta, "count", field_name="count", warn=False, default=0),
        "questions": safe_get(data, "data", field_name="data", warn=False, default=[]),
    }


async def ab_get_competitors(company_id: str) -> dict:
    """Fetch top 41 competitors with ratings and review counts."""
    logger.info("AB REST: fetching competitors for company %s", company_id)
    url = f"{AB_COMPANY_COMPARE_API}/{company_id}"
    data = await _ab_rest_get(url)
    if isinstance(data, list):
        competitors = data
    else:
        competitors = safe_get(data, "data", field_name="data", warn=False, default=[])
    return {
        "status": "success",
        "count": len(competitors),
        "competitors": competitors,
    }


async def ab_get_locations(company_id: str) -> dict:
    """Fetch all office locations for a company."""
    logger.info("AB REST: fetching locations for company %s", company_id)
    url = f"{AB_COMPANY_LOCATIONS_API}/{company_id}/sectionalDetails"
    data = await _ab_rest_get(url, params={"sections": "companyLocations"})
    locations = safe_get(data, "companyLocations", field_name="companyLocations", warn=False, default=[])
    return {
        "status": "success",
        "count": len(locations),
        "locations": locations,
    }


async def ab_get_applied_jobs_insights() -> dict:
    """Fetch salary insights for jobs you've applied to on Naukri.

    Cross-references Naukri applications with AmbitionBox salary data.
    No parameters needed -- returns insights for your recent applications.
    """
    logger.info("AB REST: fetching applied jobs insights")
    data = await _ab_rest_get(AB_INSIGHTS_APPLIED_API)
    if isinstance(data, list):
        insights = data
    else:
        insights = safe_get(data, "data", field_name="data", warn=False, default=[])
    return {
        "status": "success",
        "count": len(insights),
        "insights": insights,
    }


async def ab_get_salary_rest(company_id: str, job_profile_id: str) -> dict:
    """Fetch detailed salary data (17 fields) for a specific role at a company."""
    logger.info("AB REST: fetching salary for company %s, profile %s", company_id, job_profile_id)
    url = f"{AB_SALARY_API}/{company_id}/jobProfile/{job_profile_id}/salaryData"
    data = await _ab_rest_get(url)
    salary_data = safe_get(data, "data", field_name="data", warn=False, default={})
    return {
        "status": "success",
        "profile_info": safe_get(salary_data, "profileInfo", field_name="profileInfo", warn=False, default={}),
        "summary": safe_get(salary_data, "summaryData", field_name="summaryData", warn=False, default={}),
        "confidence": safe_get(salary_data, "confidence", field_name="confidence", warn=False, default=""),
        "experience_levels": safe_get(salary_data, "experienceLevels", field_name="experienceLevels", warn=False, default=[]),
        "ctc_comparison": safe_get(salary_data, "ctcComparison", field_name="ctcComparison", warn=False, default={}),
        "take_home": safe_get(salary_data, "takeHomeSalary", field_name="takeHomeSalary", warn=False, default={}),
    }
