"""Deep unit tests for _research_company in naukri_server.tools.research.

Covers combo failure permutations, Unicode company names, timeout handling,
and AB REST bridge integration paths.

Every test is PURE: no network, no browser, no file I/O.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# Helpers — build mock returns for the various data sources
# ---------------------------------------------------------------------------

def _ok_search(name="Google", group_id="42"):
    return {"status": "success", "companies": [{"group_id": group_id, "name": name}]}


def _ok_jobs(total=10, count=3, jobs=None):
    if jobs is None:
        jobs = [
            {"title": f"Job {i}", "company": "Google", "company_id": "999"}
            for i in range(count)
        ]
    return {"status": "success", "total": total, "count": count, "jobs": jobs}


def _ok_salary():
    return {"status": "success", "avg_salary": 2500000, "min_salary": 800000, "max_salary": 5000000, "url": "x"}


def _ok_reviews():
    return {"status": "success", "overall_rating": 4.2, "review_count": 500, "url": "y"}


def _ok_interviews():
    return {"status": "success", "total_interviews": 100, "interview_experiences": []}


def _err(msg="network error"):
    return {"status": "error", "message": msg}


def _ab_ok_work_culture():
    return {"status": "success", "work_days": "5 days", "shift": "Day"}


def _ab_ok_benefits():
    return {"status": "success", "health_insurance": True, "gym": False}


# Common patch targets (patch at source module)
_P_SEARCH = "naukri_server.tools.companies._search_companies"
_P_JOBS = "naukri_server.tools.companies._get_company_jobs"
_P_SALARY = "naukri_server.tools.ambitionbox._fetch_salary"
_P_REVIEWS = "naukri_server.tools.ambitionbox._fetch_reviews"
_P_INTERVIEWS = "naukri_server.tools.ambitionbox._fetch_interviews"
_P_AB_CULTURE = "naukri_server.tools.ambitionbox.ab_get_work_culture"
_P_AB_BENEFITS = "naukri_server.tools.ambitionbox.ab_get_benefits"


# =====================================================================
# 1. Combo failure tests — partial failures across data sources
# =====================================================================

class TestComboFailures:
    """Verify that individual data-source failures are collected in errors
    while the rest of the result is still populated."""

    @pytest.mark.asyncio
    async def test_jobs_ok_salary_fails(self):
        """Jobs succeed but salary raises an exception."""
        from naukri_server.tools.research import _research_company

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search()), \
             patch(_P_JOBS, new_callable=AsyncMock, return_value=_ok_jobs()), \
             patch(_P_SALARY, new_callable=AsyncMock, side_effect=RuntimeError("timeout")), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()):
            result = await _research_company(keyword="Google")

        assert result["status"] == "success"
        assert "jobs" in result
        assert "reviews" in result
        assert "salary" not in result
        assert any("Salary" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_jobs_ok_reviews_fail(self):
        """Jobs succeed but reviews returns an error dict."""
        from naukri_server.tools.research import _research_company

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search()), \
             patch(_P_JOBS, new_callable=AsyncMock, return_value=_ok_jobs()), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_err("scrape blocked")), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()):
            result = await _research_company(keyword="Google")

        assert result["status"] == "success"
        assert "salary" in result
        assert "reviews" not in result
        assert any("Reviews" in e and "scrape blocked" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_jobs_ok_interviews_fail(self):
        """Jobs succeed but interviews raises an exception."""
        from naukri_server.tools.research import _research_company

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search()), \
             patch(_P_JOBS, new_callable=AsyncMock, return_value=_ok_jobs()), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, side_effect=ConnectionError("refused")):
            result = await _research_company(keyword="Google")

        assert result["status"] == "success"
        assert "salary" in result
        assert "reviews" in result
        assert "interviews" not in result
        assert any("Interviews" in e and "refused" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_salary_and_reviews_fail_interviews_ok(self):
        """Salary + reviews fail, but interviews succeeds."""
        from naukri_server.tools.research import _research_company

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search()), \
             patch(_P_JOBS, new_callable=AsyncMock, return_value=_ok_jobs()), \
             patch(_P_SALARY, new_callable=AsyncMock, side_effect=TimeoutError("slow")), \
             patch(_P_REVIEWS, new_callable=AsyncMock, side_effect=ValueError("bad json")), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()):
            result = await _research_company(keyword="Google")

        assert result["status"] == "success"
        assert "interviews" in result
        assert "salary" not in result
        assert "reviews" not in result
        errors = result["errors"]
        assert len(errors) >= 2
        assert any("Salary" in e for e in errors)
        assert any("Reviews" in e for e in errors)

    @pytest.mark.asyncio
    async def test_all_ab_fail_jobs_ok(self):
        """All AmbitionBox data fails but jobs succeed — partial success with errors."""
        from naukri_server.tools.research import _research_company

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search()), \
             patch(_P_JOBS, new_callable=AsyncMock, return_value=_ok_jobs()), \
             patch(_P_SALARY, new_callable=AsyncMock, side_effect=RuntimeError("fail")), \
             patch(_P_REVIEWS, new_callable=AsyncMock, side_effect=RuntimeError("fail")), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, side_effect=RuntimeError("fail")):
            result = await _research_company(keyword="Google")

        # Still "success" because jobs worked, but errors collected
        assert result["status"] == "success"
        assert "jobs" in result
        assert "salary" not in result
        assert "reviews" not in result
        assert "interviews" not in result
        assert len(result["errors"]) >= 3

    @pytest.mark.asyncio
    async def test_everything_fails(self):
        """All data sources fail — errors collected for every source."""
        from naukri_server.tools.research import _research_company

        with patch(_P_SEARCH, new_callable=AsyncMock, side_effect=RuntimeError("search down")), \
             patch(_P_SALARY, new_callable=AsyncMock, side_effect=RuntimeError("fail")), \
             patch(_P_REVIEWS, new_callable=AsyncMock, side_effect=RuntimeError("fail")), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, side_effect=RuntimeError("fail")):
            result = await _research_company(keyword="Google")

        # Jobs exception is caught, then AB calls also fail
        assert result["status"] == "success"  # structural status, errors tell the story
        assert "errors" in result
        assert any("Jobs" in e for e in result["errors"])
        assert any("Salary" in e for e in result["errors"])
        assert any("Reviews" in e for e in result["errors"])
        assert any("Interviews" in e for e in result["errors"])


# =====================================================================
# 2. Unicode company names
# =====================================================================

class TestUnicodeCompanyNames:
    """Verify research handles non-ASCII and special-character company names."""

    @pytest.mark.asyncio
    async def test_japanese_company_name(self):
        """Japanese chars should be stripped to empty slug but not crash."""
        from naukri_server.tools.research import _research_company

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search("ソニー株式会社", "77")), \
             patch(_P_JOBS, new_callable=AsyncMock, return_value=_ok_jobs()), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()):
            result = await _research_company(keyword="ソニー株式会社")

        assert result["status"] == "success"
        # derive_slug strips non-ASCII, slug may be empty string
        assert isinstance(result["slug"], str)

    @pytest.mark.asyncio
    async def test_special_chars_ampersand(self):
        """AT&T — ampersand should be converted to hyphen in slug."""
        from naukri_server.tools.research import _research_company

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search("AT&T", "10")), \
             patch(_P_JOBS, new_callable=AsyncMock, return_value=_ok_jobs()), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()):
            result = await _research_company(keyword="AT&T")

        assert result["status"] == "success"
        assert result["slug"] == "at-t"

    @pytest.mark.asyncio
    async def test_special_chars_apostrophe(self):
        """L'Oreal — apostrophe is stripped and slug is normalized."""
        from naukri_server.tools.research import _research_company

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search("L'Oréal", "20")), \
             patch(_P_JOBS, new_callable=AsyncMock, return_value=_ok_jobs()), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()):
            result = await _research_company(keyword="L'Oréal")

        assert result["status"] == "success"
        assert result["slug"] == "l-or-al"

    @pytest.mark.asyncio
    async def test_parentheses_company_name(self):
        """Tata (TCS) — parentheses converted to hyphens."""
        from naukri_server.tools.research import _research_company

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search("Tata (TCS)", "30")), \
             patch(_P_JOBS, new_callable=AsyncMock, return_value=_ok_jobs()), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()):
            result = await _research_company(keyword="Tata (TCS)")

        assert result["status"] == "success"
        assert result["slug"] == "tata-tcs"

    @pytest.mark.asyncio
    async def test_empty_company_name(self):
        """Empty string keyword — should not crash, slug is empty."""
        from naukri_server.tools.research import _research_company

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value={"status": "success", "companies": []}), \
             patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock,
                   return_value={"status": "success", "jobs": [], "total": 0}), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()):
            result = await _research_company(keyword="")

        assert result["status"] == "success"
        assert result["slug"] == ""


