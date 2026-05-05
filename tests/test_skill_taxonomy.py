"""Unit tests for naukri_server.domain.skill_taxonomy — SkillTaxonomy normalization.

All tests are PURE: no network, no browser, no file I/O.
"""

import pytest

from naukri_server.domain.skill_taxonomy import (
    SKILL_ALIASES,
    SkillTaxonomy,
    DEFAULT_TAXONOMY,
)


# ---------------------------------------------------------------------------
# 1. SKILL_ALIASES module-level data
# ---------------------------------------------------------------------------

class TestSkillAliasesData:
    def test_canonical_count(self):
        """Documented as 88 canonical skills (the actual count may grow over time;
        we assert there are at least 80 to catch regressions)."""
        # Per CLAUDE.md scoring section: 88 canonical skills with 150 aliases
        assert len(SKILL_ALIASES) >= 80

    def test_alias_count(self):
        """At least 100 aliases across all canonical skills."""
        total = sum(len(v) for v in SKILL_ALIASES.values())
        assert total >= 100

    def test_all_canonical_keys_lowercase(self):
        """Canonical names should all be lowercase for consistent normalization."""
        for canonical in SKILL_ALIASES:
            assert canonical == canonical.lower(), (
                f"Canonical '{canonical}' is not lowercase"
            )

    def test_all_aliases_lowercase(self):
        """Aliases should all be lowercase for consistent normalization."""
        for canonical, aliases in SKILL_ALIASES.items():
            for alias in aliases:
                assert alias == alias.lower(), (
                    f"Alias '{alias}' under '{canonical}' is not lowercase"
                )

    def test_no_alias_collides_with_canonical_of_different_skill(self):
        """An alias for skill X must not be a canonical name for skill Y."""
        canonicals = set(SKILL_ALIASES.keys())
        for canonical, aliases in SKILL_ALIASES.items():
            for alias in aliases:
                if alias in canonicals and alias != canonical:
                    pytest.fail(
                        f"Alias '{alias}' for canonical '{canonical}' is also "
                        f"a canonical name for a different skill"
                    )


