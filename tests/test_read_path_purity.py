"""Read paths must not mutate - the whole defect class, plus the guard that
catches the next one.

`tests/test_reminder_storm.py` fixed ONE event, `ReminderDue`. The same shape
was live in four more places. Measured on his DB, 2026-08-21, 222 undelivered
notifications:

    ApplicationStale     85 rows / 17 distinct job_ids / 5 identical bursts
    SavedJobExpiring      5 rows /  2 distinct job_ids
    ProfileScoreChanged   1 row
    RecruiterEngaged      0 rows

Four of the five ApplicationStale bursts sat exactly on the 6h `stale_check`
cadence; the fifth (15:07) did not. That fifth burst is a read caught in the
act, and it is why this is a measurement rather than an argument.

Two of the four turned out NOT to be the plain read-path shape, and are fixed
differently. Both corrections are pinned below:

  * `ProfileScoreChanged` had no scheduled caller, so an `emit_events` opt-in
    would have left a parameter nothing ever sets. It also lied - `old_score`
    was hardcoded to 0 on every emit. Fixed by change detection instead.
  * `RecruiterEngaged`'s read-path emit was UNREACHABLE: it tested a field
    parsed from keys the API does not return. Removed rather than gated, and
    the field-name bug fixed separately so the tool stops reporting a blank
    action for all 95 of his recorded recruiter actions.

Every guard here ships with a CONTROL proving it can fail. A suppression fix is
trivially satisfiable by suppressing everything, and this codebase has already
produced more than seven checks that could not fail - two of them found today.
"""

import ast
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from naukri_server.events import (
    ApplicationStale, SavedJobExpiring, ProfileScoreChanged, RecruiterEngaged,
)

PKG = Path(__file__).resolve().parent.parent / "naukri_server"


# =====================================================================
# 0. THE GUARD - a source census of every emit, and what it is allowed to be
#
# A behavioural test cannot see a NEW read path added next month. These can.
# The census is extracted as a plain function so it can be pointed at a
# synthetic tree and SHOWN both detecting and not detecting - a source guard
# that has never been run against a known-bad tree is exactly the kind of check
# that certifies nothing.
# =====================================================================

# Function-name prefixes that declare "this is a read". Matched after leading
# underscores are stripped, so `_audit_profile` and `_read_message` count.
READ_PREFIXES = (
    "get_", "list_", "fetch_", "read_", "audit_", "search_", "find_", "load_",
)

# Read-shaped functions allowed to emit WITHOUT an `emit_events` guard. Each
# needs a reason, and the reason is printed on failure. This list is the
# durable record of the exceptions - a "known defect" entry here is visible to
# the suite forever, where a TODO comment is not.
READ_SHAPED_EMIT_ALLOWLIST = {
    ("services/profile_service.py", "_audit_profile"):
        "ProfileScoreChanged is change-detected, not opt-in: it emits only "
        "when the score actually differs from the last logged one. There is no "
        "scheduled profile audit to opt in, so a flag would be a decoy.",
    ("tools/inbox.py", "_read_message"):
        "KNOWN DEFECT, reported not fixed 2026-08-21: reading a message emits "
        "InboxMessageRead, whose subscriber banks a notification per read. Same "
        "class as the four fixed here; 0 rows banked so far only because the "
        "tool has not been used. Fix is the same two halves.",
}

