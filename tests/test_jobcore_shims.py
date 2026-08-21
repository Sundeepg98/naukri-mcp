"""The scoring modules are re-export shims over the shared ``jobcore`` package.

These tests are the contract that lets jobcore be edited without silently
changing what this server scores. They fail if:
  - any previously-working import path stops resolving,
  - naukri stops being wired to jobcore (a copy crept back in),
  - the Naukri salary unit stops following ``config.LAKHS_MULTIPLIER``.

All PURE: no network, no browser, no file I/O.
"""

import ast
import importlib

import pytest


def _imported_names(node) -> list[str]:
    """Every module-ish name an AST node imports.

    ``ast.Import`` and ``ast.ImportFrom`` carry ``names`` as ``ast.alias``
    objects with a ``.name``. **``ast.Global`` and ``ast.Nonlocal`` also carry
    ``names``, and theirs are plain strings** — so the obvious
    ``[a.name for a in getattr(node, "names", [])]`` raises
    ``AttributeError: 'str' object has no attribute 'name'`` the moment any
    scanned module contains a ``global`` statement. The guard below exists to
    prove jobcore never imports naukri; a guard that crashes on an unrelated
    keyword cannot check anything at all.
    """
    return [a.name for a in getattr(node, "names", []) if isinstance(a, ast.alias)]


# ---------------------------------------------------------------------------
# 1. Every import path that existed before the extraction still resolves
# ---------------------------------------------------------------------------

class TestLegacyImportPaths:
    def test_scoring_public_names(self):
        from naukri_server.scoring import (  # noqa: F401
            compute_fit_score,
            normalize_skill,
            parse_skills,
        )

    def test_scoring_private_bonus_helpers(self):
        """tools/auto_hunt, tools/compare and tools/smart_apply import these."""
        from naukri_server.scoring import (  # noqa: F401
            _score_location,
            _score_salary,
            _score_work_mode,
        )

    def test_scoring_reexports_taxonomy_names(self):
        from naukri_server.scoring import DEFAULT_TAXONOMY, SKILL_ALIASES, FitScore

        assert SKILL_ALIASES
        assert DEFAULT_TAXONOMY.canonical_count >= 80
        assert FitScore is not None

    def test_fit_score_aggregate_names(self):
        from naukri_server.domain.fit_score import (  # noqa: F401
            BonusScore,
            ExperienceScore,
            FitScore,
            SkillMatch,
        )

    def test_skill_taxonomy_names(self):
        from naukri_server.domain.skill_taxonomy import (  # noqa: F401
            DEFAULT_TAXONOMY,
            SKILL_ALIASES,
            SkillTaxonomy,
        )

    def test_salary_name(self):
        from naukri_server.domain.salary import Salary  # noqa: F401


# ---------------------------------------------------------------------------
# 2. The wiring is real — these are jobcore's objects, not a local copy
# ---------------------------------------------------------------------------

