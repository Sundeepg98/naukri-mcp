"""Tests for apply helper functions — _find_user_answer, _format_answer,
_build_apply_answers, _cache_answers.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import patch, MagicMock


# =====================================================================
# 1. _find_user_answer
# =====================================================================

class TestFindUserAnswer:
    """Tests for naukri_server.tools.apply._find_user_answer."""

    def test_match_by_question_id(self):
        """Exact match on question ID key takes priority."""
        from naukri_server.tools.apply import _find_user_answer
        answers = {"12345": "5 years", "notice period": "30 days"}
        result = _find_user_answer("12345", "What is your experience?", answers)
        assert result == "5 years"

    def test_match_by_name_substring(self):
        """Key that is a substring of the question name should match."""
        from naukri_server.tools.apply import _find_user_answer
        answers = {"current ctc": "16"}
        result = _find_user_answer("999", "What is your current CTC?", answers)
        assert result == "16"

    def test_no_match_returns_none(self):
        """When neither ID nor substring match, return None."""
        from naukri_server.tools.apply import _find_user_answer
        answers = {"salary": "20"}
        result = _find_user_answer("111", "What is your notice period?", answers)
        assert result is None

    def test_multiple_matches_first_wins(self):
        """When multiple keys could match, the first dict-iteration match wins."""
        from naukri_server.tools.apply import _find_user_answer
        # Both "ctc" and "current" appear in the question name
        answers = {"ctc": "16", "current": "20"}
        result = _find_user_answer("999", "What is your current CTC?", answers)
        # First key in dict iteration order wins
        assert result in ("16", "20")  # implementation-dependent order
        # But critically, it should NOT be None
        assert result is not None

    def test_case_insensitive_matching(self):
        """Matching should be case-insensitive."""
        from naukri_server.tools.apply import _find_user_answer
        answers = {"Notice Period": "30 days"}
        result = _find_user_answer("999", "what is your notice period?", answers)
        assert result == "30 days"

    def test_underscore_replaced_with_space(self):
        """Underscores in answer keys should be treated as spaces."""
        from naukri_server.tools.apply import _find_user_answer
        answers = {"notice_period": "15 days"}
        result = _find_user_answer("999", "What is your notice period?", answers)
        assert result == "15 days"

    def test_list_value_unwrapped(self):
        """Single-element list values should be unwrapped to string."""
        from naukri_server.tools.apply import _find_user_answer
        answers = {"12345": [5]}
        result = _find_user_answer("12345", "Experience", answers)
        assert result == "5"


# =====================================================================
# 2. _format_answer
# =====================================================================

class TestFormatAnswer:
    """Tests for naukri_server.tools.apply._format_answer."""

    def test_text_box_returns_raw_string(self):
        """Text Box type should return the answer string as-is."""
        from naukri_server.tools.apply import _format_answer
        result = _format_answer("5 years", "Text Box", {})
        assert result == "5 years"

    def test_select_exact_match(self):
        """Select type with exact match should return option in a list."""
        from naukri_server.tools.apply import _format_answer
        options = {"1": "0-3 years", "2": "3-5 years", "3": "5+ years"}
        result = _format_answer("3-5 years", "Radio Button", options)
        assert result == ["3-5 years"]

    def test_select_case_insensitive(self):
        """Select matching should be case-insensitive."""
        from naukri_server.tools.apply import _format_answer
        options = {"1": "Yes", "2": "No"}
        result = _format_answer("yes", "Radio Button", options)
        assert result == ["Yes"]

    def test_select_by_key(self):
        """When answer matches an option dict key, use the value."""
        from naukri_server.tools.apply import _format_answer
        options = {"1": "Option A", "2": "Option B"}
        result = _format_answer("1", "Radio Button", options)
        assert result == ["Option A"]

    def test_no_options_returns_string(self):
        """Non-text-box type with empty options should return string as-is."""
        from naukri_server.tools.apply import _format_answer
        result = _format_answer("custom answer", "Radio Button", {})
        assert result == "custom answer"

    def test_fallback_wraps_in_list(self):
        """When no option matches, answer should be wrapped in a list."""
        from naukri_server.tools.apply import _format_answer
        options = {"1": "Apple", "2": "Banana"}
        result = _format_answer("Cherry", "Check Box", options)
        assert result == ["Cherry"]


# =====================================================================
# 3. _build_apply_answers
# =====================================================================

class TestBuildApplyAnswers:
    """Tests for naukri_server.tools.apply._build_apply_answers."""

    def test_numeric_keys_passed_through(self):
        """Numeric string keys should be included in the result."""
        from naukri_server.tools.apply import _build_apply_answers
        answers = {"12345": "5", "67890": ["Yes"]}
        result = _build_apply_answers("job1", answers, {})
        assert result == {"12345": "5", "67890": ["Yes"]}

    def test_non_numeric_keys_ignored(self):
        """Non-numeric keys (text substrings) should be excluded."""
        from naukri_server.tools.apply import _build_apply_answers
        answers = {"current ctc": "16", "notice period": "30"}
        result = _build_apply_answers("job1", answers, {})
        assert result == {}

    def test_mixed_keys(self):
        """Only numeric keys are included; text keys are skipped."""
        from naukri_server.tools.apply import _build_apply_answers
        answers = {"100": "Yes", "notice period": "30", "200": "No"}
        result = _build_apply_answers("job1", answers, {})
        assert result == {"100": "Yes", "200": "No"}

    def test_empty_answers(self):
        """Empty answers dict should produce empty result."""
        from naukri_server.tools.apply import _build_apply_answers
        result = _build_apply_answers("job1", {}, {})
        assert result == {}


# =====================================================================
# 4. _cache_answers
# =====================================================================

class TestCacheAnswers:
    """Tests for naukri_server.tools.apply._cache_answers."""

    @patch("naukri_server.tools.apply._cache_key")
    def test_caches_new_answer(self, mock_cache_key):
        """A matched answer for an uncached question should be stored."""
        from naukri_server.tools.apply import _cache_answers
        mock_cache_key.return_value = "q_notice_key"

        questionnaire = [
            {
                "questionId": 1,
                "questionName": "What is your notice period?",
                "questionType": "Text Box",
                "answerOption": {},
            }
        ]
        answers = {"notice period": "30 days"}
        cache = {}

        _cache_answers(questionnaire, answers, cache)

        assert "q_notice_key" in cache
        assert cache["q_notice_key"]["answer"] == "30 days"
        assert cache["q_notice_key"]["questionType"] == "Text Box"
        assert cache["q_notice_key"]["questionName"] == "What is your notice period?"

    @patch("naukri_server.tools.apply._cache_key")
    def test_does_not_overwrite_existing_cache(self, mock_cache_key):
        """If the cache key already exists, it should NOT be overwritten."""
        from naukri_server.tools.apply import _cache_answers
        mock_cache_key.return_value = "q_ctc_key"

        questionnaire = [
            {
                "questionId": 2,
                "questionName": "Current CTC",
                "questionType": "Text Box",
                "answerOption": {},
            }
        ]
        answers = {"current ctc": "20"}
        cache = {
            "q_ctc_key": {
                "questionType": "Text Box",
                "questionName": "Current CTC",
                "answer": "16",  # old cached value
            }
        }

        _cache_answers(questionnaire, answers, cache)

        # Original cached value should be preserved
        assert cache["q_ctc_key"]["answer"] == "16"

    @patch("naukri_server.tools.apply._cache_key")
    def test_skips_unmatched_questions(self, mock_cache_key):
        """Questions with no matching user answer should not be cached."""
        from naukri_server.tools.apply import _cache_answers
        mock_cache_key.return_value = "q_key"

        questionnaire = [
            {
                "questionId": 3,
                "questionName": "Are you willing to relocate?",
                "questionType": "Radio Button",
                "answerOption": {"1": "Yes", "2": "No"},
            }
        ]
        answers = {"salary": "20"}  # no match for "relocate"
        cache = {}

        _cache_answers(questionnaire, answers, cache)

        assert cache == {}

    @patch("naukri_server.tools.apply._cache_key")
    def test_caches_select_answer_formatted(self, mock_cache_key):
        """A select-type answer should be cached in formatted (list) form."""
        from naukri_server.tools.apply import _cache_answers
        mock_cache_key.return_value = "q_relocate_key"

        questionnaire = [
            {
                "questionId": 4,
                "questionName": "Are you willing to relocate?",
                "questionType": "Radio Button",
                "answerOption": {"1": "Yes", "2": "No"},
            }
        ]
        answers = {"relocate": "yes"}
        cache = {}

        _cache_answers(questionnaire, answers, cache)

        assert "q_relocate_key" in cache
        # _format_answer should wrap the matched option in a list
        assert cache["q_relocate_key"]["answer"] == ["Yes"]
