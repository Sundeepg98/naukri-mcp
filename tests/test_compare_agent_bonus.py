"""naukri_compare_jobs must score a job the same way every other tool does.

THE FINDING. `compare.py` was the only scorer in the package that did not pass
`is_agent_eligible`. `auto_hunt.py` and `smart_apply.py` both did. So an
agent-eligible job scored FIVE POINTS LOWER when compared than when hunted or
assessed — inside the one tool whose entire purpose is comparability, with
nothing in the output saying so.

WHICH WAS RIGHT: auto_hunt and smart_apply. `Job.is_agent_eligible` is parsed
out of the very API dict `_compare_jobs` already turns into a `Job`, so the
omission bought nothing. And the bonus is part of what a naukri fit score MEANS
— dropping it in one tool does not make that tool's number purer, it makes it a
different number wearing the same name.

This is a real behaviour change, shipped on its own so it is revertible on its
own: an agent-eligible job now compares 5 points higher than it did yesterday.

All PURE: mocked job detail + profile. No network, no browser.
"""

import pytest
from unittest.mock import AsyncMock, patch


def _job(job_id="J1", agent_eligible=False):
    return {
        "status": "success",
        "job_id": job_id,
        "title": "Backend Engineer",
        "company": "Acme",
        "company_rating": 4.0,
        "is_applied": False,
        "tags": ["Python", "Django", "Kubernetes"],
        "skills": None,
        "experience": "2-5 years",
        "experience_min": 2,
        "experience_max": 5,
        "location": "Bangalore",
        "work_mode": "Work from office",
        "salary": "Not disclosed",
        "group_id": None,
        "vacancies": 1,
        "external_apply": False,
        "external_apply_url": None,
        "posted_date": "2026-08-01",
        "apply_count": 10,
        "candidates_count": 20,
        "is_agent_eligible": agent_eligible,
    }


_PROFILE = {
    "status": "success",
    "key_skills": ["Python", "Django", "AWS"],
    "total_experience": "4 years 0 months",
    "current_location": "Bangalore",
    "expected_ctc": None,
}


async def _compare_score(agent_eligible: bool) -> dict:
    from naukri_server.tools.compare import _compare_jobs

    with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock,
               side_effect=[_job("J1", agent_eligible), _job("J2", agent_eligible)]), \
         patch("naukri_server.tools.profile.get_cached_profile",
               new_callable=AsyncMock, return_value=_PROFILE), \
         patch("naukri_server.database.get_applied_job_ids",
               new_callable=AsyncMock, return_value=set()):
        result = await _compare_jobs(["J1", "J2"], timeout_seconds=10)
    assert result["status"] == "success", result
    return result["jobs"][0]


async def _hunt_score(agent_eligible: bool) -> int:
    from naukri_server.tools.auto_hunt import naukri_auto_hunt

    with patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock,
               return_value={"status": "success", "jobs": [_job("J1", agent_eligible)]}), \
         patch("naukri_server.tools.profile.get_cached_profile",
               new_callable=AsyncMock, return_value=_PROFILE), \
         patch("naukri_server.database.get_applied_job_ids",
               new_callable=AsyncMock, return_value=set()):
        result = await naukri_auto_hunt(keywords="python", min_fit_score=0)
    assert result["status"] == "success", result
    return result["ranked_jobs"][0]["fit_score"]


class TestCompareAgreesWithHunt:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_eligible", [False, True])
    async def test_same_job_same_score(self, agent_eligible):
        """The property that matters, in both states of the flag.

        Fails against the pre-fix tree for agent_eligible=True: compare returned
        85 where auto_hunt returned 90.
        """
        compared = await _compare_score(agent_eligible)
        hunted = await _hunt_score(agent_eligible)
        assert compared["fit_score"] == hunted, (
            f"is_agent_eligible={agent_eligible}: compare said "
            f"{compared['fit_score']}, auto_hunt said {hunted}"
        )

    @pytest.mark.asyncio
    async def test_the_bonus_is_worth_five_and_is_therefore_visible(self):
        """CONTROL. If the flag moved nothing, the test above would pass whether
        or not compare passed it — the assertion has to be able to fail."""
        off = (await _compare_score(False))["fit_score"]
        on = (await _compare_score(True))["fit_score"]
        assert on - off == 5, (off, on)

    @pytest.mark.asyncio
    async def test_the_bonus_breakdown_reports_it(self):
        """A five-point difference the caller cannot see is the disease, not the
        cure. `bonuses.agent_eligible` names it."""
        entry = await _compare_score(True)
        assert entry["bonuses"]["agent_eligible"] == 5
        assert entry["bonuses"]["total"] >= 5
