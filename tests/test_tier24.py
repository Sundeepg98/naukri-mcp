"""Tests for Tier 24 Phase 1 feature changes:
  1. daily_brief._build_competition_section — applicant count bucketing
  2. scoring.compute_fit_score — is_agent_eligible parameter and bonus
  3. smart_apply._apply_top_fits — agent-eligible sort priority
  4. daily_brief._build_recommended_actions — high-competition action

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch


# ===========================================================================
# 1. TestCompetitionSection
# ===========================================================================

class TestCompetitionSection:
    """_build_competition_section — bucket applications by applicant count."""

    def _call(self, apps_result):
        from naukri_server.tools.daily_brief import _build_competition_section
        return _build_competition_section(apps_result)

    def test_none_input_returns_empty_section(self):
        """None apps_result returns zero-counts section with empty top_competitive."""
        result = self._call(None)
        assert result["total_with_data"] == 0
        assert result["low"] == 0
        assert result["medium"] == 0
        assert result["high"] == 0
        assert result["very_high"] == 0
        assert result["average_applicants"] is None
        assert result["top_competitive"] == []

    def test_empty_applications_list_returns_zeroes(self):
        """Dict with empty applications list returns zero counts."""
        result = self._call({"applications": []})
        assert result["total_with_data"] == 0
        assert result["average_applicants"] is None
        assert result["top_competitive"] == []

    def test_apps_with_no_total_applicants_field_all_zeroes(self):
        """Applications missing total_applicants are skipped — all bucket counts zero."""
        apps = [
            {"job_id": "J1", "title": "SWE", "company": "Acme"},
            {"job_id": "J2", "title": "PM", "company": "Beta"},
        ]
        result = self._call({"applications": apps})
        assert result["total_with_data"] == 0
        assert result["low"] == 0
        assert result["medium"] == 0
        assert result["high"] == 0
        assert result["very_high"] == 0
        assert result["average_applicants"] is None

    def test_mixed_some_have_total_applicants_some_dont(self):
        """Only apps with total_applicants contribute to the buckets."""
        apps = [
            {"job_id": "J1", "total_applicants": 30},   # low
            {"job_id": "J2"},                            # no field — skipped
            {"job_id": "J3", "total_applicants": None},  # None — skipped
            {"job_id": "J4", "total_applicants": 100},   # medium
        ]
        result = self._call({"applications": apps})
        assert result["total_with_data"] == 2
        assert result["low"] == 1
        assert result["medium"] == 1
        assert result["high"] == 0
        assert result["very_high"] == 0

    def test_bucket_boundary_50_is_low(self):
        """Exactly 50 applicants falls in the 'low' bucket (0-50 inclusive)."""
        result = self._call({"applications": [{"job_id": "J1", "total_applicants": 50}]})
        assert result["low"] == 1
        assert result["medium"] == 0

    def test_bucket_boundary_51_is_medium(self):
        """51 applicants is the first value in the 'medium' bucket (51-200)."""
        result = self._call({"applications": [{"job_id": "J1", "total_applicants": 51}]})
        assert result["low"] == 0
        assert result["medium"] == 1

    def test_bucket_boundary_200_is_medium_and_201_is_high(self):
        """200 → medium, 201 → high boundary."""
        apps = [
            {"job_id": "J1", "total_applicants": 200},
            {"job_id": "J2", "total_applicants": 201},
        ]
        result = self._call({"applications": apps})
        assert result["medium"] == 1
        assert result["high"] == 1

    def test_bucket_boundary_500_is_high_and_501_is_very_high(self):
        """500 → high, 501 → very_high boundary."""
        apps = [
            {"job_id": "J1", "total_applicants": 500},
            {"job_id": "J2", "total_applicants": 501},
        ]
        result = self._call({"applications": apps})
        assert result["high"] == 1
        assert result["very_high"] == 1

    def test_top_competitive_returns_top_3_sorted_descending(self):
        """top_competitive contains at most 3 entries sorted by applicant count desc."""
        apps = [
            {"job_id": "J1", "title": "T1", "company": "C1", "total_applicants": 100},
            {"job_id": "J2", "title": "T2", "company": "C2", "total_applicants": 600},
            {"job_id": "J3", "title": "T3", "company": "C3", "total_applicants": 300},
            {"job_id": "J4", "title": "T4", "company": "C4", "total_applicants": 50},
        ]
        result = self._call({"applications": apps})
        top = result["top_competitive"]
        assert len(top) == 3
        assert top[0]["total_applicants"] == 600
        assert top[1]["total_applicants"] == 300
        assert top[2]["total_applicants"] == 100

    def test_average_applicants_rounded_correctly(self):
        """average_applicants is rounded to 1 decimal place."""
        apps = [
            {"job_id": "J1", "total_applicants": 10},
            {"job_id": "J2", "total_applicants": 20},
            {"job_id": "J3", "total_applicants": 30},
        ]
        result = self._call({"applications": apps})
        # (10 + 20 + 30) / 3 = 20.0
        assert result["average_applicants"] == 20.0

    def test_average_applicants_non_trivial_rounding(self):
        """average_applicants rounded to 1 decimal (e.g. 33.3 not 33.333...)."""
        apps = [
            {"job_id": "J1", "total_applicants": 10},
            {"job_id": "J2", "total_applicants": 20},
            {"job_id": "J3", "total_applicants": 50},
        ]
        result = self._call({"applications": apps})
        # (10 + 20 + 50) / 3 = 26.666... → rounded to 26.7
        assert result["average_applicants"] == round((10 + 20 + 50) / 3, 1)


# ===========================================================================
# 2. TestAgentEligibleScoring
# ===========================================================================

class TestAgentEligibleScoring:
    """compute_fit_score — is_agent_eligible parameter adds +5 agent bonus."""

    def _score(self, is_agent_eligible=None, job_skills=None, profile_skills=None):
        from naukri_server.scoring import compute_fit_score
        return compute_fit_score(
            job_skills=job_skills or {"python"},
            profile_skills=profile_skills or {"python"},
            job_exp_str="2-4 years",
            profile_exp=3,
            is_agent_eligible=is_agent_eligible,
        )

    def test_agent_eligible_true_adds_5_to_total_bonus(self):
        """is_agent_eligible=True → agent_eligible bonus is 5."""
        result = self._score(is_agent_eligible=True)
        assert "bonuses" in result
        assert result["bonuses"]["agent_eligible"] == 5

    def test_agent_eligible_false_adds_0_to_total_bonus(self):
        """is_agent_eligible=False → agent_eligible bonus is 0."""
        result = self._score(is_agent_eligible=False)
        assert "bonuses" in result
        assert result["bonuses"]["agent_eligible"] == 0

    def test_agent_eligible_none_adds_0_and_bonuses_absent(self):
        """is_agent_eligible=None (default) → bonuses dict not included in result."""
        from naukri_server.scoring import compute_fit_score
        result = compute_fit_score(
            job_skills={"python"},
            profile_skills={"python"},
            job_exp_str="2-4 years",
            profile_exp=3,
            # No is_agent_eligible — default None
        )
        # bonuses should NOT be present when no enrichment data provided at all
        assert "bonuses" not in result

    def test_agent_eligible_true_score_higher_than_false(self):
        """All else equal, is_agent_eligible=True yields a score 5 points higher.

        Uses a partial skill overlap so the base score is well below 95,
        ensuring the +5 bonus does not hit the cap and the delta is exactly 5.
        """
        from naukri_server.scoring import compute_fit_score
        # Job requires 5 skills; profile matches only 1 → ~20% skill score
        # base ≈ 20*0.6 + 50*0.4 = 12 + 20 = 32 → well below 95
        job_skills = {"python", "java", "golang", "rust", "scala"}
        profile_skills = {"python"}
        result_true = compute_fit_score(
            job_skills, profile_skills, "2-4 years", 3, is_agent_eligible=True
        )
        result_false = compute_fit_score(
            job_skills, profile_skills, "2-4 years", 3, is_agent_eligible=False
        )
        assert result_true["overall_score"] == result_false["overall_score"] + 5

    def test_agent_eligible_bonus_appears_in_bonuses_dict(self):
        """bonuses dict includes 'agent_eligible' key when is_agent_eligible is not None."""
        result = self._score(is_agent_eligible=True)
        assert "agent_eligible" in result["bonuses"]

    def test_total_score_capped_at_100_even_with_agent_bonus(self):
        """Overall score never exceeds 100 even when agent bonus pushes it over."""
        # Perfect skill match + in-range experience = ~100 base; bonus should not go over 100
        from naukri_server.scoring import compute_fit_score
        result = compute_fit_score(
            job_skills={"python", "django", "rest api"},
            profile_skills={"python", "django", "rest api"},
            job_exp_str="2-4 years",
            profile_exp=3,
            experience_min=2,
            experience_max=4,
            is_agent_eligible=True,
        )
        assert result["overall_score"] <= 100

    def test_agent_eligible_none_explicit_triggers_bonuses_when_other_enrichment_present(self):
        """When other enrichment data provided but is_agent_eligible=None, bonus is 0."""
        from naukri_server.scoring import compute_fit_score
        result = compute_fit_score(
            job_skills={"python"},
            profile_skills={"python"},
            job_exp_str="2-4 years",
            profile_exp=3,
            job_location="Bangalore",
            profile_location="Bangalore",
            is_agent_eligible=None,
        )
        # bonuses present due to location data, agent_eligible should be 0
        assert "bonuses" in result
        assert result["bonuses"]["agent_eligible"] == 0

    def test_agent_eligible_works_with_minimal_args(self):
        """compute_fit_score works with only skills + is_agent_eligible."""
        from naukri_server.scoring import compute_fit_score
        result = compute_fit_score(
            job_skills={"python"},
            profile_skills={"python"},
            job_exp_str="",
            profile_exp=None,
            is_agent_eligible=True,
        )
        assert isinstance(result, dict)
        assert "overall_score" in result
        assert result["bonuses"]["agent_eligible"] == 5


# ===========================================================================
# 3. TestApplyTopFitsPriority
# ===========================================================================

class TestApplyTopFitsPriority:
    """_apply_top_fits — agent-eligible jobs sorted before non-eligible at equal scores."""

    def _make_scored_jobs(self, specs):
        """Build scored_jobs list from specs: (job_id, fit_score, agent_eligible_bonus)."""
        jobs = []
        for job_id, fit_score, agent_bonus in specs:
            jobs.append({
                "job_id": job_id,
                "title": f"Job {job_id}",
                "company": "Co",
                "fit_score": fit_score,
                "fit_details": {
                    "bonuses": {"agent_eligible": agent_bonus},
                },
            })
        return jobs

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_agent_eligible_job_sorted_before_non_eligible_at_equal_score(
        self, mock_bulk, mock_apply
    ):
        """Two jobs at score 80: agent-eligible should be applied first."""
        mock_bulk.return_value = {
            "status": "success",
            "total_saved": 2,
            "scored_jobs": self._make_scored_jobs([
                ("J_non", 80, 0),   # non-eligible, same score
                ("J_elig", 80, 5),  # agent-eligible, same score
            ]),
        }
        mock_apply.return_value = {"status": "applied"}

        from naukri_server.tools.smart_apply import _apply_top_fits
        result = await _apply_top_fits(min_fit_score=60, limit=1)

        # Only one job applied — should be the agent-eligible one
        assert result["applied"] == 1
        assert result["results"][0]["job_id"] == "J_elig"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_agent_eligible_ranks_above_higher_scored_non_eligible(
        self, mock_bulk, mock_apply
    ):
        """Agent-eligible job ranks first even when non-eligible has a higher fit_score.

        The sort key is (not agent_eligible_bonus, -fit_score).
        not 5 = False, not 0 = True — False < True — so eligible always leads.
        """
        mock_bulk.return_value = {
            "status": "success",
            "total_saved": 2,
            "scored_jobs": self._make_scored_jobs([
                ("J_elig", 70, 5),   # agent-eligible, lower score
                ("J_non",  90, 0),   # non-eligible, higher score
            ]),
        }
        mock_apply.return_value = {"status": "applied"}

        from naukri_server.tools.smart_apply import _apply_top_fits
        result = await _apply_top_fits(min_fit_score=60, limit=1)

        # Agent-eligible job wins even though it has a lower score
        assert result["results"][0]["job_id"] == "J_elig"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_full_ordering_eligible_then_score_descending(
        self, mock_bulk, mock_apply
    ):
        """Verify sort: eligible-90, eligible-80, non-eligible-95, non-eligible-70.
        Expected order: eligible-90, eligible-80, non-eligible-95, non-eligible-70.
        Key: (not agent_eligible, -fit_score) — eligible jobs (False) always before
        non-eligible (True), then score descending within each group.
        """
        mock_bulk.return_value = {
            "status": "success",
            "total_saved": 4,
            "scored_jobs": self._make_scored_jobs([
                ("JE90", 90, 5),
                ("JE80", 80, 5),
                ("JN95", 95, 0),
                ("JN70", 70, 0),
            ]),
        }
        mock_apply.return_value = {"status": "applied"}

        from naukri_server.tools.smart_apply import _apply_top_fits
        result = await _apply_top_fits(min_fit_score=60, limit=4)

        applied_ids = [r["job_id"] for r in result["results"]]
        # Agent-eligible first (score-sorted within group), then non-eligible
        assert applied_ids.index("JE90") < applied_ids.index("JN95")
        assert applied_ids.index("JE80") < applied_ids.index("JN70")

    @pytest.mark.asyncio
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_bulk_scoring_error_propagated(self, mock_bulk):
        """If _bulk_saved_scoring returns error, _apply_top_fits returns it unchanged."""
        mock_bulk.return_value = {
            "status": "error",
            "message": "timeout",
            "error_code": "API_ERROR",
        }

        from naukri_server.tools.smart_apply import _apply_top_fits
        result = await _apply_top_fits(min_fit_score=60)
        assert result["status"] == "error"
        assert "timeout" in result["message"]


# ===========================================================================
# 4. TestCompetitionRecommendedAction
# ===========================================================================

class TestCompetitionRecommendedAction:
    """_build_recommended_actions — high competition triggers a medium-priority action."""

    def _base_brief(self):
        """Minimal brief with all conditions inert except competition_overview."""
        return {
            "unread_messages": {"count": 0},
            "due_reminders": {"count": 0},
            "stale_applications": {"count": 0},
            "notification_summary": {"categories": {}},
            "early_access_roles": {"newly_posted_count": 0},
            "assessments": {"pending": 0},
        }

    def test_avg_above_200_triggers_medium_action(self):
        """average_applicants > 200 produces a medium-priority competition action."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = self._base_brief()
        brief["competition_overview"] = {"average_applicants": 350}
        actions = _build_recommended_actions(brief)
        competition_actions = [
            a for a in actions
            if "competition" in a["action"].lower() or "applicant" in a["action"].lower()
        ]
        assert len(competition_actions) == 1
        assert competition_actions[0]["priority"] == "medium"
        assert "350" in competition_actions[0]["action"]

    def test_avg_exactly_200_does_not_trigger_action(self):
        """average_applicants == 200 does NOT trigger the competition action (> 200 required)."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = self._base_brief()
        brief["competition_overview"] = {"average_applicants": 200}
        actions = _build_recommended_actions(brief)
        competition_actions = [
            a for a in actions
            if "competition" in a["action"].lower() or "applicant" in a["action"].lower()
        ]
        assert len(competition_actions) == 0

    def test_avg_below_200_does_not_trigger_action(self):
        """average_applicants < 200 does NOT trigger the competition action."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = self._base_brief()
        brief["competition_overview"] = {"average_applicants": 100}
        actions = _build_recommended_actions(brief)
        competition_actions = [
            a for a in actions
            if "competition" in a["action"].lower() or "applicant" in a["action"].lower()
        ]
        assert len(competition_actions) == 0

    def test_missing_competition_overview_is_safe(self):
        """Brief without competition_overview key does not raise."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = self._base_brief()
        # No competition_overview key at all
        actions = _build_recommended_actions(brief)
        # Should return without error; no competition action expected
        assert isinstance(actions, list)

    def test_competition_overview_with_none_avg_is_safe(self):
        """average_applicants=None in competition_overview does not trigger action."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = self._base_brief()
        brief["competition_overview"] = {"average_applicants": None}
        actions = _build_recommended_actions(brief)
        competition_actions = [
            a for a in actions
            if "competition" in a["action"].lower() or "applicant" in a["action"].lower()
        ]
        assert len(competition_actions) == 0
