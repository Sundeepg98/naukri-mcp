"""AmbitionBox REST bridge — direct API calls using browser cookies.

Provides 7 REST helper functions for AmbitionBox service gateways:
  - ab_get_benefits, ab_get_work_culture, ab_get_interview_questions
  - ab_get_competitors, ab_get_locations, ab_get_applied_jobs_insights
  - ab_get_salary_rest

Uses cookies extracted from the persistent Chrome browser context,
cached with 30-minute TTL and auto-refreshed on 401/403.

Used by:
  - ambitionbox.py (_enrich_with_rest) for supplementary data
  - research.py for parallel data fetching
  - daily_brief.py for applied-job salary insights
"""

import asyncio
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

logger = logging.getLogger(__name__)

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


async def _ab_rest_get(url: str, params: dict = None) -> dict:
    """GET request to an AmbitionBox REST endpoint using cached cookies.

    Retries once with fresh cookies on 401/403.
    """
    global _ab_cookies, _ab_cookies_ts
    cookies = await _get_ab_cookies()

    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(cookies=cookies) as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
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
    url = f"{AB_BENEFITS_API}/{company_id}/benefits-stats"
    data = await _ab_rest_get(url)
    benefits_data = data.get("data", {})
    return {
        "status": "success",
        "total_benefits": benefits_data.get("totalBenefits", 0),
        "benefits": benefits_data.get("benefits", []),
    }


async def ab_get_work_culture(company_id: str) -> dict:
    """Fetch work culture distribution (timing, travel, work days, shifts)."""
    url = f"{AB_REVIEW_DIST_API}/{company_id}"
    data = await _ab_rest_get(url)
    dist = data.get("data", {})
    return {
        "status": "success",
        "reviews_count": dist.get("reviewsCount", 0),
        "work_timing": {
            "labels": dist.get("workMonitorLabels", []),
            "values": dist.get("workMonitorSeries", []),
            "insight": dist.get("workMonitorInsight", ""),
        },
        "travel": {
            "labels": dist.get("travelTagsLabels", []),
            "values": dist.get("travelTagsSeries", []),
        },
        "work_days": {
            "labels": dist.get("workDaysLabels", []),
            "values": dist.get("workDaysSeries", []),
        },
        "shifts": {
            "labels": dist.get("shiftsLabels", []),
            "values": dist.get("shiftsSeries", []),
        },
    }


async def ab_get_interview_questions(company_id: str, designation_id: str = None, limit: int = 7) -> dict:
    """Fetch actual interview questions by company and role."""
    params = {"companyId": company_id, "limit": str(limit)}
    if designation_id:
        params["designation"] = designation_id
    data = await _ab_rest_get(AB_INTERVIEW_QS_API, params=params)
    meta = data.get("meta", {})
    return {
        "status": "success",
        "total_interviews": data.get("totalInterviewExperiencesLive", 0),
        "count": meta.get("count", 0),
        "questions": data.get("data", []),
    }


async def ab_get_competitors(company_id: str) -> dict:
    """Fetch top 41 competitors with ratings and review counts."""
    url = f"{AB_COMPANY_COMPARE_API}/{company_id}"
    data = await _ab_rest_get(url)
    competitors = data if isinstance(data, list) else data.get("data", [])
    return {
        "status": "success",
        "count": len(competitors),
        "competitors": competitors,
    }


async def ab_get_locations(company_id: str) -> dict:
    """Fetch all office locations for a company."""
    url = f"{AB_COMPANY_LOCATIONS_API}/{company_id}/sectionalDetails"
    data = await _ab_rest_get(url, params={"sections": "companyLocations"})
    locations = data.get("companyLocations", [])
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
    data = await _ab_rest_get(AB_INSIGHTS_APPLIED_API)
    insights = data if isinstance(data, list) else data.get("data", [])
    return {
        "status": "success",
        "count": len(insights),
        "insights": insights,
    }


async def ab_get_salary_rest(company_id: str, job_profile_id: str) -> dict:
    """Fetch detailed salary data (17 fields) for a specific role at a company."""
    url = f"{AB_SALARY_API}/{company_id}/jobProfile/{job_profile_id}/salaryData"
    data = await _ab_rest_get(url)
    salary_data = data.get("data", {})
    summary = salary_data.get("summaryData", {})
    return {
        "status": "success",
        "profile_info": salary_data.get("profileInfo", {}),
        "summary": summary,
        "confidence": salary_data.get("confidence", ""),
        "experience_levels": salary_data.get("experienceLevels", []),
        "ctc_comparison": salary_data.get("ctcComparison", {}),
        "take_home": salary_data.get("takeHomeSalary", {}),
    }
