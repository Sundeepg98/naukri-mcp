"""Unit tests for naukri_server.block_state — the pure block-state classifier.

Fully offline: feeds synthetic interstitial / 403 / 406 / redirect / JSON bodies
to the pure ``classify_block_state`` function and asserts the verdict. No network,
no mocks of the request path.
"""

import pytest

from naukri_server.block_state import (
    BlockState,
    BlockAssessment,
    classify_block_state,
)


# ---------------------------------------------------------------------------
# Healthy paths
# ---------------------------------------------------------------------------


class TestHealthy:
    def test_clean_json_200_is_healthy(self):
        a = classify_block_state(
            status=200, content_type="application/json; charset=utf-8",
            body='{"jobDetails": [], "noOfJobs": 0}',
        )
        assert a.state is BlockState.HEALTHY
        assert a.is_block is False
        assert a.signal == ""

    def test_javascript_content_type_is_healthy(self):
        a = classify_block_state(
            status=200, content_type="application/javascript", body="var x = 1;",
        )
        assert a.state is BlockState.HEALTHY

    def test_201_created_json_is_healthy(self):
        a = classify_block_state(status=201, content_type="application/json", body="{}")
        assert a.state is BlockState.HEALTHY

    def test_json_body_mentioning_word_login_is_not_login_wall(self):
        """A real JSON payload that merely contains a field is still healthy."""
        a = classify_block_state(
            status=200, content_type="application/json",
            body='{"links": {"login": "/account"}, "data": [1,2,3]}',
        )
        # 'login' alone is not a login marker; the marker list is phrase-based.
        assert a.state is BlockState.HEALTHY


# ---------------------------------------------------------------------------
# CAPTCHA
# ---------------------------------------------------------------------------


class TestCaptcha:
    @pytest.mark.parametrize("marker", [
        "Please complete the reCAPTCHA",
        "<div class='g-recaptcha'></div>",
        "hCaptcha verification required",
        "Are you a robot? Solve the captcha",
        "I'm not a robot",
    ])
    def test_captcha_markers_in_html_200(self, marker):
        a = classify_block_state(status=200, content_type="text/html", body=marker)
        assert a.state is BlockState.CAPTCHA
        assert a.is_block
        assert a.signal.startswith("body-marker:")

    def test_akamai_sensor_cookie_marker(self):
        a = classify_block_state(
            status=200, content_type="text/html",
            body="<script>bazadebezolkohpepadr=\"_abck\"</script>",
        )
        assert a.state is BlockState.CAPTCHA

    def test_captcha_wins_over_softblock_when_both_present(self):
        """Priority: captcha is more specific than a generic bot-flag."""
        body = "Unusual activity detected. Please complete the reCAPTCHA."
        a = classify_block_state(status=200, content_type="text/html", body=body)
        assert a.state is BlockState.CAPTCHA


# ---------------------------------------------------------------------------
# Login wall
# ---------------------------------------------------------------------------


class TestLoginWall:
    def test_redirect_to_nlogin_url(self):
        a = classify_block_state(
            status=200, content_type="text/html", body="<html></html>",
            redirected=True, final_url="https://www.naukri.com/nlogin/login",
        )
        assert a.state is BlockState.LOGIN_WALL
        assert a.signal == "redirect:/nlogin"

    def test_redirect_to_checkpoint_url(self):
        a = classify_block_state(
            status=200, content_type="text/html", body="x",
            redirected=True, final_url="https://www.naukri.com/checkpoint?ref=1",
        )
        assert a.state is BlockState.LOGIN_WALL

    def test_login_marker_in_body(self):
        a = classify_block_state(
            status=200, content_type="text/html",
            body="<form id='loginForm'>Please log in to continue</form>",
        )
        assert a.state is BlockState.LOGIN_WALL

    def test_session_expired_marker(self):
        a = classify_block_state(
            status=403, content_type="text/html", body="Your session has expired",
        )
        assert a.state is BlockState.LOGIN_WALL

    def test_login_url_wins_even_on_200_json(self):
        """A redirect to login is decisive regardless of status/content-type."""
        a = classify_block_state(
            status=200, content_type="application/json", body="{}",
            redirected=True, final_url="https://naukri.com/login",
        )
        assert a.state is BlockState.LOGIN_WALL


