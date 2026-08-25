"""Tests for dashboard REST API routes.

Every test is PURE: no network, no browser, no file I/O.
Route handlers are called directly with a mock Request object.
"""

import json
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from starlette.requests import Request


# ── Helpers ──────────────────────────────────────────────────


def _mock_request(query_params=None):
    """Create a minimal mock Request for route handler testing.

    query_params is a plain dict — routes use .get(key, default) which
    dict supports natively.
    """
    req = MagicMock(spec=Request)
    req.query_params = query_params or {}
    return req


# =====================================================================
# 1. /api/dashboard — Morning brief
# =====================================================================


class TestApiDashboard:
    """Tests for the api_dashboard route handler."""

    @patch("naukri_server.tools.daily_brief.naukri_daily_brief", new_callable=AsyncMock)
    async def test_success(self, mock_brief):
        from naukri_server.dashboard.routes import api_dashboard

        mock_brief.return_value = {
            "status": "success",
            "recommended_actions": ["Apply to 3 new jobs"],
        }
        resp = await api_dashboard(_mock_request())

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["status"] == "success"
        assert len(body["recommended_actions"]) == 1
        mock_brief.assert_awaited_once()

    @patch("naukri_server.tools.daily_brief.naukri_daily_brief", new_callable=AsyncMock)
    async def test_error_returns_500(self, mock_brief):
        from naukri_server.dashboard.routes import api_dashboard

        mock_brief.side_effect = RuntimeError("brief failed")
        resp = await api_dashboard(_mock_request())

        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert body["status"] == "error"
        assert "brief failed" in body["message"]

    @patch("naukri_server.tools.daily_brief.naukri_daily_brief", new_callable=AsyncMock)
    async def test_returns_full_result(self, mock_brief):
        from naukri_server.dashboard.routes import api_dashboard

        data = {"status": "success", "applications": 50, "views": 200}
        mock_brief.return_value = data
        resp = await api_dashboard(_mock_request())

        body = json.loads(resp.body)
        assert body == data


# =====================================================================
# 2. /api/funnel — Conversion funnel
# =====================================================================


class TestApiFunnel:
    """Tests for the api_funnel route handler."""

    @patch("naukri_server.services.insights_service.conversion_funnel", new_callable=AsyncMock)
    async def test_success_with_days_param(self, mock_funnel):
        from naukri_server.dashboard.routes import api_funnel

        mock_funnel.return_value = {
            "status": "success",
            "total_applied": 50,
            "funnel": {"applied": 50, "viewed": 20},
        }
        resp = await api_funnel(_mock_request({"days": "30"}))

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["total_applied"] == 50
        mock_funnel.assert_called_once_with(30)

    @patch("naukri_server.services.insights_service.conversion_funnel", new_callable=AsyncMock)
    async def test_default_days_is_30(self, mock_funnel):
        from naukri_server.dashboard.routes import api_funnel

        mock_funnel.return_value = {"status": "success"}
        await api_funnel(_mock_request())

        mock_funnel.assert_called_once_with(30)

    @patch("naukri_server.services.insights_service.conversion_funnel", new_callable=AsyncMock)
    async def test_custom_days(self, mock_funnel):
        from naukri_server.dashboard.routes import api_funnel

        mock_funnel.return_value = {"status": "success"}
        await api_funnel(_mock_request({"days": "7"}))

        mock_funnel.assert_called_once_with(7)

    @patch("naukri_server.services.insights_service.conversion_funnel", new_callable=AsyncMock)
    async def test_error_returns_500(self, mock_funnel):
        from naukri_server.dashboard.routes import api_funnel

        mock_funnel.side_effect = Exception("db error")
        resp = await api_funnel(_mock_request())

        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert body["status"] == "error"


# =====================================================================
# 3. /api/health — Health probes
# =====================================================================