class TestActuallyWiredToJobcore:
    def test_fit_score_is_jobcore_class(self):
        import jobcore.fit
        from naukri_server.domain.fit_score import FitScore

        assert FitScore is jobcore.fit.FitScore

    def test_taxonomy_is_the_shared_singleton(self):
        import jobcore.skills
        from naukri_server.domain.skill_taxonomy import DEFAULT_TAXONOMY, SKILL_ALIASES

        assert DEFAULT_TAXONOMY is jobcore.skills.DEFAULT_TAXONOMY
        assert SKILL_ALIASES is jobcore.skills.SKILL_ALIASES

    def test_salary_subclasses_jobcore_salary(self):
        import jobcore.salary
        from naukri_server.domain.salary import Salary

        assert issubclass(Salary, jobcore.salary.Salary)

    def test_jobcore_does_not_import_naukri(self):
        """The dependency arrow points one way only."""
        import jobcore

        src = __import__("pathlib").Path(jobcore.__file__).parent
        scanned = 0
        for path in src.glob("*.py"):
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mod = getattr(node, "module", None) or ""
                names = _imported_names(node)
                assert not mod.startswith("naukri"), f"{path.name}: {mod}"
                assert not any(n.startswith("naukri") for n in names), path.name
        assert scanned >= 5, f"scanned only {scanned} jobcore modules — did the glob break?"

    def test_the_guard_survives_a_global_statement(self):
        """CONTROL for the line above. Shown failing before it was trusted.

        ``[a.name for a in node.names]`` crashes on ``ast.Global``/``Nonlocal``
        (plain strings, not aliases). jobcore's config loader gained a
        ``global`` on 2026-08-21 and turned the import guard red with an
        ``AttributeError`` — a guard one keyword away from being unable to
        check the thing it exists to check.
        """
        for source in ("global _x", "def f():\n    x = 1\n    def g():\n        nonlocal x\n"):
            for node in ast.walk(ast.parse(source)):
                assert _imported_names(node) == []

    def test_the_guard_still_sees_a_real_import(self):
        """And the fix must not make the guard blind. Both directions pinned."""
        tree = ast.parse("import naukri_server.config\nfrom naukri_server import mcp")
        found = [n for node in ast.walk(tree) for n in _imported_names(node)]
        found += [
            getattr(node, "module", "") or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        assert any(n.startswith("naukri") for n in found)


# ---------------------------------------------------------------------------
# 3. Naukri's salary unit is injected, and it follows config
# ---------------------------------------------------------------------------

class TestSalaryUnitsAreNaukris:
    def test_config_multiplier_is_bound(self):
        from naukri_server.config import LAKHS_MULTIPLIER
        from naukri_server.domain.salary import Salary

        assert Salary.CONFIG.lakhs_multiplier == LAKHS_MULTIPLIER

    def test_raw_rupees_convert_to_lakhs(self):
        from naukri_server.domain.salary import Salary

        assert Salary.from_string("1500000").max_lakhs == 15.0
        assert Salary.from_string("10-15 Lacs").max_lakhs == 15.0

    def test_undisclosed_is_none_not_zero(self):
        """An empty result must be distinguishable from a real zero salary."""
        from naukri_server.domain.salary import Salary

        s = Salary.from_string("Not disclosed")
        assert s.min_lakhs is None and s.max_lakhs is None
        assert s.is_disclosed is False

    def test_a_different_multiplier_would_change_the_answer(self):
        """Proves the binding is live, not a coincidence of equal defaults."""
        from jobcore.salary import Salary as JobcoreSalary
        from jobcore.salary import SalaryConfig

        class Thousands(JobcoreSalary):
            CONFIG = SalaryConfig(lakhs_multiplier=1_000.0)

        assert Thousands.from_string("1500000").max_lakhs == 1500.0


# ---------------------------------------------------------------------------
# 4. The scoring surface still behaves
# ---------------------------------------------------------------------------

class TestScoringBehaviourUnchanged:
    def test_normalize_and_parse(self):
        from naukri_server.scoring import normalize_skill, parse_skills

        assert normalize_skill("ReactJS") == "react"
        assert normalize_skill("  JS ") == "javascript"
        assert parse_skills("React, Node.js") == {"react", "node.js"}
        assert parse_skills(["reactjs", "NODEJS"]) == {"react", "node.js"}

    def test_perfect_match_scores_100(self):
        from naukri_server.scoring import compute_fit_score

        r = compute_fit_score(
            job_skills={"react", "node.js"},
            profile_skills={"react", "node.js"},
            job_exp_str="3-5 years",
            profile_exp="4 years 0 months",
        )
        assert r["overall_score"] == 100
        assert "bonuses" not in r  # no enrichment supplied

    def test_bonuses_appear_when_enrichment_supplied(self):
        from naukri_server.scoring import compute_fit_score

        r = compute_fit_score(
            job_skills={"react"},
            profile_skills={"react"},
            job_exp_str="3-5 years",
            profile_exp="4 years",
            job_location="Bangalore",
            profile_location="Bangalore",
            job_work_mode="wfh",
            job_salary="20-30 Lacs",
            profile_expected_ctc=25,
            is_agent_eligible=True,
        )
        assert r["bonuses"]["total"] == 20
        assert r["overall_score"] == 100

    @pytest.mark.parametrize(
        "job_loc,prof_loc,expected",
        [
            ("Bangalore", "Bangalore", 5),
            ("Remote", "Pune", 5),
            ("Mumbai", "Pune", 0),
            (None, "Pune", 0),
        ],
    )
    def test_score_location(self, job_loc, prof_loc, expected):
        from naukri_server.scoring import _score_location

        assert _score_location(job_loc, prof_loc) == expected

    def test_helpers_still_injectable_into_fit_score_compute(self):
        """The exact pattern tools/smart_apply.py uses."""
        from naukri_server.domain.fit_score import FitScore
        from naukri_server.scoring import (
            _score_location,
            _score_salary,
            _score_work_mode,
            parse_skills,
        )

        fit = FitScore.compute(
            job_skills=parse_skills("React, AWS"),
            profile_skills=parse_skills("reactjs, aws"),
            job_exp_str="3-5 years",
            profile_exp="4 years",
            job_location="Bangalore",
            profile_location="Bangalore",
            job_work_mode="wfh",
            job_salary="20-30 Lacs",
            profile_expected_ctc=25,
            score_location_fn=_score_location,
            score_work_mode_fn=_score_work_mode,
            score_salary_fn=_score_salary,
        )
        assert fit.bonuses.location == 5
        assert fit.bonuses.work_mode == 5
        assert fit.bonuses.salary == 5
        assert fit.to_dict()["overall_score"] == 100


# ---------------------------------------------------------------------------
# 5. Import order tolerance — the shims must not create a cycle
# ---------------------------------------------------------------------------

class TestNoImportCycle:
    @pytest.mark.parametrize(
        "module",
        [
            "naukri_server.scoring",
            "naukri_server.domain.salary",
            "naukri_server.domain.fit_score",
            "naukri_server.domain.skill_taxonomy",
        ],
    )
    def test_module_reimports_cleanly(self, module):
        importlib.reload(importlib.import_module(module))
