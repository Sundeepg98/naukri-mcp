"""Honesty tests for the api.* health probes.

Context (2026-08-21): naukri_daily_brief was printing

    {"priority": "high", "action": "System health alert: api.login_check failing"}

in the same minute that naukri_auth_status returned
``{"logged_in": true, "verified": true, "reason": "api_confirmed"}`` and every
authenticated section of the brief returned real data.

The probe was the liar. It GET-ed
``/cloudgateway-myaccount/myaccount/getaccountdetails``, a route that does not
exist on Naukri's gateway and answers **HTTP 404** regardless of session state
(verified live through two independent clients). Its bare ``except Exception``
turned that 404 into ``status="unhealthy"``, and because the probe is
``criticality="critical"`` it landed in ``summary()["critical_failures"]`` and
became a high-priority action in every brief. A check that cannot pass certifies
nothing -- it is the same disease as a check that cannot fail.

These tests pin BOTH verdicts for each probe, so none of them can regress into
a one-answer check:

  * healthy   -- the session is confirmed
  * unhealthy -- the session is PROVEN dead (401/403, api_denied, no token)
  * degraded  -- the check could not be completed; NOT a logout claim, and
                 deliberately not a critical failure, so it cannot cry wolf.

All tests are pure: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server.api import NaukriAPIError
from naukri_server.config import ACTIVITY_LEVEL_API
from naukri_server.health import probe_registry
from naukri_server.health.framework import HealthProbeRegistry, _RegisteredProbe
from naukri_server.health.probes import api as api_probes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_api_client(**client_attrs):
    """Patch the api_client seen by BOTH probe generations.

    The probe module imports ``api_client`` lazily from
    ``naukri_server.interfaces`` (resolved per call), while
    ``services.auth_service`` binds it at module import time. Patching only one
    of the two would let a test pass for the wrong reason -- the code under test
    would quietly reach the real client. Patching both means the fake client is
    the only client, whichever path the probe takes.
    """
    fake = AsyncMock()
    for name, value in client_attrs.items():
        setattr(fake, name, value)
    return (
        patch("naukri_server.interfaces.api_client", fake),
        patch("naukri_server.services.auth_service.api_client", fake),
    )


async def _run_with_client(coro_factory, **client_attrs):
    """Await ``coro_factory()`` with both api_client references faked.

    The await happens INSIDE the patch context on purpose: building the
    coroutine under the patch and awaiting it outside would run the probe
    against the real client, and the test would pass or fail for a reason
    that has nothing to do with the code under test.
    """
    p1, p2 = _patch_api_client(**client_attrs)
    with p1, p2:
        return await coro_factory()


def _login_status(logged_in, verified, reason):
    return {"status": "success", "logged_in": logged_in,
            "verified": verified, "reason": reason}


# ---------------------------------------------------------------------------
# api.login_check -- the critical probe that cried wolf
# ---------------------------------------------------------------------------

class TestLoginCheckVerdicts:
    """The probe must be able to return all three verdicts."""

    @pytest.mark.asyncio
    async def test_healthy_when_api_confirms_session(self):
        with patch("naukri_server.services.auth_service.get_login_status",
                   new=AsyncMock(return_value=_login_status(True, True, "api_confirmed"))):
            result = await api_probes.api_login_check()
        assert result.status == "healthy", result.message

    @pytest.mark.asyncio
    async def test_unhealthy_when_api_denies_session(self):
        """A PROVEN logout is exactly what this critical probe exists to catch."""
        with patch("naukri_server.services.auth_service.get_login_status",
                   new=AsyncMock(return_value=_login_status(False, True, "api_denied"))):
            result = await api_probes.api_login_check()
        assert result.status == "unhealthy", result.message

    @pytest.mark.asyncio
    async def test_unhealthy_when_no_token(self):
        with patch("naukri_server.services.auth_service.get_login_status",
                   new=AsyncMock(return_value=_login_status(False, True, "no_token"))):
            result = await api_probes.api_login_check()
        assert result.status == "unhealthy", result.message

    @pytest.mark.asyncio
    async def test_degraded_when_check_could_not_complete(self):
        """verified=False is "could not check", which is NOT "logged out"."""
        with patch("naukri_server.services.auth_service.get_login_status",
                   new=AsyncMock(return_value=_login_status(False, False, "check_failed: TimeoutError"))):
            result = await api_probes.api_login_check()
        assert result.status == "degraded", result.message


class TestLoginCheckDoesNotCryWolf:
    """The regression guard for the 2026-08-21 false alarm."""

    @pytest.mark.asyncio
    async def test_unrelated_404_is_not_reported_as_a_dead_session(self):
        """A 404 says "no such route", never "you are logged out".

        This is the exact live condition: the endpoint answered 404 while the
        session was healthy. The old probe reported unhealthy -> critical
        failure -> a high-priority action in the daily brief.
        """
        raiser = AsyncMock(side_effect=NaukriAPIError(404, "Not Found"))
        with patch("naukri_server.interfaces.browser_provider.cached_token",
                   new=AsyncMock(return_value="tok")), \
             patch("naukri_server.services.auth_service.browser_provider.cached_token",
                   new=AsyncMock(return_value="tok")):
            result = await _run_with_client(
                lambda: api_probes.api_login_check(), get=raiser)

        assert result.status != "unhealthy", (
            "a 404 on an unrelated route must not be reported as a dead "
            "session -- that is the false alarm this test exists to stop; "
            "got: %s" % result.message
        )

    @pytest.mark.asyncio
    async def test_401_still_reports_unhealthy(self):
        """The mirror case: a real 401 MUST still escalate.

        Without this, "stop crying wolf" could be satisfied by a probe that
        never reports anything -- a check that cannot fail.
        """
        raiser = AsyncMock(side_effect=NaukriAPIError(401, "Unauthorized"))
        with patch("naukri_server.interfaces.browser_provider.cached_token",
                   new=AsyncMock(return_value="tok")), \
             patch("naukri_server.services.auth_service.browser_provider.cached_token",
                   new=AsyncMock(return_value="tok")):
            result = await _run_with_client(
                lambda: api_probes.api_login_check(), get=raiser)

        assert result.status == "unhealthy", result.message

    def test_probe_does_not_target_the_dead_route(self):
        """The 404 route must not come back on a copy-paste.

        Docstrings are stripped before the check: this module DESCRIBES the dead
        route at length so the next reader knows why it was removed, and a guard
        that cannot tell prose from a call would forbid documenting the bug.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(api_probes))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]

        executable = ast.unparse(ast.fix_missing_locations(tree))
        assert "cloudgateway-myaccount" not in executable, (
            "getaccountdetails is a 404 on every call -- verified live "
            "2026-08-21 through both api_client and a cookie-only fetch"
        )

    def test_the_dead_route_guard_can_actually_fail(self):
        """Control for the guard above -- it must reject a real call site.

        Without this, the grep could silently stop matching (wrong module,
        typo'd needle) and certify nothing.
        """
        import ast

        tree = ast.parse(
            'async def p():\n'
            '    """A docstring naming cloudgateway-myaccount harmlessly."""\n'
            '    return await api_client.get('
            '"/cloudgateway-myaccount/myaccount/getaccountdetails")\n'
        )
        for node in ast.walk(tree):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
        assert "cloudgateway-myaccount" in ast.unparse(tree)

    def test_probe_is_still_registered_as_critical(self):
        """Fixing the false alarm must not quietly demote the probe."""
        probe = probe_registry.get("api.login_check")
        assert probe is not None
        assert probe.criticality == "critical"