class TestApiHealth:
    """Tests for the api_health route handler."""

    async def test_all_healthy(self):
        from naukri_server.dashboard.routes import api_health

        mock_probe = MagicMock()
        mock_probe.last_result = MagicMock(
            status="healthy", message="OK", elapsed_ms=5.0,
            timestamp="2026-01-01T00:00:00Z",
        )
        mock_probe.criticality = "critical"

        with patch("naukri_server.health.probe_registry") as mock_reg:
            mock_reg._probes = {"api.connectivity": mock_probe}
            resp = await api_health(_mock_request())

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["overall"] == "healthy"
        assert body["probe_count"] == 1
        assert body["probes"]["api.connectivity"]["status"] == "healthy"
        assert body["probes"]["api.connectivity"]["criticality"] == "critical"

    async def test_unhealthy_overrides_overall(self):
        from naukri_server.dashboard.routes import api_health

        healthy_probe = MagicMock()
        healthy_probe.last_result = MagicMock(
            status="healthy", message="OK", elapsed_ms=5.0, timestamp="t",
        )
        healthy_probe.criticality = "warning"

        unhealthy_probe = MagicMock()
        unhealthy_probe.last_result = MagicMock(
            status="unhealthy", message="Down", elapsed_ms=30000, timestamp="t",
        )
        unhealthy_probe.criticality = "critical"

        with patch("naukri_server.health.probe_registry") as mock_reg:
            mock_reg._probes = {"api": healthy_probe, "browser": unhealthy_probe}
            resp = await api_health(_mock_request())

        body = json.loads(resp.body)
        assert body["overall"] == "unhealthy"
        assert body["probe_count"] == 2

    async def test_degraded_overall(self):
        from naukri_server.dashboard.routes import api_health

        degraded_probe = MagicMock()
        degraded_probe.last_result = MagicMock(
            status="degraded", message="Slow", elapsed_ms=5000, timestamp="t",
        )
        degraded_probe.criticality = "warning"

        with patch("naukri_server.health.probe_registry") as mock_reg:
            mock_reg._probes = {"api": degraded_probe}
            resp = await api_health(_mock_request())

        body = json.loads(resp.body)
        assert body["overall"] == "degraded"

    async def test_no_results_yet_shows_unknown(self):
        from naukri_server.dashboard.routes import api_health

        probe = MagicMock()
        probe.last_result = None
        probe.criticality = "informational"

        with patch("naukri_server.health.probe_registry") as mock_reg:
            mock_reg._probes = {"new_probe": probe}
            resp = await api_health(_mock_request())

        body = json.loads(resp.body)
        assert body["probes"]["new_probe"]["status"] == "unknown"
        assert body["probes"]["new_probe"]["message"] == "No data yet"
        assert body["probes"]["new_probe"]["elapsed_ms"] is None
        assert body["probes"]["new_probe"]["timestamp"] is None

    async def test_empty_probes(self):
        from naukri_server.dashboard.routes import api_health

        with patch("naukri_server.health.probe_registry") as mock_reg:
            mock_reg._probes = {}
            resp = await api_health(_mock_request())

        body = json.loads(resp.body)
        assert body["overall"] == "healthy"
        assert body["probe_count"] == 0
        assert body["probes"] == {}


# =====================================================================
# 4. /api/stale — Stale applications
# =====================================================================


class TestApiStale:
    """Tests for the api_stale route handler."""

    @patch(
        "naukri_server.services.application_service.get_stale_applications",
        new_callable=AsyncMock,
    )
    async def test_success_with_defaults(self, mock_stale):
        from naukri_server.dashboard.routes import api_stale

        mock_stale.return_value = {"status": "success", "total": 3, "stale_applications": []}
        resp = await api_stale(_mock_request())

        assert resp.status_code == 200
        mock_stale.assert_called_once_with(days_threshold=14, min_stale_score=40)

    @patch(
        "naukri_server.services.application_service.get_stale_applications",
        new_callable=AsyncMock,
    )
    async def test_custom_params(self, mock_stale):
        from naukri_server.dashboard.routes import api_stale

        mock_stale.return_value = {"status": "success", "total": 0}
        resp = await api_stale(_mock_request({"days": "7", "score": "60"}))

        assert resp.status_code == 200
        mock_stale.assert_called_once_with(days_threshold=7, min_stale_score=60)

    @patch(
        "naukri_server.services.application_service.get_stale_applications",
        new_callable=AsyncMock,
    )
    async def test_error_returns_500(self, mock_stale):
        from naukri_server.dashboard.routes import api_stale

        mock_stale.side_effect = Exception("service error")
        resp = await api_stale(_mock_request())

        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert body["status"] == "error"


# =====================================================================
# 5. /api/notifications — Unified notifications
# =====================================================================


class TestApiNotifications:
    """Tests for the api_notifications route handler."""

    @patch(
        "naukri_server.tools.notifications._get_unified_notify",
        new_callable=AsyncMock,
    )
    async def test_success(self, mock_notif):
        from naukri_server.dashboard.routes import api_notifications

        mock_notif.return_value = {
            "status": "success",
            "notifications": [{"type": "viewed", "message": "Recruiter viewed"}],
        }
        resp = await api_notifications(_mock_request())

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["status"] == "success"
        assert len(body["notifications"]) == 1

    @patch(
        "naukri_server.tools.notifications._get_unified_notify",
        new_callable=AsyncMock,
    )
    async def test_error_returns_500(self, mock_notif):
        from naukri_server.dashboard.routes import api_notifications

        mock_notif.side_effect = Exception("API down")
        resp = await api_notifications(_mock_request())

        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert "API down" in body["message"]


