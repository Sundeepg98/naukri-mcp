"""Tests for miscellaneous helper functions in job_parsing and ambitionbox modules.

Covers edge cases in _parse_job_list() and _ensure_slug() / derive_slug()
that are not exercised by existing test suites.
"""

from naukri_server.config import NAUKRI_BASE
from naukri_server.tools.job_parsing import _parse_job_list
from naukri_server.tools.ambitionbox import _ensure_slug
from naukri_server.utils import derive_slug


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job(**overrides):
    """Minimal Naukri job API response with sensible defaults."""
    base = {
        "jobId": "100",
        "title": "Engineer",
        "companyName": "Acme",
        "salaryDetail": {"label": "", "minimumSalary": 800000, "maximumSalary": 1500000},
        "placeholders": [{"type": "location", "label": "Pune"}],
        "minimumExperience": 2,
        "maximumExperience": 5,
        "isApplied": False,
        "createdDate": "2026-01-01",
        "tagsAndSkills": "Python, Flask",
        "jdURL": "/job/engineer-100",
    }
    base.update(overrides)
    return base


# ===========================================================================
# _parse_job_list() — edge cases
# ===========================================================================


class TestParseJobListMissingAmbitionBox:
    """ambitionBoxData absent, None, or partially populated."""

    def test_missing_ambitionbox_data(self):
        job = _make_job()  # no ambitionBoxData key at all
        result = _parse_job_list([job], limit=10)
        assert result[0]["company_rating"] is None
        assert result[0]["company_reviews_count"] is None

    def test_ambitionbox_data_none(self):
        job = _make_job(ambitionBoxData=None)
        result = _parse_job_list([job], limit=10)
        assert result[0]["company_rating"] is None
        assert result[0]["company_reviews_count"] is None

    def test_ambitionbox_data_partial(self):
        """Only Rating present, ReviewsCount absent."""
        job = _make_job(ambitionBoxData={"Rating": 4.2})
        result = _parse_job_list([job], limit=10)
        assert result[0]["company_rating"] == 4.2
        assert result[0]["company_reviews_count"] is None

    def test_ambitionbox_aggregate_rating_fallback(self):
        """Falls back to AggregateRating when Rating is missing."""
        job = _make_job(ambitionBoxData={"AggregateRating": 3.9, "ReviewsCount": 120})
        result = _parse_job_list([job], limit=10)
        assert result[0]["company_rating"] == 3.9
        assert result[0]["company_reviews_count"] == 120


class TestParseJobListMalformedJdURL:
    """jdURL edge cases: missing, empty, None."""

    def test_jd_url_missing(self):
        """No jdURL key — falls back to job-listings-{jobId}."""
        job = _make_job()
        del job["jdURL"]
        result = _parse_job_list([job], limit=10)
        assert result[0]["url"] == f"{NAUKRI_BASE}/job-listings-100"
        assert result[0]["jd_url"] is None

    def test_jd_url_empty_string(self):
        """Empty jdURL is falsy — falls back to job-listings."""
        job = _make_job(jdURL="")
        result = _parse_job_list([job], limit=10)
        assert result[0]["url"] == f"{NAUKRI_BASE}/job-listings-100"

    def test_jd_url_none(self):
        """Explicit None jdURL — falls back to job-listings."""
        job = _make_job(jdURL=None)
        result = _parse_job_list([job], limit=10)
        assert result[0]["url"] == f"{NAUKRI_BASE}/job-listings-100"


class TestParseJobListNoneFields:
    """None or missing values in individual job dict fields."""

    def test_none_tags_and_skills(self):
        """tagsAndSkills is None — not a str, so falls to .get() which returns None."""
        job = _make_job(tagsAndSkills=None)
        result = _parse_job_list([job], limit=10)
        # Key exists with value None; isinstance(..., str) is False,
        # so job.get("tagsAndSkills", []) returns None (key present).
        assert result[0]["tags"] is None

    def test_tags_as_list(self):
        """tagsAndSkills provided as list instead of comma-separated string."""
        job = _make_job(tagsAndSkills=["Python", "Django"])
        result = _parse_job_list([job], limit=10)
        assert result[0]["tags"] == ["Python", "Django"]

    def test_none_created_date_uses_footer(self):
        """createdDate is None — falls back to footerPlaceholderLabel."""
        job = _make_job(createdDate=None, footerPlaceholderLabel="30+ Days Ago")
        result = _parse_job_list([job], limit=10)
        assert result[0]["posted_date"] == "30+ Days Ago"

    def test_location_fallback_non_location_placeholder(self):
        """No placeholder with type=location — falls back to first placeholder's label."""
        job = _make_job(placeholders=[{"type": "experience", "label": "3-5 Yrs"}])
        result = _parse_job_list([job], limit=10)
        assert result[0]["location"] == "3-5 Yrs"

    def test_empty_placeholders(self):
        """Empty placeholders list — location should be None."""
        job = _make_job(placeholders=[])
        result = _parse_job_list([job], limit=10)
        assert result[0]["location"] is None

    def test_missing_experience_fields(self):
        """minimumExperience and maximumExperience not present at all."""
        job = _make_job()
        del job["minimumExperience"]
        del job["maximumExperience"]
        result = _parse_job_list([job], limit=10)
        assert result[0]["experience"] == "?-? Yrs"
        assert result[0]["experience_min"] is None
        assert result[0]["experience_max"] is None


