"""skill_gap told him to learn "development", "software" and "stack".

Naukri's `tagsAndSkills` is an SEO tag list, not a skill list. One live job
carried, verbatim:

    "Artificial Intelligence,Fullstack Development,React.js,Langchain,
     Fast API,Full Stack,Node.js,SQL"

`parse_skills` NORMALISES (React.js -> react, Fast API -> fastapi) but does not
FILTER, so every tag came through as a "skill". Across his 53 live
recommendations that produced 213 distinct "skills", and the gap report advised
him to go and learn "development", "full stack" and "stack".

That is worse than the tool being absent: it is advice he might act on.
`known_skills` keeps only what the 88-skill taxonomy recognises -- 45 of the 213
on the same sample, with the real top skills (node.js, python, react, aws,
typescript, docker) untouched.

Precision over recall ON PURPOSE, and it has a real cost: genuine skills outside
the 88 are dropped too. On that sample the casualties included ".net core",
"ajax" and "agentic ai". That is a taxonomy coverage gap, fixed by extending the
taxonomy rather than by loosening the filter -- and the tool now RETURNS the
most frequent dropped tags so the extension candidates are visible instead of
silently discarded.
"""

import pytest

from naukri_server.scoring import known_skills, parse_skills, taxonomy_tokens

# Verbatim from a live recommendations item.
LIVE_TAGS = ["Artificial Intelligence", "Fullstack Development", "React.js",
             "Langchain", "Fast API", "Full Stack", "Node.js", "SQL"]

# The fragments that reached his gap report.
NOT_SKILLS = ["development", "software", "stack", "full stack",
              "fullstack development", "application", "architecture"]


class TestTheFilter:
    def test_the_fragments_he_was_told_to_learn_are_gone(self):
        """THE regression."""
        out = known_skills(NOT_SKILLS)
        assert out == set(), "still reporting non-skills: %s" % sorted(out)

    def test_real_skills_in_the_same_payload_survive(self):
        out = known_skills(LIVE_TAGS)
        assert "react" in out
        assert "node.js" in out
        assert "sql" in out
        assert "fastapi" in out, "aliases must still normalise (Fast API -> fastapi)"
        assert "artificial intelligence" in out

    def test_the_filter_actually_removes_something_from_the_live_payload(self):
        """CONTROL: if filtered == unfiltered the filter is doing nothing."""
        before, after = parse_skills(LIVE_TAGS), known_skills(LIVE_TAGS)
        assert after < before, "the filter removed nothing"
        assert "full stack" in before and "full stack" not in after

    def test_aliases_are_normalised_before_membership_is_checked(self):
        """Order matters: "React.js" is not in the taxonomy, "react" is."""
        assert known_skills(["React.js"]) == {"react"}
        assert known_skills(["JS"]) == {"javascript"}

    def test_empty_and_junk_input_are_safe(self):
        assert known_skills([]) == set()
        assert known_skills("") == set()
        assert known_skills(None) == set()


class TestTheTokenSet:
    def test_the_taxonomy_exposes_its_tokens(self):
        toks = taxonomy_tokens()
        assert toks, "membership cannot be checked, so nothing can be filtered"
        for real in ("react", "node.js", "docker", "kubernetes", "sql"):
            assert real in toks, real
        for junk in ("stack", "development", "software"):
            assert junk not in toks, junk

    def test_an_empty_token_set_disables_filtering_rather_than_erasing_everything(
            self, monkeypatch):
        """FAIL-SAFE DIRECTION. `_lookup` is a private jobcore attribute; if it
        ever disappears, dropping every skill would silently empty the report.
        Returning everything is the honest degradation -- and the tool reports
        `taxonomy_filtered: false` so it is visible rather than assumed."""
        import naukri_server.scoring as sc

        monkeypatch.setattr(sc, "taxonomy_tokens", lambda: frozenset())
        assert sc.known_skills(LIVE_TAGS) == sc.parse_skills(LIVE_TAGS)


class TestTheReportMakesTheDropVisible:
    @pytest.mark.asyncio
    async def test_gap_report_names_what_it_dropped(self):
        from unittest.mock import AsyncMock, patch

        from naukri_server.services.insights_service import skill_gap_analysis

        jobs = {"status": "success", "jobs": [
            {"title": "T1", "company": "C1",
             "tags": ["React.js", "Full Stack", "Development", "Kubernetes"]},
            {"title": "T2", "company": "C2",
             "tags": ["Node.js", "Stack", "Development"]},
        ]}
        profile = {"status": "success", "key_skills": ["react"],
                   "skills_with_experience": []}

        with patch("naukri_server.tools.search.naukri_get_recommendations",
                   new_callable=AsyncMock, return_value=jobs), \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock, return_value=profile), \
             patch("naukri_server.tools.assessments._list_assessments",
                   new_callable=AsyncMock, return_value={"status": "success",
                                                         "assessments": []}):
            r = await skill_gap_analysis(use_recommendations=True, sample_size=5)

        assert r["status"] == "success", r
        gaps = {g["skill"] for g in r["skill_gaps"]}
        assert "kubernetes" in gaps and "node.js" in gaps
        for junk in ("development", "stack", "full stack"):
            assert junk not in gaps, "%s is not a skill" % junk

        assert r["taxonomy_filtered"] is True
        assert r["unrecognised_tags_dropped"] >= 3
        dropped = {d["tag"] for d in r["unrecognised_tags_top"]}
        assert "development" in dropped, "the drop must be VISIBLE, not silent"
        # "development" appeared in both jobs; the list is frequency-ordered so
        # the best taxonomy-extension candidates come first.
        assert r["unrecognised_tags_top"][0]["seen_in_jobs"] >= 1