# =====================================================================
# 6. /api/scheduler — Scheduler status
# =====================================================================


class TestApiScheduler:
    """Tests for the api_scheduler route handler."""

    @patch("naukri_server.database.get_last_run", new_callable=AsyncMock)
    async def test_success_with_tasks(self, mock_last_run):
        from naukri_server.dashboard.routes import api_scheduler

        mock_last_run.return_value = {
            "started_at": "2026-01-01T10:00:00",
            "status": "completed",
            "duration_ms": 100,
        }

        mock_sched = MagicMock()
        mock_sched.status = {
            "running": True,
            "total_tasks": 8,
            "enabled_tasks": 8,
            "disabled_tasks": 0,
            "tasks": {
                "sync": {"enabled": True, "interval_seconds": 3600},
            },
        }

        with patch("naukri_server.scheduler.scheduler", mock_sched):
            resp = await api_scheduler(_mock_request())

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["status"] == "success"
        assert body["running"] is True
        assert body["tasks"]["sync"]["last_run"]["status"] == "completed"

    @patch("naukri_server.database.get_last_run", new_callable=AsyncMock)
    async def test_task_with_no_last_run(self, mock_last_run):
        from naukri_server.dashboard.routes import api_scheduler

        mock_last_run.return_value = None

        mock_sched = MagicMock()
        mock_sched.status = {
            "running": True,
            "total_tasks": 1,
            "enabled_tasks": 1,
            "disabled_tasks": 0,
            "tasks": {"sync": {"enabled": True, "interval_seconds": 3600}},
        }

        with patch("naukri_server.scheduler.scheduler", mock_sched):
            resp = await api_scheduler(_mock_request())

        body = json.loads(resp.body)
        assert body["tasks"]["sync"]["last_run"] is None

    async def test_not_initialized_returns_503(self):
        from naukri_server.dashboard.routes import api_scheduler

        with patch("naukri_server.scheduler.scheduler", None):
            resp = await api_scheduler(_mock_request())

        assert resp.status_code == 503
        body = json.loads(resp.body)
        assert body["status"] == "error"
        assert "not initialized" in body["message"]

    @patch("naukri_server.database.get_last_run", new_callable=AsyncMock)
    async def test_error_returns_500(self, mock_last_run):
        from naukri_server.dashboard.routes import api_scheduler

        mock_last_run.side_effect = Exception("DB error")

        mock_sched = MagicMock()
        mock_sched.status = {
            "running": True,
            "total_tasks": 1,
            "enabled_tasks": 1,
            "disabled_tasks": 0,
            "tasks": {"sync": {"enabled": True}},
        }

        with patch("naukri_server.scheduler.scheduler", mock_sched):
            resp = await api_scheduler(_mock_request())

        assert resp.status_code == 500


# =====================================================================
# 7. /api/stats — Application statistics
# =====================================================================


class TestApiStats:
    """Tests for the api_stats route handler."""

    @patch("naukri_server.database.list_scheduled_runs", new_callable=AsyncMock)
    @patch("naukri_server.database.count_applications_by_status", new_callable=AsyncMock)
    async def test_success(self, mock_counts, mock_runs):
        from naukri_server.dashboard.routes import api_stats

        mock_counts.return_value = {"applied": 50, "viewed": 20, "shortlisted": 5}
        mock_runs.return_value = [
            {"task_name": "sync", "status": "completed"},
            {"task_name": "brief", "status": "completed"},
        ]
        resp = await api_stats(_mock_request())

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["status"] == "success"
        assert body["applications_by_status"]["applied"] == 50
        assert len(body["recent_scheduled_runs"]) == 2
        mock_runs.assert_called_once_with(limit=10)

    @patch("naukri_server.database.list_scheduled_runs", new_callable=AsyncMock)
    @patch("naukri_server.database.count_applications_by_status", new_callable=AsyncMock)
    async def test_error_returns_500(self, mock_counts, mock_runs):
        from naukri_server.dashboard.routes import api_stats

        mock_counts.side_effect = Exception("DB failure")
        resp = await api_stats(_mock_request())

        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert body["status"] == "error"


# =====================================================================
# 8. /dashboard — HTML page
# =====================================================================


class TestDashboardHtml:
    """Tests for the dashboard_html route handler."""

    async def test_success_returns_html(self):
        from naukri_server.dashboard.routes import dashboard_html

        resp = await dashboard_html(_mock_request())

        assert resp.status_code == 200
        body = resp.body.decode() if isinstance(resp.body, bytes) else resp.body
        assert "<!DOCTYPE html>" in body
        assert "Naukri" in body

    async def test_template_missing_returns_500(self):
        from naukri_server.dashboard.routes import dashboard_html

        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            resp = await dashboard_html(_mock_request())

        assert resp.status_code == 500
        body = resp.body.decode() if isinstance(resp.body, bytes) else resp.body
        assert "not found" in body.lower()