class TestDegradedDoesNotReachTheDailyBrief:
    """degraded must stay OUT of critical_failures; unhealthy must land in it.

    build_recommended_actions() reads summary()["critical_failures"] verbatim,
    so this is the exact channel the false alarm travelled down.
    """

    def _registry_with(self, status):
        reg = HealthProbeRegistry()
        probe = _RegisteredProbe(
            name="api.login_check", description="", criticality="critical",
            interval_seconds=300, execute=None, subsystem="api",
        )
        probe.last_result = api_probes.ProbeResult(status=status, message="x")
        reg._register(probe)
        return reg

    def test_unhealthy_critical_reaches_the_brief(self):
        summary = self._registry_with("unhealthy").summary()
        assert summary["critical_failures"] == ["api.login_check"]

    def test_degraded_critical_does_not_reach_the_brief(self):
        summary = self._registry_with("degraded").summary()
        assert summary["critical_failures"] == []


# ---------------------------------------------------------------------------
# api.profile -- HTTP 400 on every run (missing expand_level)
# ---------------------------------------------------------------------------

class TestProfileProbe:
    @pytest.mark.asyncio
    async def test_sends_expand_level_so_the_call_is_not_a_400(self):
        """PROFILE_API without expand_level answers 400 Bad Request.

        Live 2026-08-21: the probe reported "Profile API: HTTP 400" while
        naukri_health_check's own profile check -- same client, same endpoint,
        plus expand_level=4 -- returned "Profile loaded: A. Candidate".
        """
        async def fake_get(path, params=None, **kwargs):
            if not params or "expand_level" not in params:
                raise NaukriAPIError(400, "Bad Request")
            return {"profile": [{"name": "A. Candidate"}]}

        result = await _run_with_client(
            lambda: api_probes.api_profile(), get=AsyncMock(side_effect=fake_get))
        assert result.status == "healthy", result.message

    @pytest.mark.asyncio
    async def test_still_reports_unhealthy_on_a_real_failure(self):
        result = await _run_with_client(
            lambda: api_probes.api_profile(),
            get=AsyncMock(side_effect=NaukriAPIError(500, "Server Error")))
        assert result.status == "unhealthy", result.message