# Every (file, function, event) that may emit at all. A new emit ANYWHERE fails
# this until someone adds it here, which forces the author to answer "is the
# function I just put this in a read?". Deliberately brittle: a rename should
# make someone look.
EMIT_CENSUS = {
    ("agent.py", "_act", "AgentActCompleted"),
    ("agent.py", "_act", "AgentJobApplied"),
    ("agent.py", "_decide", "AgentDecideCompleted"),
    ("agent.py", "_decide", "AgentJobSkipped"),
    ("agent.py", "_learn", "AgentCycleCompleted"),
    ("agent.py", "_observe", "AgentObserveCompleted"),
    ("agent.py", "_run_agent_cycle_bound", "AgentCycleStarted"),
    ("browser_watchdog.py", "_attempt_restart", "BrowserRecovered"),
    ("browser_watchdog.py", "_handle_failure", "BrowserCrashed"),
    ("healing/t1_autofix.py", "apply_t1_fix", "AutoFixApplied"),
    ("healing/t2_autofix.py", "apply_t2_fix", "AutoFixApplied"),
    ("healing/t2_autofix.py", "verify_pending_row", "AutoFixReverted"),
    ("health/framework.py", "_on_result", "ProbeStateChanged"),
    ("health/probes/discovery.py", "discover_config_audit", "NewEndpointDiscovered"),
    ("probes/api_validator.py", "api_validator_probe", "EndpointDriftDetected"),
    ("scheduler.py", "_execute_task", "ScheduledTaskCompleted"),
    ("services/application_service.py", "add_interview_round", "ApplicationInterviewScheduled"),
    ("services/application_service.py", "get_stale_applications", "ApplicationStale"),
    ("services/application_service.py", "purge_applications", "ApplicationsPurged"),
    ("services/profile_service.py", "_audit_profile", "ProfileScoreChanged"),
    ("services/reminder_service.py", "list_reminders", "ReminderDue"),
    ("services/reminder_service.py", "set_reminder", "ReminderSet"),
    ("services/saved_jobs_service.py", "list_saved_jobs", "SavedJobExpiring"),
    ("services/saved_jobs_service.py", "save_job", "SavedJobAdded"),
    ("services/saved_jobs_service.py", "unsave_job", "SavedJobRemoved"),
    ("tools/agent_tool.py", "_agent_approve", "AgentJobApplied"),
    ("tools/alerts.py", "_create", "AlertCreated"),
    ("tools/alerts.py", "_delete_alert_browser", "AlertDeleted"),
    ("tools/alerts.py", "_update_alert_browser", "AlertUpdated"),
    ("tools/apply.py", "_finalize_applied", "ApplicationSubmitted"),
    ("tools/inbox.py", "_accept_nvite", "InboxInviteAccepted"),
    ("tools/inbox.py", "_mark_interested", "RecruiterEngaged"),
    ("tools/inbox.py", "_read_message", "InboxMessageRead"),
    ("tools/insights.py", "_cached_answers", "CachedAnswerDeleted"),
    ("tools/insights.py", "_cached_answers", "CachedAnswerUpdated"),
    ("tools/profile_update.py", "_boost_visibility", "ProfileBoosted"),
    ("tools/profile_update.py", "_update_profile", "ProfileUpdated"),
    ("tools/resume_photo.py", "_photo_delete", "PhotoDeleted"),
    ("tools/resume_photo.py", "_photo_upload", "PhotoUploaded"),
    ("tools/resume_photo.py", "_resume_upload", "ResumeUploaded"),
    ("tools/settings.py", "_update_settings", "SettingsUpdated"),
    ("tools/sync.py", "_sync_applications", "SyncCompletedEvent"),
    ("tools/sync.py", "step_emit_events", "ApplicationStatusChanged"),
    ("tools/sync.py", "step_emit_events", "SyncCompletedEvent"),
}


class _EmitVisitor(ast.NodeVisitor):
    """Collect every `<something with 'bus' in it>.emit(Event(...))` call site.

    `guarded` is True when the call sits lexically inside the BODY of an `if`
    whose test mentions the name `emit_events` - the opt-in shape. An `else`
    branch is deliberately not guarded.
    """

    def __init__(self, relpath, out):
        self.relpath = relpath
        self.out = out
        self.funcs = []
        self.guard_depth = 0

    def visit_FunctionDef(self, node):
        self.funcs.append(node.name)
        self.generic_visit(node)
        self.funcs.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node):
        self.visit(node.test)
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        guarded = "emit_events" in names
        if guarded:
            self.guard_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        if guarded:
            self.guard_depth -= 1
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "emit":
            base = ast.unparse(func.value)
            if "bus" in base:
                event = ast.unparse(node.args[0]).split("(")[0] if node.args else "?"
                self.out.append({
                    "file": self.relpath,
                    "function": self.funcs[-1] if self.funcs else "<module>",
                    "event": event,
                    "line": node.lineno,
                    "guarded": self.guard_depth > 0,
                })
        self.generic_visit(node)


def emit_sites(root: Path) -> list:
    """Every event-bus emit call site under `root`, with its guard state."""
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        _EmitVisitor(path.relative_to(root).as_posix(), found).visit(tree)
    return found


def _is_read_shaped(name: str) -> bool:
    return name.lstrip("_").startswith(READ_PREFIXES)


# --- controls: the census must be shown DETECTING and NOT detecting ---------

_BAD_READ = '''
from naukri_server.events import event_bus, Thing

async def list_widgets():
    """A read that mutates - exactly the shape this guard exists to catch."""
    for w in await fetch():
        await event_bus.emit(Thing(id=w))
'''