class TestPropertiesWeAskForAndUsedToDiscard:
    """`DASHBOARD_PROPERTIES` requests ten properties; five were read by nothing.

    incompleteSection, profileSegment, res360NotifType, campusData and
    aiInterviewEligibility each had ZERO non-config references across the
    package. Naukri computed them, sent them on every dashboard call, and they
    were dropped.

    Asking for a field and discarding it is worse than not asking: it costs the
    same request, and it reads to the next person as though the area were
    covered. These tests pin that the bytes now reach the caller.
    """

    PROPS = ("incomplete_section", "profile_segment", "res360_notif_type",
             "campus_data", "ai_interview_eligibility", "profile_performance")

    @staticmethod
    async def _run(dash: dict) -> dict:
        from naukri_server.services import profile_service as S
        with patch.object(S, "api_client") as client:
            client.get = AsyncMock(return_value={"dashBoard": dash})
            return await S._get_dashboard()

    @pytest.mark.asyncio
    async def test_every_requested_property_reaches_the_caller(self):
        sent = {
            "incompleteSection": ["resume"], "profileSegment": "SEGMENT_X",
            "res360NotifType": 3, "campusData": {"isCampus": False},
            "aiInterviewEligibility": {"eligible": True, "slots": 2},
            "profilePerformance": {"views": 12},
        }
        out = await self._run(dict(sent, profileViewCount=5))
        block = out["requested_unparsed"]
        assert sorted(block["present"]) == sorted(self.PROPS)
        assert block["absent"] == []
        # values arrive UNINTERPRETED -- the point is the shape survives
        assert block["raw"]["ai_interview_eligibility"] == {"eligible": True, "slots": 2}
        assert block["raw"]["incomplete_section"] == ["resume"]

    @pytest.mark.asyncio
    async def test_absent_is_reported_rather_than_silently_missing(self):
        """"We asked and Naukri sent nothing" must be distinguishable from
        "we never asked". That distinction is the entire point of the block."""
        out = await self._run({"profileViewCount": 5})
        block = out["requested_unparsed"]
        assert block["present"] == []
        assert sorted(block["absent"]) == sorted(self.PROPS)
        assert block["raw"] == {}

    @pytest.mark.asyncio
    async def test_an_empty_structure_counts_as_PRESENT_not_absent(self):
        """`campusData: {}` is Naukri answering, not Naukri staying silent.

        Written because the obvious implementation -- a truthiness test -- gets
        this backwards and reports an empty payload as "not sent", which is the
        same information loss in a new place.
        """
        out = await self._run({"campusData": {}, "res360NotifType": 0})
        block = out["requested_unparsed"]
        assert "campus_data" in block["present"]
        assert "res360_notif_type" in block["present"]
        assert block["raw"]["campus_data"] == {}
        assert block["raw"]["res360_notif_type"] == 0

    def test_every_requested_property_is_either_parsed_or_passed_through(self):
        """The real guard: nothing in DASHBOARD_PROPERTIES may be discarded.

        This is what stops the defect recurring. Add a property to the request
        string and forget to read it, and this fails -- which is exactly what
        nobody noticed for six properties -- the sixth found by this guard, not by
        the survey that preceded it.
        """
        import re
        from naukri_server import config
        import naukri_server

        # SCAN THE WHOLE PACKAGE, not one module. The first version of this
        # check read only profile_service.py and flagged userDetails,
        # profilePerformance and photoInfo -- two of which are consumed
        # elsewhere (insights_service.py:509, resume_photo.py:186). A guard
        # with too narrow a FILE SET manufactures findings exactly as readily
        # as one with too narrow a pattern misses them, and this one was shown
        # doing it before the scope was widened.
        root = Path(naukri_server.__file__).parent
        blob = "\n".join(
            f.read_text(encoding="utf-8", errors="replace")
            for f in root.rglob("*.py")
            if f.name != "config.py"
        )
        requested = [p.strip() for p in config.DASHBOARD_PROPERTIES.split(",")]
        unread = [p for p in requested
                  if not re.search(r'["\']%s["\']' % re.escape(p), blob)]
        assert unread == [], (
            "requested from Naukri and read by NOTHING in the package: %s -- "
            "parse it, consume it, or add it to the pass-through block. "
            "Asking for a field and discarding it costs the same request and "
            "reads to the next person as though the area were covered." % unread
        )
