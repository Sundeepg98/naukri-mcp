"""Deep tests for naukri_server.scoring — agent-eligible scoring bonus.

Every test is PURE: no network, no browser, no file I/O.
Recovered from deleted tier24.py.
"""

import pytest


# ---------------------------------------------------------------------------
# Agent-eligible scoring
# ---------------------------------------------------------------------------

class TestAgentEligibleScoring:
    def _score(self, is_agent_eligible=None, job_skills=None, profile_skills=None):
        from naukri_server.scoring import compute_fit_score
        return compute_fit_score(
            job_skills=job_skills or {"python"},
            profile_skills=profile_skills or {"python"},
            job_exp_str="2-4 years",
            profile_exp=3,
            is_agent_eligible=is_agent_eligible,
        )

    def test_agent_eligible_true_adds_5(self):
        result = self._score(is_agent_eligible=True)
        assert result["bonuses"]["agent_eligible"] == 5

    def test_agent_eligible_false_adds_0(self):
        result = self._score(is_agent_eligible=False)
        assert result["bonuses"]["agent_eligible"] == 0

    def test_agent_eligible_none_no_bonuses(self):
        """is_agent_eligible=None (default) omits bonuses dict."""
        from naukri_server.scoring import compute_fit_score
        result = compute_fit_score(
            job_skills={"python"}, profile_skills={"python"},
            job_exp_str="2-4 years", profile_exp=3,
        )
        assert "bonuses" not in result

    def test_agent_eligible_true_score_higher_than_false(self):
        """All else equal, is_agent_eligible=True yields 5 points more."""
        from naukri_server.scoring import compute_fit_score
        job_skills = {"python", "java", "golang", "rust", "scala"}
        profile_skills = {"python"}
        result_true = compute_fit_score(job_skills, profile_skills, "2-4 years", 3, is_agent_eligible=True)
        result_false = compute_fit_score(job_skills, profile_skills, "2-4 years", 3, is_agent_eligible=False)
        assert result_true["overall_score"] == result_false["overall_score"] + 5

    def test_total_score_capped_at_100(self):
        from naukri_server.scoring import compute_fit_score
        result = compute_fit_score(
            job_skills={"python", "django", "rest api"},
            profile_skills={"python", "django", "rest api"},
            job_exp_str="2-4 years", profile_exp=3,
            experience_min=2, experience_max=4, is_agent_eligible=True,
        )
        assert result["overall_score"] <= 100

    def test_agent_eligible_none_with_location_triggers_bonuses(self):
        """When other enrichment data provided but agent_eligible=None, bonus is 0."""
        from naukri_server.scoring import compute_fit_score
        result = compute_fit_score(
            job_skills={"python"}, profile_skills={"python"},
            job_exp_str="2-4 years", profile_exp=3,
            job_location="Bangalore", profile_location="Bangalore",
            is_agent_eligible=None,
        )
        assert "bonuses" in result
        assert result["bonuses"]["agent_eligible"] == 0

    def test_works_with_minimal_args(self):
        from naukri_server.scoring import compute_fit_score
        result = compute_fit_score(
            job_skills={"python"}, profile_skills={"python"},
            job_exp_str="", profile_exp=None, is_agent_eligible=True,
        )
        assert isinstance(result, dict)
        assert result["bonuses"]["agent_eligible"] == 5