# ---------------------------------------------------------------------------
# Rate limited
# ---------------------------------------------------------------------------


class TestRateLimited:
    def test_status_429_no_body(self):
        a = classify_block_state(status=429, content_type="application/json", body="")
        assert a.state is BlockState.RATE_LIMITED
        assert a.signal == "status:429"

    def test_too_many_requests_marker_on_200(self):
        a = classify_block_state(
            status=200, content_type="text/html", body="Too many requests, slow down",
        )
        assert a.state is BlockState.RATE_LIMITED

    def test_429_precedes_body_markers(self):
        a = classify_block_state(
            status=429, content_type="text/html", body="unusual activity",
        )
        assert a.state is BlockState.RATE_LIMITED


# ---------------------------------------------------------------------------
# Soft block (generic bot-flag / interstitial)
# ---------------------------------------------------------------------------


class TestSoftBlock:
    @pytest.mark.parametrize("marker", [
        "Unusual activity from your network",
        "Access Denied",
        "You have been blocked",
        "Reference #18.abcd",
        "Verify you are human",
        "Automated traffic detected",
    ])
    def test_soft_block_markers(self, marker):
        a = classify_block_state(status=200, content_type="text/html", body=marker)
        assert a.state is BlockState.SOFT_BLOCK
        assert a.is_block

    def test_403_without_marker_is_soft_block(self):
        a = classify_block_state(status=403, content_type="text/html", body="<html>nope</html>")
        assert a.state is BlockState.SOFT_BLOCK
        assert a.signal == "status:403"

    def test_406_without_marker_is_soft_block(self):
        a = classify_block_state(status=406, content_type="application/json", body="{}")
        assert a.state is BlockState.SOFT_BLOCK
        assert a.signal == "status:406"

    def test_200_html_where_json_expected_is_soft_block(self):
        """A 200 carrying HTML on a REST endpoint = silent interstitial."""
        a = classify_block_state(
            status=200, content_type="text/html; charset=utf-8",
            body="<!doctype html><html><body>loading...</body></html>",
        )
        assert a.state is BlockState.SOFT_BLOCK
        assert a.signal == "html-on-success"

    def test_200_doctype_without_content_type(self):
        a = classify_block_state(status=200, content_type="", body="<!DOCTYPE html><html></html>")
        assert a.state is BlockState.SOFT_BLOCK


# ---------------------------------------------------------------------------
# Defensive / edge cases — must never raise
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_none_body(self):
        a = classify_block_state(status=200, content_type="application/json", body=None)
        assert isinstance(a, BlockAssessment)
        assert a.state is BlockState.HEALTHY

    def test_bytes_body_captcha(self):
        a = classify_block_state(
            status=200, content_type="text/html", body=b"please solve the captcha",
        )
        assert a.state is BlockState.CAPTCHA

    def test_non_string_body_does_not_raise(self):
        a = classify_block_state(status=200, content_type="application/json", body={"a": 1})
        assert isinstance(a, BlockAssessment)

    def test_huge_body_is_capped_and_classifies(self):
        body = ("x" * 200_000) + " unusual activity"  # marker beyond the cap
        a = classify_block_state(status=200, content_type="text/html", body=body)
        # Marker is past the 64KiB cap, but html-on-success still fires.
        assert a.is_block

    def test_case_insensitive_markers(self):
        a = classify_block_state(status=200, content_type="text/html", body="UNUSUAL ACTIVITY")
        assert a.state is BlockState.SOFT_BLOCK

    def test_blockstate_str_equality(self):
        assert BlockState.HEALTHY == "healthy"
        assert BlockState.SOFT_BLOCK.value == "soft_block"

    def test_is_block_property(self):
        assert BlockState.HEALTHY.is_block is False
        assert BlockState.CAPTCHA.is_block is True
        assert BlockState.SOFT_BLOCK.is_block is True
        assert BlockState.RATE_LIMITED.is_block is True
        assert BlockState.LOGIN_WALL.is_block is True