# ---------------------------------------------------------------------------
# 2. SkillTaxonomy.normalize — alias resolution
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_canonical_returns_self(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        assert t.normalize("python") == "python"
        assert t.normalize("javascript") == "javascript"
        assert t.normalize("react") == "react"

    def test_known_alias_resolves_to_canonical(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        assert t.normalize("js") == "javascript"
        assert t.normalize("ts") == "typescript"
        assert t.normalize("py") == "python"
        assert t.normalize("k8s") == "kubernetes"
        assert t.normalize("aws") == "amazon web services"
        assert t.normalize("nodejs") == "node.js"
        assert t.normalize("reactjs") == "react"

    def test_case_insensitive(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        assert t.normalize("JS") == "javascript"
        assert t.normalize("Python") == "python"
        assert t.normalize("REACT") == "react"
        assert t.normalize("aWs") == "amazon web services"

    def test_strips_whitespace(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        assert t.normalize("  python  ") == "python"
        assert t.normalize("\tjs\n") == "javascript"

    def test_unknown_skill_returns_lowercased_input(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        assert t.normalize("MyMadeUpSkill") == "mymadeupskill"
        assert t.normalize("  foobar  ") == "foobar"

    def test_aliases_with_spaces_dots_dashes(self):
        """Aliases containing spaces, dots, or dashes resolve correctly."""
        t = SkillTaxonomy(SKILL_ALIASES)
        assert t.normalize("react.js") == "react"
        assert t.normalize("vue.js") == "vue"
        assert t.normalize("angular.js") == "angular"
        assert t.normalize("ci cd") == "ci/cd"
        assert t.normalize("micro-services") == "microservices"


# ---------------------------------------------------------------------------
# 3. SkillTaxonomy.parse_set — handles set/str/list/tuple input
# ---------------------------------------------------------------------------

class TestParseSet:
    def test_parse_set_input(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        result = t.parse_set({"js", "py", "react"})
        assert result == frozenset({"javascript", "python", "react"})
        assert isinstance(result, frozenset)

    def test_parse_csv_string(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        result = t.parse_set("js, py, react")
        assert result == frozenset({"javascript", "python", "react"})

    def test_parse_list(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        result = t.parse_set(["js", "k8s", "aws"])
        assert result == frozenset({"javascript", "kubernetes", "amazon web services"})

    def test_parse_tuple(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        result = t.parse_set(("js", "py"))
        assert result == frozenset({"javascript", "python"})

    def test_parse_empty_returns_empty_frozenset(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        assert t.parse_set(set()) == frozenset()
        assert t.parse_set("") == frozenset()
        assert t.parse_set([]) == frozenset()
        assert t.parse_set(()) == frozenset()

    def test_parse_unsupported_type_returns_empty(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        assert t.parse_set(None) == frozenset()
        assert t.parse_set(42) == frozenset()
        assert t.parse_set({"key": "value"}) == frozenset()  # dict not supported

    def test_parse_filters_empty_strings(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        # Empty strings in CSV should be dropped
        result = t.parse_set("js, , py,  ")
        assert result == frozenset({"javascript", "python"})

    def test_parse_list_skips_non_string_elements(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        # Non-string entries in a list are filtered out
        result = t.parse_set(["js", 42, None, "py"])
        assert result == frozenset({"javascript", "python"})


# ---------------------------------------------------------------------------
# 4. SkillTaxonomy.match — set difference operation
# ---------------------------------------------------------------------------

class TestMatch:
    def test_match_returns_intersection_and_difference(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        job = frozenset({"python", "javascript", "react", "kubernetes"})
        profile = frozenset({"python", "javascript", "go", "docker"})
        matched, missing = t.match(job, profile)
        assert matched == frozenset({"python", "javascript"})
        assert missing == frozenset({"react", "kubernetes"})

    def test_match_full_overlap(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        skills = frozenset({"python", "javascript"})
        matched, missing = t.match(skills, skills)
        assert matched == skills
        assert missing == frozenset()

    def test_match_no_overlap(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        job = frozenset({"python"})
        profile = frozenset({"java"})
        matched, missing = t.match(job, profile)
        assert matched == frozenset()
        assert missing == frozenset({"python"})

    def test_match_empty_inputs(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        matched, missing = t.match(frozenset(), frozenset())
        assert matched == frozenset()
        assert missing == frozenset()


# ---------------------------------------------------------------------------
# 5. Properties + DEFAULT_TAXONOMY singleton
# ---------------------------------------------------------------------------

class TestProperties:
    def test_canonical_count_property(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        assert t.canonical_count == len(SKILL_ALIASES)

    def test_alias_count_property(self):
        t = SkillTaxonomy(SKILL_ALIASES)
        expected = sum(len(v) for v in SKILL_ALIASES.values())
        assert t.alias_count == expected

    def test_default_taxonomy_singleton_uses_skill_aliases(self):
        """DEFAULT_TAXONOMY is the production singleton."""
        assert DEFAULT_TAXONOMY.canonical_count == len(SKILL_ALIASES)
        # And it normalizes correctly
        assert DEFAULT_TAXONOMY.normalize("js") == "javascript"

    def test_custom_aliases(self):
        """Can build a SkillTaxonomy with a custom alias map."""
        custom = {
            "myskill": {"ms", "myskl"},
            "another": {"a", "an"},
        }
        t = SkillTaxonomy(custom)
        assert t.canonical_count == 2
        assert t.alias_count == 4
        assert t.normalize("ms") == "myskill"
        assert t.normalize("myskl") == "myskill"
        assert t.normalize("an") == "another"
        # Unknown alias still falls through
        assert t.normalize("python") == "python"  # not in custom map

    def test_round_trip_normalize_then_parse_set(self):
        """parse_set normalizes everything; result members are all canonical (or unknown lowercased)."""
        t = SkillTaxonomy(SKILL_ALIASES)
        result = t.parse_set("JS, Python, K8S, foobar")
        assert "javascript" in result
        assert "python" in result
        assert "kubernetes" in result
        assert "foobar" in result  # unknown → lowercased input
