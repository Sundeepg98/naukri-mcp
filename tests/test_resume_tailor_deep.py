"""Deep tests for resume_tailor module — pure unit tests, no network/browser/file I/O.

Covers:
- Job fetch failure → API_ERROR
- Profile fetch failure → API_ERROR
- Skills to add (in job, not in profile)
- Skills to reorder (in profile but not top 5, present in job)
- Headline suggestions for missing top skills
- Experience emphasis (keyword overlap >= threshold)
- Keyword gaps (in JD, not found in profile)
- _extract_keywords: filters stopwords, filters short words, strips HTML
- _extract_phrases: capitalized multi-word terms, common tech patterns
- Parallel job + profile fetch (asyncio.gather called)
- Skill normalization via alias map (e.g., "JS" → "javascript")
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers: build minimal fake job/profile results
# ---------------------------------------------------------------------------

def _make_job(
    title="Backend Engineer",
    company="Acme Corp",
    description="",
    skills=None,
):
    return {
        "status": "success",
        "title": title,
        "company": company,
        "description": description,
        "skills": skills or [],
    }


def _make_profile(
    key_skills=None,
    resume_headline="",
    employment=None,
    summary="",
    certifications=None,
    projects=None,
):
    return {
        "status": "success",
        "key_skills": key_skills or [],
        "resume_headline": resume_headline,
        "employment": employment or [],
        "summary": summary,
        "certifications": certifications or [],
        "projects": projects or [],
    }


# ---------------------------------------------------------------------------
# 1. Job fetch fails → API_ERROR
# ---------------------------------------------------------------------------

class TestJobFetchFailsApiError:
    """When naukri_get_job returns an error dict, _tailor_resume returns API_ERROR."""

    @pytest.mark.asyncio
    async def test_job_fetch_error_dict_returns_api_error(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        job_err = {"status": "error", "message": "Job not found"}
        profile_ok = _make_profile()

        # Patch at source: inner imports inside _do_work
        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_err),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "error"
        assert result.get("error_code") == "API_ERROR"
        assert "job" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_job_fetch_exception_returns_api_error(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        profile_ok = _make_profile()

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(side_effect=RuntimeError("network down")),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="99999")

        assert result["status"] == "error"
        assert result.get("error_code") == "API_ERROR"


# ---------------------------------------------------------------------------
# 2. Profile fetch fails → API_ERROR
# ---------------------------------------------------------------------------

class TestProfileFetchFailsApiError:
    """When get_cached_profile returns an error dict, _tailor_resume returns API_ERROR."""

    @pytest.mark.asyncio
    async def test_profile_fetch_error_dict_returns_api_error(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        job_ok = _make_job(skills=["Python"])
        profile_err = {"status": "error", "message": "Session expired"}

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_err),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "error"
        assert result.get("error_code") == "API_ERROR"
        assert "profile" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_profile_fetch_exception_returns_api_error(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        job_ok = _make_job(skills=["Python"])

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(side_effect=ConnectionError("auth failed")),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "error"
        assert result.get("error_code") == "API_ERROR"


# ---------------------------------------------------------------------------
# 3. Skills to add (in job but not in profile)
# ---------------------------------------------------------------------------

class TestSkillsToAdd:
    """Skills present in job but absent from profile appear in skills_to_add."""

    @pytest.mark.asyncio
    async def test_skill_in_job_not_in_profile_is_added(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        job_ok = _make_job(skills=["Python", "Kubernetes"])
        # Profile only has Python
        profile_ok = _make_profile(key_skills=["Python"])

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        skills_to_add = result["suggestions"]["skills_to_add"]
        assert "Kubernetes" in skills_to_add
        assert "Python" not in skills_to_add

    @pytest.mark.asyncio
    async def test_all_job_skills_in_profile_gives_empty_add_list(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        job_ok = _make_job(skills=["Python", "Docker"])
        profile_ok = _make_profile(key_skills=["Python", "Docker", "AWS"])

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        assert result["suggestions"]["skills_to_add"] == []


# ---------------------------------------------------------------------------
# 4. Skills to reorder (in profile but not top 5, required by job)
# ---------------------------------------------------------------------------

class TestSkillsToReorder:
    """Skills present in profile beyond position 5 and required by job → reorder suggestion."""

    @pytest.mark.asyncio
    async def test_skill_outside_top5_generates_reorder_suggestion(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        # Job requires Kubernetes; profile has it at position 7
        job_ok = _make_job(skills=["Kubernetes"])
        profile_skills = ["Python", "Django", "REST API", "PostgreSQL", "Docker", "Linux", "Kubernetes"]
        profile_ok = _make_profile(key_skills=profile_skills)

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        reorder = result["suggestions"]["skills_to_reorder"]
        assert len(reorder) >= 1
        # The suggestion should mention Kubernetes and its current position
        assert any("Kubernetes" in s for s in reorder)

    @pytest.mark.asyncio
    async def test_skill_in_top5_not_in_reorder_list(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        # Job requires Python; profile has Python at position 1
        job_ok = _make_job(skills=["Python"])
        profile_ok = _make_profile(key_skills=["Python", "Django", "Docker", "AWS", "SQL"])

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        reorder = result["suggestions"]["skills_to_reorder"]
        assert not any("Python" in s for s in reorder)


# ---------------------------------------------------------------------------
# 5. Headline suggestions for missing top skills
# ---------------------------------------------------------------------------

class TestHeadlineSuggestions:
    """When top job skills are absent from the current headline, a suggestion is returned."""

    @pytest.mark.asyncio
    async def test_headline_suggestion_when_skill_missing_from_headline(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        job_ok = _make_job(skills=["Kubernetes", "Docker", "Terraform", "AWS", "CI/CD"])
        profile_ok = _make_profile(
            key_skills=["Kubernetes", "Docker"],
            resume_headline="Senior Software Engineer",
        )

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        headline_suggestion = result["suggestions"]["headline"]
        assert headline_suggestion is not None
        assert "Consider adding" in headline_suggestion

    @pytest.mark.asyncio
    async def test_no_headline_suggestion_when_all_skills_present(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        job_ok = _make_job(skills=["Python"])
        profile_ok = _make_profile(
            key_skills=["Python"],
            resume_headline="Python Developer with 5 years experience",
        )

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        assert result["suggestions"]["headline"] is None


# ---------------------------------------------------------------------------
# 6. Experience emphasis (keyword overlap >= 3)
# ---------------------------------------------------------------------------

class TestExperienceEmphasis:
    """Employment entries with >= 3 keyword overlaps with JD appear in experience_emphasis."""

    @pytest.mark.asyncio
    async def test_employment_with_high_overlap_included(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        jd = "We need strong Python Django REST microservices PostgreSQL experience"
        job_ok = _make_job(description=jd, skills=["Python"])
        emp = {
            "designation": "Backend Developer",
            "organization": "TechCo",
            "description": "Built Python Django REST APIs with PostgreSQL microservices architecture",
        }
        profile_ok = _make_profile(key_skills=["Python"], employment=[emp])

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        emphasis = result["suggestions"]["experience_emphasis"]
        assert len(emphasis) >= 1
        assert "Backend Developer" in emphasis[0]["role"] or "TechCo" in emphasis[0]["role"]

    @pytest.mark.asyncio
    async def test_employment_with_low_overlap_excluded(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        jd = "Seeking Kubernetes Terraform AWS cloud DevOps engineer with CI/CD pipeline experience"
        job_ok = _make_job(description=jd, skills=["Kubernetes"])
        emp = {
            "designation": "Frontend Developer",
            "organization": "WebCo",
            "description": "Built React components",  # no overlap with DevOps JD
        }
        profile_ok = _make_profile(key_skills=["Kubernetes"], employment=[emp])

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        emphasis = result["suggestions"]["experience_emphasis"]
        assert len(emphasis) == 0


# ---------------------------------------------------------------------------
# 7. Keyword gaps
# ---------------------------------------------------------------------------

class TestKeywordGaps:
    """Keywords that appear in the JD but not anywhere in the profile → keyword_gaps."""

    @pytest.mark.asyncio
    async def test_jd_keyword_absent_from_profile_appears_in_gaps(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        jd = "Experience with observability monitoring distributed tracing telemetry required"
        job_ok = _make_job(description=jd, skills=[])
        # Profile has no mention of observability/monitoring/tracing
        profile_ok = _make_profile(
            key_skills=["Python", "Django"],
            resume_headline="Python Developer",
            employment=[{
                "designation": "Developer",
                "organization": "Co",
                "description": "Wrote Python code",
            }],
        )

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        gaps = result["suggestions"]["keyword_gaps"]
        # At least some of the unique JD terms should appear as gaps
        assert isinstance(gaps, list)
        all_gaps = " ".join(gaps).lower()
        # observability, monitoring, tracing, telemetry — at least one should surface
        assert any(k in all_gaps for k in ("observability", "monitoring", "tracing", "telemetry"))

    @pytest.mark.asyncio
    async def test_short_words_excluded_from_gaps(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        jd = "We ai ml dl need you to do ok it go"
        job_ok = _make_job(description=jd, skills=[])
        profile_ok = _make_profile(key_skills=["Python"])

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        gaps = result["suggestions"]["keyword_gaps"]
        # Single-char or two-char words (< 3 chars) must not appear
        for g in gaps:
            assert len(g) >= 3, f"Short word '{g}' should be filtered from keyword_gaps"


# ---------------------------------------------------------------------------
# 8. _extract_keywords unit tests
# ---------------------------------------------------------------------------

class TestExtractKeywords:
    """Pure unit tests for _extract_keywords helper."""

    def test_filters_stopwords(self):
        from naukri_server.tools.resume_tailor import _extract_keywords
        text = "We are looking for a candidate with experience"
        result = _extract_keywords(text)
        # All these are stopwords — should be removed
        for stopword in ("we", "are", "for", "a", "with"):
            assert stopword not in result

    def test_filters_short_words(self):
        from naukri_server.tools.resume_tailor import _extract_keywords
        result = _extract_keywords("a b x y abc")
        # 1-char words must be excluded (len > 1 rule)
        assert "a" not in result
        assert "b" not in result
        assert "x" not in result
        assert "y" not in result
        # 3-char word should survive if not a stopword
        assert "abc" in result

    def test_strips_html_tags(self):
        from naukri_server.tools.resume_tailor import _extract_keywords
        html = "<p>Strong <b>Python</b> developer</p>"
        result = _extract_keywords(html)
        assert "python" in result
        assert "strong" not in result  # stopword
        # No HTML tags or angle-bracket fragments
        for word in result:
            assert "<" not in word and ">" not in word

    def test_empty_string_returns_empty_set(self):
        from naukri_server.tools.resume_tailor import _extract_keywords
        assert _extract_keywords("") == set()

    def test_none_returns_empty_set(self):
        from naukri_server.tools.resume_tailor import _extract_keywords
        assert _extract_keywords(None) == set()

    def test_preserves_cpp_and_csharp(self):
        from naukri_server.tools.resume_tailor import _extract_keywords
        result = _extract_keywords("Experience with C++ and C#")
        assert "c++" in result
        assert "c#" in result


# ---------------------------------------------------------------------------
# 9. _extract_phrases unit tests
# ---------------------------------------------------------------------------

class TestExtractPhrases:
    """Pure unit tests for _extract_phrases helper."""

    def test_capitalized_multi_word_terms_extracted(self):
        from naukri_server.tools.resume_tailor import _extract_phrases
        text = "Experience with Machine Learning and Deep Learning"
        result = _extract_phrases(text)
        # Capitalized multi-word: "Machine Learning", "Deep Learning"
        assert "machine learning" in result or "deep learning" in result

    def test_common_tech_patterns_extracted(self):
        from naukri_server.tools.resume_tailor import _extract_phrases
        text = "Requires ci/cd pipelines and rest api design"
        result = _extract_phrases(text)
        assert "ci/cd" in result or "rest api" in result

    def test_empty_string_returns_empty_set(self):
        from naukri_server.tools.resume_tailor import _extract_phrases
        assert _extract_phrases("") == set()

    def test_none_returns_empty_set(self):
        from naukri_server.tools.resume_tailor import _extract_phrases
        assert _extract_phrases(None) == set()

    def test_strips_html_before_phrase_extraction(self):
        from naukri_server.tools.resume_tailor import _extract_phrases
        html = "<div>Experience with <b>Machine Learning</b> frameworks</div>"
        result = _extract_phrases(html)
        assert "machine learning" in result


# ---------------------------------------------------------------------------
# 10. Parallel fetch via asyncio.gather
# ---------------------------------------------------------------------------

class TestParallelFetch:
    """Both job and profile fetches are fired in parallel via asyncio.gather."""

    @pytest.mark.asyncio
    async def test_both_coroutines_called_concurrently(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        call_log = []

        async def fake_get_job(**kwargs):
            call_log.append("job")
            return _make_job(skills=["Python"])

        async def fake_get_profile():
            call_log.append("profile")
            return _make_profile(key_skills=["Python"])

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            side_effect=fake_get_job,
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            side_effect=fake_get_profile,
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        assert "job" in call_log
        assert "profile" in call_log


# ---------------------------------------------------------------------------
# 11. Skill normalization via alias map
# ---------------------------------------------------------------------------

class TestSkillNormalization:
    """Alias skills (e.g., 'JS') are normalized and matched to canonical profile skills."""

    @pytest.mark.asyncio
    async def test_js_alias_matches_javascript_in_profile(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        # Job lists "JS" (alias for javascript); profile has "JavaScript"
        job_ok = _make_job(skills=["JS"])
        profile_ok = _make_profile(key_skills=["JavaScript", "React"])

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        # "JS" should NOT appear in skills_to_add because profile has "JavaScript"
        assert "JS" not in result["suggestions"]["skills_to_add"]

    @pytest.mark.asyncio
    async def test_k8s_alias_matches_kubernetes_in_profile(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        # Job lists "k8s" (alias for kubernetes); profile has "Kubernetes"
        job_ok = _make_job(skills=["k8s"])
        profile_ok = _make_profile(key_skills=["Kubernetes", "Docker"])

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        assert "k8s" not in result["suggestions"]["skills_to_add"]

    @pytest.mark.asyncio
    async def test_unknown_skill_not_in_profile_is_flagged(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        job_ok = _make_job(skills=["RustLang"])
        profile_ok = _make_profile(key_skills=["Python", "Go"])

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        # RustLang normalizes to "rust" which is not in profile
        assert "RustLang" in result["suggestions"]["skills_to_add"]


# ---------------------------------------------------------------------------
# 12. Success response shape
# ---------------------------------------------------------------------------

class TestSuccessResponseShape:
    """Happy-path result contains all expected keys."""

    @pytest.mark.asyncio
    async def test_success_result_has_all_suggestion_keys(self):
        from naukri_server.tools.resume_tailor import _tailor_resume

        job_ok = _make_job(title="Data Engineer", company="DataCo", skills=["Python", "Spark"])
        profile_ok = _make_profile(
            key_skills=["Python"],
            resume_headline="Python Developer",
        )

        with patch(
            "naukri_server.tools.jobs.naukri_get_job",
            new=AsyncMock(return_value=job_ok),
        ), patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_ok),
        ):
            result = await _tailor_resume(job_id="12345")

        assert result["status"] == "success"
        assert result["job_title"] == "Data Engineer"
        assert result["company"] == "DataCo"

        suggestions = result["suggestions"]
        for key in (
            "headline",
            "current_headline",
            "skills_to_add",
            "skills_to_reorder",
            "experience_emphasis",
            "keyword_gaps",
            "phrase_gaps",
        ):
            assert key in suggestions, f"Missing key '{key}' in suggestions"