_GOOD_READ = '''
from naukri_server.events import event_bus, Thing

async def list_widgets(emit_events: bool = False):
    """The fixed shape: emission is opt-in."""
    for w in await fetch():
        if emit_events:
            await event_bus.emit(Thing(id=w))
'''

_ALIASED_BUS = '''
from naukri_server.events import event_bus as bus, Thing

async def list_widgets():
    await bus.emit(Thing())
'''

_NO_EMITS = '''
async def list_widgets():
    return [1, 2, 3]

async def save_widget(w):
    await db.write(w)
'''


class TestTheGuardItself:
    """The census is an instrument. An instrument that has never been shown
    failing certifies nothing."""

    def test_CONTROL_census_detects_an_unguarded_read_emit(self, tmp_path):
        (tmp_path / "bad.py").write_text(_BAD_READ, encoding="utf-8")
        sites = emit_sites(tmp_path)

        assert len(sites) == 1, "the census missed an emit it must catch"
        assert sites[0]["function"] == "list_widgets"
        assert sites[0]["event"] == "Thing"
        assert sites[0]["guarded"] is False
        assert _is_read_shaped(sites[0]["function"])

    def test_CONTROL_census_marks_the_opt_in_shape_guarded(self, tmp_path):
        (tmp_path / "good.py").write_text(_GOOD_READ, encoding="utf-8")
        sites = emit_sites(tmp_path)

        assert len(sites) == 1
        assert sites[0]["guarded"] is True, (
            "the fixed shape must read as guarded, or the guard flags every "
            "fix it just asked for"
        )

    def test_CONTROL_census_follows_a_renamed_bus(self, tmp_path):
        """`from ... import event_bus as bus` must not dodge the census."""
        (tmp_path / "alias.py").write_text(_ALIASED_BUS, encoding="utf-8")
        assert len(emit_sites(tmp_path)) == 1

    def test_CONTROL_census_is_silent_on_a_tree_with_no_emits(self, tmp_path):
        """The other direction: no false positives, or the guard is noise."""
        (tmp_path / "clean.py").write_text(_NO_EMITS, encoding="utf-8")
        assert emit_sites(tmp_path) == []

    def test_CONTROL_read_prefix_matcher_separates_reads_from_writes(self):
        for name in ("get_stale_applications", "list_saved_jobs",
                     "_audit_profile", "_read_message", "fetch_inbox"):
            assert _is_read_shaped(name), name
        for name in ("save_job", "_apply", "_update_profile", "purge_applications",
                     "_mark_interested", "set_reminder"):
            assert not _is_read_shaped(name), name


class TestNoReadPathEmits:
    """The semantic guard: a read-shaped function may emit only behind an
    `emit_events` opt-in, or with a written reason."""

    def test_no_read_shaped_function_emits_unguarded(self):
        violations = [
            s for s in emit_sites(PKG)
            if _is_read_shaped(s["function"])
            and not s["guarded"]
            and (s["file"], s["function"]) not in READ_SHAPED_EMIT_ALLOWLIST
        ]
        assert violations == [], (
            "read-shaped function(s) emitting unguarded - every caller of these "
            "becomes a writer, which is how 17 stale applications became 85 "
            "notifications:\n%s" % "\n".join(
                "  %s::%s emits %s at line %d" %
                (v["file"], v["function"], v["event"], v["line"])
                for v in violations
            )
        )

    def test_the_allowlist_entries_still_exist_and_still_emit(self):
        """An allowlist whose entries have gone stale silences nothing real and
        hides the fact that the exception has been resolved."""
        actual = {(s["file"], s["function"]) for s in emit_sites(PKG)
                  if not s["guarded"]}
        stale = sorted(set(READ_SHAPED_EMIT_ALLOWLIST) - actual)
        assert stale == [], (
            "allowlisted read-path emitter(s) no longer emit unguarded - delete "
            "the entry: %s" % stale
        )

    def test_emit_census_matches_the_frozen_allowlist(self):
        """The completeness net. The prefix guard above cannot see an emit added
        to a read whose NAME is not read-shaped (`_daily_brief` is the obvious
        one). This can."""
        actual = {(s["file"], s["function"], s["event"]) for s in emit_sites(PKG)}
        added = sorted(actual - EMIT_CENSUS)
        removed = sorted(EMIT_CENSUS - actual)
        assert not added and not removed, (
            "the set of event emitters changed.\n"
            "  NEW (is the enclosing function a READ? if so, gate it behind "
            "emit_events before adding it here):\n%s\n"
            "  GONE (intentional? update EMIT_CENSUS):\n%s"
            % ("\n".join("    %s::%s -> %s" % a for a in added) or "    none",
               "\n".join("    %s::%s -> %s" % r for r in removed) or "    none")
        )

    def test_only_scheduler_tasks_opts_into_emission(self):
        """Generalised from test_reminder_storm: now covers all three opt-in
        events, not just ReminderDue."""
        opted_in = sorted(
            p.relative_to(PKG).as_posix()
            for p in PKG.rglob("*.py")
            if "emit_events=True" in p.read_text(encoding="utf-8")
        )
        assert opted_in == ["scheduler_tasks.py"], (
            "only scheduled tasks may opt into emission; found %s" % opted_in
        )