# =====================================================================
# 3. Timeout handling
# =====================================================================

class TestTimeoutHandling:
    """Verify asyncio.wait_for timeout wrapping around _do_work."""

    @pytest.mark.asyncio
    async def test_timeout_returns_partial_success(self):
        """When the whole operation times out, return partial_success with TIMEOUT."""
        from naukri_server.tools.research import _research_company

        async def _hang(*args, **kwargs):
            await asyncio.sleep(999)

        with patch(_P_SEARCH, new_callable=AsyncMock, side_effect=_hang), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()):
            result = await _research_company(keyword="SlowCo", timeout_seconds=0)

        assert result["status"] == "partial_success"
        assert result["error_code"] == "TIMEOUT"
        assert "0s" in result["message"]

    @pytest.mark.asyncio
    async def test_very_short_timeout(self):
        """1-second timeout with a slow helper triggers TIMEOUT."""
        from naukri_server.tools.research import _research_company

        async def _slow_search(*a, **kw):
            await asyncio.sleep(10)
            return _ok_search()

        with patch(_P_SEARCH, new_callable=AsyncMock, side_effect=_slow_search), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()):
            result = await _research_company(keyword="SlowCo", timeout_seconds=1)

        assert result["status"] == "partial_success"
        assert result["error_code"] == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_normal_completion_within_timeout(self):
        """Fast helpers complete well within the timeout."""
        from naukri_server.tools.research import _research_company

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search()), \
             patch(_P_JOBS, new_callable=AsyncMock, return_value=_ok_jobs()), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()):
            result = await _research_company(keyword="FastCo", timeout_seconds=120)

        assert result["status"] == "success"
        assert "error_code" not in result


