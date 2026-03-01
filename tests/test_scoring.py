"""Tests for naukri_server.scoring — skill normalization and fit scoring."""

from naukri_server.scoring import normalize_skill, parse_skills, compute_fit_score, _score_salary, _score_location


class TestNormalizeSkill:
    def test_canonical_passthrough(self):
        assert normalize_skill("python") == "python"

    def test_alias_resolution(self):
        assert normalize_skill("JS") == "javascript"
        assert normalize_skill("k8s") == "kubernetes"
        assert normalize_skill("ReactJS") == "react"

    def test_unknown_passthrough(self):
        assert normalize_skill("obscure-framework") == "obscure-framework"

    def test_whitespace_handling(self):
        assert normalize_skill("  Python  ") == "python"


class TestParseSkills:
    def test_comma_separated_string(self):
        result = parse_skills("Python, JS, React")
        assert "python" in result
        assert "javascript" in result
        assert "react" in result

    def test_list_input(self):
        result = parse_skills(["Python", "Docker", "k8s"])
        assert result == {"python", "docker", "kubernetes"}

    def test_set_input(self):
        result = parse_skills({"Python", "Go"})
        assert "python" in result
        assert "golang" in result

    def test_empty_inputs(self):
        assert parse_skills("") == set()
        assert parse_skills([]) == set()
        assert parse_skills(None) == set()
        assert parse_skills(42) == set()

    def test_tuple_input(self):
        result = parse_skills(("Python", "Node"))
        assert "python" in result
        assert "node.js" in result


class TestComputeFitScore:
    def test_perfect_skill_match(self):
        job = {"python", "react", "docker"}
        profile = {"python", "react", "docker", "linux"}
        result = compute_fit_score(job, profile, "3-5 years", "4 years")
        assert result["overall_score"] >= 80
        assert len(result["skill_match"]["matched"]) == 3
        assert len(result["skill_match"]["missing"]) == 0

    def test_no_skill_overlap(self):
        job = {"java", "spring boot"}
        profile = {"python", "django"}
        result = compute_fit_score(job, profile, "3-5 years", "4 years")
        assert result["overall_score"] < 50
        assert len(result["skill_match"]["missing"]) == 2

    def test_experience_in_range(self):
        result = compute_fit_score(
            {"python"}, {"python"},
            "3-5 years", "4 years",
            experience_min=3, experience_max=5,
        )
        assert result["experience_match"]["score"] == 100

    def test_experience_below_range(self):
        result = compute_fit_score(
            {"python"}, {"python"},
            "5-8 years", "2 years",
            experience_min=5, experience_max=8,
        )
        assert result["experience_match"]["score"] < 100

    def test_bonuses_included(self):
        result = compute_fit_score(
            {"python"}, {"python"},
            "3-5 years", "4 years",
            job_location="Bangalore",
            profile_location="Bangalore",
            job_work_mode="WFH",
            job_salary="15-25 LPA",
            profile_expected_ctc=20,
        )
        assert "bonuses" in result
        assert result["bonuses"]["location"] == 5
        assert result["bonuses"]["work_mode"] == 5
        assert result["bonuses"]["salary"] == 5

    def test_recommendation_tiers(self):
        result = compute_fit_score({"python"}, {"python"}, "3-5", "4",
                                    experience_min=3, experience_max=5)
        assert "Strong" in result["recommendation"] or "Good" in result["recommendation"]


class TestScoreSalary:
    def test_not_disclosed(self):
        assert _score_salary("Not Disclosed", 15) == 0

    def test_meets_expectation(self):
        assert _score_salary("15-25 LPA", 20) == 5

    def test_below_expectation(self):
        assert _score_salary("5-10 LPA", 20) == 0

    def test_within_20_percent(self):
        assert _score_salary("10-17 LPA", 20) == 3

    def test_string_ctc(self):
        assert _score_salary("15-25 LPA", "20.0 Lacs") == 5

    def test_none_inputs(self):
        assert _score_salary(None, 15) == 0
        assert _score_salary("15-25 LPA", None) == 0


class TestScoreLocation:
    def test_exact_match(self):
        assert _score_location("Bangalore", "Bangalore") == 5

    def test_substring_match(self):
        assert _score_location("Bangalore/Bengaluru", "Bangalore") == 5

    def test_remote(self):
        assert _score_location("Remote", "Mumbai") == 5

    def test_no_match(self):
        assert _score_location("Chennai", "Mumbai") == 0

    def test_none_inputs(self):
        assert _score_location(None, "Mumbai") == 0
        assert _score_location("Bangalore", None) == 0