# =====================================================================
# 1. ApplicationStale - the read path must not emit
# =====================================================================

def _stale_row(job_id="STALE-1", job_activity=5, ars_score=None, days_ago=10):
    """A closed application: staleness 100, well past STALE_MIN_SCORE=40."""
    applied = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        "job_id": job_id,
        "title": "Backend Engineer",
        "company": "Acme",
        "applied_at": applied,
        "days_since_applied": days_ago,
        "is_open": False,
        "view_count": 0,
        "job_activity": job_activity,
        "ars_score": ars_score,
    }


def _emitted(mock_emit, event_type):
    return [c.args[0] for c in mock_emit.call_args_list
            if isinstance(c.args[0], event_type)]


class TestApplicationStaleReadIsPure:

    @pytest.mark.asyncio
    async def test_read_does_not_emit(self):
        """RED before the fix: one event per high-priority stale application."""
        from naukri_server.services.application_service import get_stale_applications

        with patch("naukri_server.database.get_stale_applications_raw",
                   new_callable=AsyncMock, return_value=[_stale_row()]), \
             patch("naukri_server.database.list_applications",
                   new_callable=AsyncMock, return_value=([], 1)), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            result = await get_stale_applications()

        assert result["count"] == 1, "the application must still be REPORTED stale"
        assert result["stale_applications"][0]["follow_up_priority"] == 70, (
            "fixture drifted - it must clear the >=60 emit threshold, or this "
            "test passes for the wrong reason"
        )
        assert _emitted(mock_emit, ApplicationStale) == []

    @pytest.mark.asyncio
    async def test_his_shape_seventeen_stale_apps_emit_nothing_on_read(self):
        """17 applications, one brief call. That is the measured burst size."""
        from naukri_server.services.application_service import get_stale_applications

        rows = [_stale_row("STALE-%d" % i) for i in range(17)]
        with patch("naukri_server.database.get_stale_applications_raw",
                   new_callable=AsyncMock, return_value=rows), \
             patch("naukri_server.database.list_applications",
                   new_callable=AsyncMock, return_value=([], 17)), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            result = await get_stale_applications(limit=50)

        assert result["count"] == 17
        assert _emitted(mock_emit, ApplicationStale) == []

    @pytest.mark.asyncio
    async def test_CONTROL_opting_in_still_emits(self):
        """CONTROL. Without this, deleting the emit outright would pass while
        killing the only stale-application notification he gets."""
        from naukri_server.services.application_service import get_stale_applications

        with patch("naukri_server.database.get_stale_applications_raw",
                   new_callable=AsyncMock, return_value=[_stale_row()]), \
             patch("naukri_server.database.list_applications",
                   new_callable=AsyncMock, return_value=([], 1)), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            await get_stale_applications(emit_events=True)

        events = _emitted(mock_emit, ApplicationStale)
        assert len(events) == 1
        assert events[0].job_id == "STALE-1"

    @pytest.mark.asyncio
    async def test_CONTROL_below_threshold_never_emits_even_opted_in(self):
        """The >=60 priority gate must survive the opt-in refactor."""
        from naukri_server.services.application_service import get_stale_applications

        low = _stale_row("STALE-LOW", job_activity=0)  # base 50, no bonuses
        with patch("naukri_server.database.get_stale_applications_raw",
                   new_callable=AsyncMock, return_value=[low]), \
             patch("naukri_server.database.list_applications",
                   new_callable=AsyncMock, return_value=([], 1)), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            result = await get_stale_applications(emit_events=True)

        assert result["stale_applications"][0]["follow_up_priority"] == 50
        assert _emitted(mock_emit, ApplicationStale) == []

    @pytest.mark.asyncio
    async def test_days_since_applied_is_not_always_zero(self):
        """RED before the fix. `applied_date` is a bare YYYY-MM-DD, so
        fromisoformat returned a NAIVE datetime and the subtraction raised
        TypeError into a bare except - every one of the 85 ApplicationStale
        rows in his event_log carries days_since_applied "0"."""
        from naukri_server.services.application_service import get_stale_applications

        with patch("naukri_server.database.get_stale_applications_raw",
                   new_callable=AsyncMock, return_value=[_stale_row(days_ago=45)]), \
             patch("naukri_server.database.list_applications",
                   new_callable=AsyncMock, return_value=([], 1)), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            await get_stale_applications(emit_events=True)

        event = _emitted(mock_emit, ApplicationStale)[0]
        assert event.days_since_applied in (44, 45), (
            "days_since_applied was %r - the field is dead again"
            % event.days_since_applied
        )

    @pytest.mark.asyncio
    async def test_scheduled_stale_check_opts_in(self):
        """The 6-hourly task is the one path that SHOULD notify him."""
        from naukri_server.scheduler_tasks import _task_stale_check

        with patch("naukri_server.services.application_service.get_stale_applications",
                   new_callable=AsyncMock, return_value={"status": "success"}) as mock:
            await _task_stale_check()

        assert mock.await_args.kwargs.get("emit_events") is True