# =====================================================================
# 4. AB REST bridge integration
# =====================================================================

class TestAbRestBridge:
    """Verify AB REST bridge (work_culture + benefits) integration in research."""

    @pytest.mark.asyncio
    async def test_work_culture_and_benefits_fetched(self):
        """When company_id is available on a sample job, AB REST data is fetched."""
        from naukri_server.tools.research import _research_company

        jobs_with_id = [{"title": "Dev", "company": "Google", "company_id": "555"}]

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search()), \
             patch(_P_JOBS, new_callable=AsyncMock,
                   return_value=_ok_jobs(jobs=jobs_with_id)), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()), \
             patch(_P_AB_CULTURE, new_callable=AsyncMock, return_value=_ab_ok_work_culture()) as m_cult, \
             patch(_P_AB_BENEFITS, new_callable=AsyncMock, return_value=_ab_ok_benefits()) as m_ben:
            result = await _research_company(keyword="Google")

        assert result["status"] == "success"
        m_cult.assert_awaited_once_with("555")
        m_ben.assert_awaited_once_with("555")
        assert "work_culture" in result
        assert result["work_culture"]["work_days"] == "5 days"
        assert "benefits" in result
        assert result["benefits"]["health_insurance"] is True

    @pytest.mark.asyncio
    async def test_ab_rest_fails_gracefully(self):
        """AB REST exception is collected in errors but does not break research."""
        from naukri_server.tools.research import _research_company

        jobs_with_id = [{"title": "Dev", "company": "Google", "company_id": "555"}]

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search()), \
             patch(_P_JOBS, new_callable=AsyncMock,
                   return_value=_ok_jobs(jobs=jobs_with_id)), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()), \
             patch(_P_AB_CULTURE, new_callable=AsyncMock, side_effect=RuntimeError("cookie expired")), \
             patch(_P_AB_BENEFITS, new_callable=AsyncMock, side_effect=RuntimeError("403")):
            result = await _research_company(keyword="Google")

        assert result["status"] == "success"
        # Core data still present
        assert "salary" in result
        assert "reviews" in result
        assert "interviews" in result
        # AB REST errors recorded
        assert any("work_culture" in e for e in result["errors"])
        assert any("benefits" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_no_company_id_skips_ab_rest(self):
        """When jobs have no company_id, AB REST calls are skipped entirely."""
        from naukri_server.tools.research import _research_company

        jobs_no_id = [{"title": "Dev", "company": "Google"}]

        with patch(_P_SEARCH, new_callable=AsyncMock, return_value=_ok_search()), \
             patch(_P_JOBS, new_callable=AsyncMock,
                   return_value=_ok_jobs(jobs=jobs_no_id)), \
             patch(_P_SALARY, new_callable=AsyncMock, return_value=_ok_salary()), \
             patch(_P_REVIEWS, new_callable=AsyncMock, return_value=_ok_reviews()), \
             patch(_P_INTERVIEWS, new_callable=AsyncMock, return_value=_ok_interviews()), \
             patch(_P_AB_CULTURE, new_callable=AsyncMock) as m_cult, \
             patch(_P_AB_BENEFITS, new_callable=AsyncMock) as m_ben:
            result = await _research_company(keyword="Google")

        assert result["status"] == "success"
        m_cult.assert_not_awaited()
        m_ben.assert_not_awaited()
        assert "work_culture" not in result
        assert "benefits" not in result