# ---------------------------------------------------------------------------
# api.recommendations -- HTTP 405 on every run (wrong verb)
# ---------------------------------------------------------------------------

class TestRecommendationsProbe:
    @pytest.mark.asyncio
    async def test_uses_post_because_recom_jobs_rejects_get(self):
        """recom-jobs is a POST-only search endpoint; GET is 405 forever."""
        get = AsyncMock(side_effect=NaukriAPIError(405, "Method Not Allowed"))
        post = AsyncMock(return_value={"jobDetails": [{"id": 1}, {"id": 2}]})

        result = await _run_with_client(
            lambda: api_probes.api_recommendations(), get=get, post=post)

        assert result.status == "healthy", result.message
        assert result.metadata["count"] == 2
        assert post.await_count == 1
        assert get.await_count == 0, "GET on recom-jobs is a permanent 405"

    @pytest.mark.asyncio
    async def test_still_degrades_on_a_real_failure(self):
        result = await _run_with_client(
            lambda: api_probes.api_recommendations(),
            get=AsyncMock(side_effect=NaukriAPIError(500, "Server Error")),
            post=AsyncMock(side_effect=NaukriAPIError(500, "Server Error")))
        assert result.status == "degraded", result.message


# ---------------------------------------------------------------------------
# api.metrics -- the probe the three broken probes were poisoning
# ---------------------------------------------------------------------------

class TestMetricsProbeVerdicts:
    """api.metrics read 62% error rate live, driven by the three probes above
    failing on a 300s loop (~864 self-inflicted errors/day). Both verdicts are
    pinned so the probe stays a real instrument."""

    @pytest.mark.asyncio
    async def test_healthy_on_a_clean_error_rate(self):
        with patch("naukri_server.api.api_metrics") as m:
            m.total = 100
            m.errors = 1
            result = await api_probes.api_metrics_check()
        assert result.status == "healthy", result.message

    @pytest.mark.asyncio
    async def test_unhealthy_on_a_high_error_rate(self):
        with patch("naukri_server.api.api_metrics") as m:
            m.total = 100
            m.errors = 62
            result = await api_probes.api_metrics_check()
        assert result.status == "unhealthy", result.message