class TestApplicationStaleSubscriberDedupe:

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_suppressed_when_one_is_already_pending(self, mock_has, mock_store):
        """RED before the fix: the subscriber stored unconditionally, which is
        how the legitimate 6h task alone banked 17 duplicates per run."""
        from naukri_server.subscribers import _on_application_stale
        mock_has.return_value = True

        await _on_application_stale(
            ApplicationStale(job_id="J1", company="Acme", follow_up_priority=65))

        mock_store.assert_not_called()
        mock_has.assert_awaited_once_with("ApplicationStale", "J1")

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_CONTROL_stores_when_nothing_is_pending(self, mock_has, mock_store):
        """CONTROL. A dedupe that suppresses everything is not a dedupe."""
        from naukri_server.subscribers import _on_application_stale
        mock_has.return_value = False

        await _on_application_stale(
            ApplicationStale(job_id="J1", company="Acme", follow_up_priority=65))

        mock_store.assert_awaited_once()
        assert mock_store.await_args.args[0]["event_type"] == "ApplicationStale"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_CONTROL_dedupe_is_per_job(self, mock_has, mock_store):
        """CONTROL. Suppressing on ANY pending row would silence 16 real
        applications the moment the 17th banked one."""
        from naukri_server.subscribers import _on_application_stale
        mock_has.side_effect = lambda et, jid: jid == "J1"

        await _on_application_stale(ApplicationStale(job_id="J1", follow_up_priority=65))
        await _on_application_stale(ApplicationStale(job_id="J2", follow_up_priority=65))

        assert mock_store.await_count == 1
        assert mock_store.await_args.args[0]["metadata"]["job_id"] == "J2"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_dedupe_fails_open(self, mock_has, mock_store):
        """A broken predicate degrades to the old noise, which is recoverable.
        Dropping his follow-ups is not."""
        from naukri_server.subscribers import _on_application_stale
        mock_has.side_effect = RuntimeError("db gone")

        await _on_application_stale(ApplicationStale(job_id="J1", follow_up_priority=65))

        mock_store.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("naukri_server.workflows.stale_follow_up_workflow", new_callable=AsyncMock)
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_suppression_also_stops_the_follow_up_workflow(
            self, mock_has, mock_store, mock_wf):
        """The second-order blast radius. At priority >=70 the subscriber runs a
        saga that banks a SECOND notification; re-running it per read is the
        same duplication one layer down."""
        from naukri_server.subscribers import _on_application_stale
        mock_has.return_value = True

        await _on_application_stale(ApplicationStale(job_id="J1", follow_up_priority=90))

        mock_wf.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.workflows.stale_follow_up_workflow", new_callable=AsyncMock)
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_CONTROL_workflow_still_runs_when_not_suppressed(
            self, mock_has, mock_store, mock_wf):
        """CONTROL. Proves the previous test is measuring suppression and not a
        workflow that never runs at all."""
        from naukri_server.subscribers import _on_application_stale
        mock_has.return_value = False

        await _on_application_stale(ApplicationStale(job_id="J1", follow_up_priority=90))

        mock_wf.assert_awaited_once()


# =====================================================================
# 2. SavedJobExpiring
# =====================================================================

def _saved_row(job_id="SJ-1", days_old=28):
    saved_at = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return {"job_id": job_id, "title": "SDE", "company": "Acme", "saved_at": saved_at}