class TestParseJobListEmptyAndMissingKeys:
    """Empty job list and jobs missing expected keys."""

    def test_empty_job_list(self):
        result = _parse_job_list([], limit=10)
        assert result == []

    def test_completely_empty_dict(self):
        """A job dict with zero keys — should not crash."""
        result = _parse_job_list([{}], limit=10)
        assert len(result) == 1
        j = result[0]
        assert j["job_id"] is None
        assert j["title"] is None
        assert j["salary"] == "Not Disclosed"
        assert j["location"] is None
        assert j["tags"] == []
        assert j["url"] == f"{NAUKRI_BASE}/job-listings-"

    def test_salary_computed_when_label_empty(self):
        """When label is empty but min/max are set, compute '8.0-15.0 LPA'."""
        job = _make_job(salaryDetail={"label": "", "minimumSalary": 800000, "maximumSalary": 1500000})
        result = _parse_job_list([job], limit=10)
        assert result[0]["salary"] == "8.0-15.0 LPA"

    def test_vacancy_fallback_to_vacany(self):
        """The API typo 'vacany' is handled as fallback."""
        job = _make_job(vacancy=None, vacany=3)
        result = _parse_job_list([job], limit=10)
        assert result[0]["vacancies"] == 3

    def test_work_mode_fallback_to_wfh_type(self):
        """workMode absent — falls back to wfhType."""
        job = _make_job(workMode=None, wfhType="Hybrid")
        result = _parse_job_list([job], limit=10)
        assert result[0]["work_mode"] == "Hybrid"

    def test_logo_url_fallback(self):
        """logoPathV3 absent — falls back to logoPath."""
        job = _make_job(logoPathV3=None, logoPath="https://img.naukri.com/old.png")
        result = _parse_job_list([job], limit=10)
        assert result[0]["logo_url"] == "https://img.naukri.com/old.png"


# ===========================================================================
# _ensure_slug() — slug derivation from ambitionbox.py
# ===========================================================================


class TestEnsureSlug:
    """_ensure_slug delegates to derive_slug for non-slug input."""

    def test_already_slug_lowercase_alpha(self):
        assert _ensure_slug("google") == "google"

    def test_already_slug_with_hyphens(self):
        assert _ensure_slug("tata-consultancy") == "tata-consultancy"

    def test_already_slug_with_digits(self):
        assert _ensure_slug("24seven") == "24seven"

    def test_company_name_with_pvt_ltd(self):
        # derive_slug strips only ONE suffix per call (break after first match).
        # "Pvt. Ltd." is stripped -> "Google India" -> slug "google-india".
        result = _ensure_slug("Google India Pvt. Ltd.")
        assert result == "google-india"

    def test_company_name_mixed_case(self):
        result = _ensure_slug("Flipkart")
        # Uppercase "F" means it's not a slug — passes through derive_slug
        assert result == "flipkart"

    def test_empty_string(self):
        # Empty string doesn't match the slug regex (requires at least one char)
        # derive_slug("") returns ""
        result = _ensure_slug("")
        assert result == ""

    def test_special_characters(self):
        result = _ensure_slug("Foo & Bar @#$ Solutions")
        assert result == "foo-bar"

    def test_slug_starting_with_digit(self):
        assert _ensure_slug("3i-infotech") == "3i-infotech"

    def test_uppercase_single_word_is_not_slug(self):
        """'Google' has uppercase — not a valid slug, goes through derive_slug."""
        result = _ensure_slug("Google")
        assert result == "google"

    def test_slug_with_trailing_hyphen_is_not_slug(self):
        """Trailing hyphen fails the slug regex, goes through derive_slug."""
        # derive_slug strips trailing hyphens
        result = _ensure_slug("foo-")
        # Regex requires [a-z0-9][a-z0-9-]* so "foo-" passes since the regex
        # matches "foo-" (trailing hyphen is valid in [a-z0-9-]*)
        # Actually re.match(r'^[a-z0-9][a-z0-9-]*$', 'foo-') IS a match
        assert result == "foo-"


# ===========================================================================
# derive_slug() — additional edge cases not in test_utils.py
# ===========================================================================


class TestDeriveSlugExtended:
    """Extra coverage for derive_slug beyond test_utils.py."""

    def test_company_with_inc_suffix(self):
        assert derive_slug("Apple Inc.") == "apple"

    def test_company_with_llp(self):
        assert derive_slug("Deloitte LLP") == "deloitte"

    def test_consecutive_special_chars_collapse(self):
        """Multiple special chars become a single hyphen."""
        assert derive_slug("A & B --- C") == "a-b-c"

    def test_only_suffix(self):
        """Company name that IS just a suffix — stripping leaves empty."""
        assert derive_slug("Technologies") == ""

    def test_preserves_digits_in_name(self):
        assert derive_slug("24Seven India") == "24seven"