class TestSavedJobExpiringReadIsPure:

    @pytest.mark.asyncio
    async def test_read_does_not_emit(self):
        """RED before the fix: listing saved jobs minted a high-priority row."""
        from naukri_server.services.saved_jobs_service import list_saved_jobs

        with patch("naukri_server.database.list_saved_jobs",
                   new_callable=AsyncMock, return_value=([_saved_row()], 1)), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            result = await list_saved_jobs()

        assert result["count"] == 1
        assert _emitted(mock_emit, SavedJobExpiring) == []

    @pytest.mark.asyncio
    async def test_CONTROL_opting_in_still_emits(self):
        from naukri_server.services.saved_jobs_service import list_saved_jobs

        with patch("naukri_server.database.list_saved_jobs",
                   new_callable=AsyncMock, return_value=([_saved_row()], 1)), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            await list_saved_jobs(emit_events=True)

        events = _emitted(mock_emit, SavedJobExpiring)
        assert len(events) == 1
        assert events[0].job_id == "SJ-1"

    @pytest.mark.asyncio
    async def test_CONTROL_fresh_saved_job_never_emits_even_opted_in(self):
        """The 27-day gate must survive the opt-in refactor."""
        from naukri_server.services.saved_jobs_service import list_saved_jobs

        with patch("naukri_server.database.list_saved_jobs",
                   new_callable=AsyncMock, return_value=([_saved_row(days_old=3)], 1)), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            await list_saved_jobs(emit_events=True)

        assert _emitted(mock_emit, SavedJobExpiring) == []

    @pytest.mark.asyncio
    async def test_scheduled_saved_sync_opts_in(self):
        """SavedJobExpiring had NO scheduled producer before this pass - making
        it opt-in without wiring one would have killed the signal outright."""
        from naukri_server.scheduler_tasks import _task_sync_saved_jobs

        with patch("naukri_server.tools.sync._sync_saved_jobs",
                   new_callable=AsyncMock, return_value={"status": "success"}), \
             patch("naukri_server.services.saved_jobs_service.list_saved_jobs",
                   new_callable=AsyncMock, return_value={"count": 2}) as mock_list:
            result = await _task_sync_saved_jobs()

        assert mock_list.await_args.kwargs.get("emit_events") is True
        assert result["expiry_checked"] == 2

    @pytest.mark.asyncio
    async def test_expiry_sweep_failure_does_not_fail_the_sync(self):
        """The sync result is the task's real payload."""
        from naukri_server.scheduler_tasks import _task_sync_saved_jobs

        with patch("naukri_server.tools.sync._sync_saved_jobs",
                   new_callable=AsyncMock, return_value={"status": "success"}), \
             patch("naukri_server.services.saved_jobs_service.list_saved_jobs",
                   new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            result = await _task_sync_saved_jobs()

        assert result["status"] == "success"
        assert result["expiry_checked"] == 0


class TestSavedJobExpiringSubscriberDedupe:

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_suppressed_when_one_is_already_pending(self, mock_has, mock_store):
        from naukri_server.subscribers import _on_saved_job_expiring
        mock_has.return_value = True

        await _on_saved_job_expiring(SavedJobExpiring(job_id="SJ-1", expires_in_days=2))

        mock_store.assert_not_called()
        mock_has.assert_awaited_once_with("SavedJobExpiring", "SJ-1")

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_CONTROL_stores_when_nothing_is_pending(self, mock_has, mock_store):
        from naukri_server.subscribers import _on_saved_job_expiring
        mock_has.return_value = False

        await _on_saved_job_expiring(SavedJobExpiring(job_id="SJ-1", expires_in_days=2))

        mock_store.assert_awaited_once()
        assert mock_store.await_args.args[0]["event_type"] == "SavedJobExpiring"


# =====================================================================
# 3. ProfileScoreChanged - a "Changed" event that never checked for a change
# =====================================================================

class TestProfileScoreChangeDetection:

    @staticmethod
    def _audit_ctx(completeness=72, previous="72"):
        """Patch the two I/O calls _audit_profile makes, plus the last score."""
        return (
            patch("naukri_server.services.profile_service._get_profile",
                  new_callable=AsyncMock,
                  return_value={"status": "success", "name": "S"}),
            patch("naukri_server.tools.assessments._get_profile_completeness",
                  new_callable=AsyncMock,
                  return_value={"status": "success",
                                "completeness_percent": completeness}),
            patch("naukri_server.database.last_event_value",
                  new_callable=AsyncMock, return_value=previous),
            patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock),
        )

    @pytest.mark.asyncio
    async def test_unchanged_score_emits_nothing(self):
        """RED before the fix: every audit emitted, with old_score hardcoded 0."""
        from naukri_server.services.profile_service import _audit_profile
        p1, p2, p3, p4 = self._audit_ctx(completeness=72, previous="72")

        with p1, p2, p3, p4 as mock_emit:
            result = await _audit_profile()

        assert result["status"] == "success"
        assert _emitted(mock_emit, ProfileScoreChanged) == []

    @pytest.mark.asyncio
    async def test_CONTROL_a_real_change_still_emits_with_a_true_old_score(self):
        """CONTROL. Without it, deleting the emit would pass - and it pins the
        old_score lie: it was hardcoded to 0 on every event ever emitted."""
        from naukri_server.services.profile_service import _audit_profile
        p1, p2, p3, p4 = self._audit_ctx(completeness=81, previous="72")

        with p1, p2, p3, p4 as mock_emit:
            await _audit_profile()

        events = _emitted(mock_emit, ProfileScoreChanged)
        assert len(events) == 1
        assert (events[0].old_score, events[0].new_score) == (72, 81)

    @pytest.mark.asyncio
    async def test_first_ever_observation_emits_from_zero(self):
        """No prior row - never logged, or pruned by the retention sweep. One
        event, then quiet."""
        from naukri_server.services.profile_service import _audit_profile
        p1, p2, p3, p4 = self._audit_ctx(completeness=72, previous=None)

        with p1, p2, p3, p4 as mock_emit:
            await _audit_profile()

        events = _emitted(mock_emit, ProfileScoreChanged)
        assert len(events) == 1
        assert (events[0].old_score, events[0].new_score) == (0, 72)

    @pytest.mark.asyncio
    async def test_unparseable_previous_value_does_not_crash_the_audit(self):
        from naukri_server.services.profile_service import _audit_profile
        p1, p2, p3, p4 = self._audit_ctx(completeness=72, previous="not-a-number")

        with p1, p2, p3, p4 as mock_emit:
            result = await _audit_profile()

        assert result["status"] == "success"
        assert len(_emitted(mock_emit, ProfileScoreChanged)) == 1


class TestLastEventValue:
    """The change detector's only input."""

    @pytest.mark.asyncio
    async def test_reads_the_most_recent_row_by_id(self):
        from naukri_server import database

        rows = {"data": '{"old_score": "68", "new_score": "81"}'}
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=rows)
        db = AsyncMock()
        db.execute = AsyncMock(return_value=cursor)

        with patch.object(database, "get_db", new_callable=AsyncMock, return_value=db):
            value = await database.last_event_value("ProfileScoreChanged", "new_score")

        assert value == "81"
        sql = db.execute.await_args.args[0]
        assert "ORDER BY id DESC" in sql, (
            "must order by insert id, not by a timestamp string"
        )

    @pytest.mark.asyncio
    async def test_CONTROL_missing_row_and_bad_json_both_read_as_no_prior_value(self):
        from naukri_server import database

        for payload in (None, {"data": None}, {"data": "{not json"},
                        {"data": '{"other": 1}'}):
            cursor = AsyncMock()
            cursor.fetchone = AsyncMock(return_value=payload)
            db = AsyncMock()
            db.execute = AsyncMock(return_value=cursor)
            with patch.object(database, "get_db", new_callable=AsyncMock, return_value=db):
                assert await database.last_event_value("X", "new_score") is None, payload


# =====================================================================
# 4. RecruiterEngaged - the corrected finding
# =====================================================================

class TestRecruiterActivityReadIsPure:

    @staticmethod
    def _api_response(activity="CONTACTED"):
        return {
            "successResponse": {
                "jobseekerActivityList": [{
                    "activity": activity,
                    "activityDate": "2026-08-21T10:00:00",
                    "name": "R Kumar",
                    "companyName": "Acme",
                    "metaData": '{"jobId":"123"}',
                }],
                "count": 1,
                "activityBucketCount": {},
            }
        }

    @pytest.mark.asyncio
    async def test_read_does_not_emit_even_with_contacted_activity(self):
        """GREEN BEFORE AND AFTER, and that IS the finding.

        The emit was there in source but unreachable, so this assertion proves
        nothing about the removal on its own - stated plainly rather than
        counted as a red-proven guard. What actually pins the removal is
        test_emit_census_matches_the_frozen_allowlist, which is RED before the
        fix because the census then still contains
        performance_service.py::get_recruiter_activity -> RecruiterEngaged.

        This test's job is the OTHER direction: once the "activity" key fix
        below makes the CONTACTED branch reachable for the first time, the read
        must still emit nothing. Without the removal, fixing the key would have
        lit up a storm rather than stopped one.
        """
        from naukri_server.services.performance_service import get_recruiter_activity

        with patch("naukri_server.interfaces.api_client.post",
                   new_callable=AsyncMock, return_value=self._api_response()), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            result = await get_recruiter_activity()

        assert result["status"] == "success"
        assert _emitted(mock_emit, RecruiterEngaged) == []

    @pytest.mark.asyncio
    async def test_action_field_is_populated_from_the_key_the_api_returns(self):
        """RED before the fix. The parser read "activityType"/"action"; the API
        returns "activity" (probing/analytics-report.md L406), so this field was
        "" on every activity of every response - and the CONTACTED/DOWNLOADED
        branch that gated the emit could never be taken. 76 CONTACTED and 4
        DOWNLOADED actions on his account, zero RecruiterEngaged rows ever
        logged."""
        from naukri_server.services.performance_service import get_recruiter_activity

        with patch("naukri_server.interfaces.api_client.post",
                   new_callable=AsyncMock, return_value=self._api_response("DOWNLOADED")):
            result = await get_recruiter_activity()

        assert result["activities"][0]["action"] == "DOWNLOADED", (
            "action was %r - the field is dead again"
            % result["activities"][0]["action"]
        )

    @pytest.mark.asyncio
    async def test_CONTROL_the_write_path_still_emits_recruiter_engaged(self):
        """CONTROL. RecruiterEngaged must not be dead - marking a message
        interested is a genuine write and keeps its event."""
        from naukri_server.tools.inbox import _mark_interested

        with patch("naukri_server.interfaces.api_client.post",
                   new_callable=AsyncMock, return_value={}), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            await _mark_interested("M1", "C1", True)

        events = _emitted(mock_emit, RecruiterEngaged)
        assert len(events) == 1
        assert events[0].action == "MARKED_INTERESTED"


# =====================================================================
# 5. Retention - a policy that never ran
# =====================================================================

class TestRetentionSweepIsWired:

    def test_the_task_is_registered_with_the_scheduler(self):
        """`cleanup_old_records` was defined and called from nowhere. A
        retention policy that never runs reads as protection that does not
        exist."""
        from naukri_server.framework.registry import registry
        import naukri_server.scheduler_tasks as st

        names = {it.metadata.get("name") for it in registry.by_kind("scheduled_task")
                 if it.fn.__module__ == st.__name__}
        assert "retention_sweep" in names, sorted(names)

    def test_retention_sweep_does_not_catch_up_on_startup(self):
        """It DELETES rows, and catch_up is reserved for tasks that do not."""
        from naukri_server.framework.registry import registry
        import naukri_server.scheduler_tasks as st

        item = next(it for it in registry.by_kind("scheduled_task")
                    if it.metadata.get("name") == "retention_sweep"
                    and it.fn.__module__ == st.__name__)
        assert item.metadata.get("catch_up", False) is False

    @pytest.mark.asyncio
    async def test_it_calls_cleanup_with_the_configured_horizon(self):
        from naukri_server.scheduler_tasks import _task_retention_sweep

        with patch("naukri_server.policy.setting", return_value=30), \
             patch("naukri_server.database.cleanup_old_records",
                   new_callable=AsyncMock,
                   return_value={"event_log": 5, "scheduled_runs": 2,
                                 "agent_runs": 0, "agent_decisions": 0}) as mock_cleanup:
            result = await _task_retention_sweep()

        mock_cleanup.assert_awaited_once_with(days=30)
        assert result["retention_days"] == 30
        assert result["deleted"]["event_log"] == 5

    @pytest.mark.asyncio
    async def test_CONTROL_the_default_horizon_is_ninety_days(self):
        """CONTROL. Proves the previous test measured the config being read and
        not a hardcoded 30."""
        from naukri_server.scheduler_tasks import _task_retention_sweep

        with patch("naukri_server.policy.setting",
                   side_effect=lambda key, default: default), \
             patch("naukri_server.database.cleanup_old_records",
                   new_callable=AsyncMock, return_value={}) as mock_cleanup:
            result = await _task_retention_sweep()

        mock_cleanup.assert_awaited_once_with(days=90)
        assert result["retention_days"] == 90
