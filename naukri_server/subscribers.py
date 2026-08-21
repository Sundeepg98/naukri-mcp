"""Event subscribers — reactive side effects triggered by domain events.

Registered at server startup via import in __init__.py.
All handlers are async, fire-and-forget, and isolated (exceptions don't propagate).

Migrated to @subscriber decorator (Track 1: unified registry framework).
Wiring happens in __init__.py via wire_subscribers(event_bus).
"""

import logging
from datetime import datetime, timezone

from naukri_server.events import (
    event_bus, ApplicationSubmitted, ApplicationStatusChanged,
    ApplicationStale, ApplicationInterviewScheduled, SyncCompleted, ReminderDue,
    RecruiterEngaged, ProfileScoreChanged,
    SavedJobExpiring, SavedJobAdded, SavedJobRemoved, ReminderSet,
    BrowserCrashed, BrowserRecovered, ProbeStateChanged,
    ProfileUpdated, ProfileBoosted, AlertCreated, AlertUpdated, AlertDeleted,
    ApplicationsPurged, ResumeUploaded, PhotoUploaded, PhotoDeleted,
    CachedAnswerUpdated, CachedAnswerDeleted, SettingsUpdated,
    InboxMessageRead, InboxInviteAccepted,
    NewEndpointDiscovered,
    AgentCycleStarted, AgentCycleCompleted, AgentJobApplied, AgentJobSkipped,
    AgentObserveCompleted, AgentDecideCompleted, AgentActCompleted,
    ScheduledTaskCompleted,
)
from naukri_server.framework.registry import subscriber

# Import the healing package so router.py's @subscriber(EndpointDriftDetected)
# decorator runs and is present in the registry BEFORE register_all() (at the
# bottom of this module) calls wire_subscribers(event_bus). Without this import
# the drift→heal subscriber would never be wired and EndpointDriftDetected would
# have zero subscribers.
import naukri_server.healing as _healing  # noqa: F401, E402

logger = logging.getLogger(__name__)


@subscriber(ApplicationSubmitted)
async def _on_application_submitted(event: ApplicationSubmitted):
    """Auto-set 7-day follow-up reminder on successful apply."""
    if not event.job_id:
        return
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ApplicationSubmitted",
            "title": f"Applied to {event.title or 'job'} at {event.company or 'company'}",
            "body": f"Application submitted for job {event.job_id}. Follow up in 7 days.",
            "priority": "medium",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": event.job_id, "company": event.company},
        })
        logger.info("Notification stored for ApplicationSubmitted: %s", event.job_id)
    except Exception as e:
        logger.warning("Subscriber _on_application_submitted failed: %s", e)


@subscriber(ApplicationStatusChanged)
async def _on_status_change(event: ApplicationStatusChanged):
    """Store notification on meaningful status changes."""
    if not event.job_id or not event.new_status:
        return
    try:
        from naukri_server.domain.application import StatusTransition
        transition = StatusTransition(old_status=event.old_status, new_status=event.new_status)

        priority = "high" if transition.is_positive else "medium"
        title = f"{event.company or 'Company'}: {event.old_status} → {event.new_status}"

        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ApplicationStatusChanged",
            "title": title,
            "body": f"Application for {event.title or event.job_id} changed status",
            "priority": priority,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "job_id": event.job_id,
                "old_status": event.old_status,
                "new_status": event.new_status,
                "is_positive": transition.is_positive,
            },
        })
    except Exception as e:
        logger.warning("Subscriber _on_status_change failed: %s", e)


@subscriber(ApplicationStale)
async def _on_application_stale(event: ApplicationStale):
    """Store notification + trigger follow-up workflow - at most one live one per job.

    An application stays stale until he acts on it, and the 6-hourly stale_check
    re-emits every stale application on every run, so an unconditional store
    banks a duplicate per application per run. That is how 17 applications
    became 85 notifications.

    The early return also suppresses the stale_follow_up_workflow below, which
    is deliberate: re-drafting the same follow-up and banking a second
    StaleFollowUp notification is the same duplication one layer down.

    Same predicate and same reasoning as _on_reminder_due - see its docstring
    for why an existing-undelivered-row check beats a last_notified_at column
    plus an interval for a condition that never clears on its own.
    """
    try:
        from naukri_server.database import store_notification, has_pending_notification

        # Fail OPEN: the notification is the payload, the dedupe is an
        # optimisation over it. A broken predicate degrades to the old noisy
        # behaviour, which is recoverable; dropping his follow-ups is not.
        try:
            if event.job_id and await has_pending_notification("ApplicationStale", event.job_id):
                logger.debug("ApplicationStale for %s already pending - suppressed", event.job_id)
                return
        except Exception as e:
            logger.warning("ApplicationStale dedupe check failed, notifying anyway: %s", e)

        await store_notification({
            "event_type": "ApplicationStale",
            "title": f"Stale: {event.company or 'Company'} - needs follow-up",
            "body": f"Application {event.job_id} has been stale. Consider following up or moving on.",
            "priority": "high" if event.follow_up_priority >= 70 else "medium",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": event.job_id, "company": event.company},
        })

        # Trigger follow-up workflow for high-priority apps
        if event.follow_up_priority >= 70:
            try:
                from naukri_server.workflows import stale_follow_up_workflow
                await stale_follow_up_workflow(
                    job_id=event.job_id,
                    company=event.company,
                )
                logger.info("Stale follow-up workflow completed for %s", event.job_id)
            except Exception as wf_err:
                logger.warning("Stale follow-up workflow failed (non-fatal): %s", wf_err)
    except Exception as e:
        logger.warning("Subscriber _on_application_stale failed: %s", e)


@subscriber(ApplicationInterviewScheduled)
async def _on_interview_scheduled(event: ApplicationInterviewScheduled):
    """Store notification + trigger interview lifecycle workflow."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ApplicationInterviewScheduled",
            "title": f"Interview: {event.round_type} at {event.company or 'company'}",
            "body": f"Interview round scheduled for job {event.job_id}. Use naukri_interview_prep to prepare.",
            "priority": "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": event.job_id, "round_type": event.round_type, "date": event.date},
        })

        # Trigger interview lifecycle workflow (fire-and-forget)
        try:
            from naukri_server.workflows import interview_lifecycle_workflow
            await interview_lifecycle_workflow(
                job_id=event.job_id,
                company=event.company,
                round_type=event.round_type,
            )
            logger.info("Interview lifecycle workflow completed for %s", event.job_id)
        except Exception as wf_err:
            logger.warning("Interview workflow failed (non-fatal): %s", wf_err)
    except Exception as e:
        logger.warning("Subscriber _on_interview_scheduled failed: %s", e)


@subscriber(SyncCompleted)
async def _on_sync_completed(event: SyncCompleted):
    """Store notification if sync found changes."""
    if event.status_changes_count == 0 and event.new_added == 0:
        return  # No changes, no notification
    try:
        from naukri_server.database import store_notification
        parts = []
        if event.new_added:
            parts.append(f"{event.new_added} new")
        if event.status_changes_count:
            parts.append(f"{event.status_changes_count} status changes")
        await store_notification({
            "event_type": "SyncCompleted",
            "title": f"Sync: {', '.join(parts)}",
            "body": f"Synced {event.entity}: {event.new_added} new, {event.updated} updated",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.warning("Subscriber _on_sync_completed failed: %s", e)


@subscriber(RecruiterEngaged)
async def _on_recruiter_engaged(event: RecruiterEngaged):
    """Store notification when recruiter contacts you."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "RecruiterEngaged",
            "title": f"Recruiter from {event.company or 'a company'} engaged",
            "body": "A recruiter viewed/contacted you. Check recruiter activity for details.",
            "priority": "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": event.job_id, "company": event.company},
        })
    except Exception as e:
        logger.warning("Subscriber _on_recruiter_engaged failed: %s", e)


@subscriber(ReminderDue)
async def _on_reminder_due(event: ReminderDue):
    """Store notification for due reminders - at most one live one per job.

    A reminder stays due forever until he acts on it, and the hourly
    reminder_check re-emits every past-due reminder on every run, so an
    unconditional store banks a duplicate per reminder per hour. That is how
    50 reminders became 1,084 notifications.

    DEDUPE BY EXISTING UNDELIVERED ROW, not by a `last_notified_at` column on
    `reminders` + an interval. The interval design is still unbounded for a
    permanently-overdue reminder: it only sets the storm's RATE (50/day at 24h),
    and every one of his 50 reminders is months past due, which is precisely
    the case it handles worst. This predicate is bounded by construction - one
    undelivered ReminderDue per job_id, ever - needs no schema migration, and
    re-arms itself the moment the brief delivers the notification, so he can
    never be shown the same reminder twice without having seen it once.
    """
    try:
        from naukri_server.database import store_notification, has_pending_notification

        # Fail OPEN: the notification is the payload, the dedupe is an
        # optimisation over it. A broken predicate degrades to the old noisy
        # behaviour, which is recoverable; dropping his reminders is not.
        try:
            if event.job_id and await has_pending_notification("ReminderDue", event.job_id):
                logger.debug("ReminderDue for %s already pending - suppressed", event.job_id)
                return
        except Exception as e:
            logger.warning("ReminderDue dedupe check failed, notifying anyway: %s", e)

        await store_notification({
            "event_type": "ReminderDue",
            "title": f"Reminder due: {event.company or 'Follow up'}",
            "body": event.note or f"Follow up on job {event.job_id}",
            "priority": "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": event.job_id},
        })
    except Exception as e:
        logger.warning("Subscriber _on_reminder_due failed: %s", e)


@subscriber(ProfileScoreChanged)
async def _on_profile_score_changed(event: ProfileScoreChanged):
    """Store notification when profile completeness score is assessed."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ProfileScoreChanged",
            "title": f"Profile score: {event.new_score}%",
            "body": "Your profile completeness score was assessed.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.warning("Subscriber _on_profile_score_changed failed: %s", e)


@subscriber(SavedJobExpiring)
async def _on_saved_job_expiring(event: SavedJobExpiring):
    """Store high-priority notification when a saved job is about to expire.

    Deduped like _on_reminder_due and _on_application_stale: a saved job stays
    past 27 days until he applies to it or removes it, so every producer run
    re-emits it and an unconditional store banks a duplicate each time.
    """
    try:
        from naukri_server.database import store_notification, has_pending_notification

        # Fail OPEN - see _on_reminder_due.
        try:
            if event.job_id and await has_pending_notification("SavedJobExpiring", event.job_id):
                logger.debug("SavedJobExpiring for %s already pending - suppressed", event.job_id)
                return
        except Exception as e:
            logger.warning("SavedJobExpiring dedupe check failed, notifying anyway: %s", e)

        await store_notification({
            "event_type": "SavedJobExpiring",
            "title": f"Saved job expiring: {event.title or 'job'} at {event.company or 'company'}",
            "body": f"Saved job {event.job_id} expires in {event.expires_in_days} days. Apply or remove it.",
            "priority": "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": event.job_id, "expires_in_days": event.expires_in_days},
        })
    except Exception as e:
        logger.warning("Subscriber _on_saved_job_expiring failed: %s", e)


@subscriber(SavedJobAdded)
async def _on_saved_job_added(event: SavedJobAdded):
    """Store low-priority notification when a job is saved."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "SavedJobAdded",
            "title": f"Job saved: {event.title or 'job'} at {event.company or 'company'}",
            "body": f"Job {event.job_id} added to saved jobs.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": event.job_id},
        })
    except Exception as e:
        logger.warning("Subscriber _on_saved_job_added failed: %s", e)


@subscriber(SavedJobRemoved)
async def _on_saved_job_removed(event: SavedJobRemoved):
    """Store low-priority notification when a saved job is removed."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "SavedJobRemoved",
            "title": f"Job unsaved: {event.job_id}",
            "body": f"Job {event.job_id} removed from saved jobs.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": event.job_id},
        })
    except Exception as e:
        logger.warning("Subscriber _on_saved_job_removed failed: %s", e)


@subscriber(ReminderSet)
async def _on_reminder_set(event: ReminderSet):
    """Store low-priority notification when a reminder is created."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ReminderSet",
            "title": f"Reminder set: {event.company or 'Follow up'} in {event.days} days",
            "body": f"Reminder for job {event.job_id} scheduled at {event.remind_at}.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": event.job_id, "remind_at": event.remind_at},
        })
    except Exception as e:
        logger.warning("Subscriber _on_reminder_set failed: %s", e)


@subscriber(BrowserCrashed)
async def _on_browser_crashed(event: BrowserCrashed):
    """Store notification when browser crashes."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "BrowserCrashed",
            "title": f"Browser crashed ({event.consecutive_failures} failures)",
            "body": f"Reason: {event.reason}. Restart attempt #{event.crash_count + 1}.",
            "priority": "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"crash_count": event.crash_count, "consecutive": event.consecutive_failures},
        })
    except Exception as e:
        logger.warning("Subscriber _on_browser_crashed failed: %s", e)


@subscriber(BrowserRecovered)
async def _on_browser_recovered(event: BrowserRecovered):
    """Store notification on successful recovery."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "BrowserRecovered",
            "title": f"Browser recovered (downtime: {event.downtime_seconds}s)",
            "body": f"Restart #{event.restart_count} successful.",
            "priority": "medium",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.warning("Subscriber _on_browser_recovered failed: %s", e)


@subscriber(ProbeStateChanged)
async def _on_probe_state_changed(event: ProbeStateChanged):
    """Store notification when critical probe changes to unhealthy."""
    if event.criticality != "critical" or event.new_status != "unhealthy":
        return  # Only notify on critical failures
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ProbeStateChanged",
            "title": f"ALERT: {event.probe_name} is {event.new_status}",
            "body": event.message,
            "priority": "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"probe": event.probe_name, "old": event.old_status, "new": event.new_status},
        })
    except Exception as e:
        logger.warning("Subscriber _on_probe_state_changed failed: %s", e)


@subscriber(ProfileUpdated)
async def _on_profile_updated(event: ProfileUpdated):
    """Store notification when profile fields are updated."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ProfileUpdated",
            "title": f"Profile updated: {event.fields}",
            "body": f"Profile fields updated via {event.method}.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"fields": event.fields, "method": event.method},
        })
    except Exception as e:
        logger.warning("Subscriber _on_profile_updated failed: %s", e)


@subscriber(ProfileBoosted)
async def _on_profile_boosted(event: ProfileBoosted):
    """Store notification when profile is boosted for visibility."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ProfileBoosted",
            "title": "Profile visibility refreshed",
            "body": f"Profile boosted via {event.method}. You appear as 'recently active'.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"method": event.method},
        })
    except Exception as e:
        logger.warning("Subscriber _on_profile_boosted failed: %s", e)


@subscriber(AlertCreated)
async def _on_alert_created(event: AlertCreated):
    """Store notification when a job alert is created."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "AlertCreated",
            "title": f"Alert created: {event.alert_name}",
            "body": f"Job alert '{event.alert_name}' created for '{event.keywords}'.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"alert_name": event.alert_name, "keywords": event.keywords},
        })
    except Exception as e:
        logger.warning("Subscriber _on_alert_created failed: %s", e)


@subscriber(AlertUpdated)
async def _on_alert_updated(event: AlertUpdated):
    """Store notification when a job alert is modified."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "AlertUpdated",
            "title": f"Alert updated: {event.alert_name}",
            "body": f"Updated fields: {event.updated_fields}.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"alert_name": event.alert_name, "updated_fields": event.updated_fields},
        })
    except Exception as e:
        logger.warning("Subscriber _on_alert_updated failed: %s", e)


@subscriber(AlertDeleted)
async def _on_alert_deleted(event: AlertDeleted):
    """Store notification when a job alert is deleted."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "AlertDeleted",
            "title": f"Alert deleted: {event.alert_name}",
            "body": f"Job alert '{event.alert_name}' (id={event.alert_id}) deleted.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"alert_id": event.alert_id, "alert_name": event.alert_name},
        })
    except Exception as e:
        logger.warning("Subscriber _on_alert_deleted failed: %s", e)


@subscriber(ApplicationsPurged)
async def _on_applications_purged(event: ApplicationsPurged):
    """Store notification when old applications are purged."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ApplicationsPurged",
            "title": f"Purged {event.purged_count} old applications",
            "body": f"Applications before {event.before_date} have been deleted.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"purged_count": event.purged_count, "before_date": event.before_date},
        })
    except Exception as e:
        logger.warning("Subscriber _on_applications_purged failed: %s", e)


@subscriber(ResumeUploaded)
async def _on_resume_uploaded(event: ResumeUploaded):
    """Store notification when a resume is uploaded."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ResumeUploaded",
            "title": f"Resume uploaded: {event.file_name}",
            "body": f"Resume '{event.file_name}' ({event.size_mb}MB) uploaded to profile.",
            "priority": "medium",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"file_name": event.file_name, "size_mb": event.size_mb},
        })
    except Exception as e:
        logger.warning("Subscriber _on_resume_uploaded failed: %s", e)


@subscriber(PhotoUploaded)
async def _on_photo_uploaded(event: PhotoUploaded):
    """Store notification when a photo is uploaded."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "PhotoUploaded",
            "title": f"Photo uploaded: {event.file_name}",
            "body": f"Profile photo '{event.file_name}' uploaded.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"file_name": event.file_name},
        })
    except Exception as e:
        logger.warning("Subscriber _on_photo_uploaded failed: %s", e)


@subscriber(PhotoDeleted)
async def _on_photo_deleted(event: PhotoDeleted):
    """Store notification when a photo is deleted."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "PhotoDeleted",
            "title": "Profile photo deleted",
            "body": "Profile photo has been removed.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.warning("Subscriber _on_photo_deleted failed: %s", e)


@subscriber(CachedAnswerUpdated)
async def _on_cached_answer_updated(event: CachedAnswerUpdated):
    """Store notification when a cached screening answer is updated."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "CachedAnswerUpdated",
            "title": f"Cached answer updated: {event.key[:50]}",
            "body": f"Screening question answer for '{event.key}' was modified.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"key": event.key},
        })
    except Exception as e:
        logger.warning("Subscriber _on_cached_answer_updated failed: %s", e)


@subscriber(CachedAnswerDeleted)
async def _on_cached_answer_deleted(event: CachedAnswerDeleted):
    """Store notification when a cached screening answer is deleted."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "CachedAnswerDeleted",
            "title": f"Cached answer deleted: {event.key[:50]}",
            "body": f"Screening question answer for '{event.key}' was removed.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"key": event.key},
        })
    except Exception as e:
        logger.warning("Subscriber _on_cached_answer_deleted failed: %s", e)


@subscriber(SettingsUpdated)
async def _on_settings_updated(event: SettingsUpdated):
    """Store notification when account settings change."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "SettingsUpdated",
            "title": f"Settings updated: {event.updated_fields}",
            "body": f"Account settings changed: {event.updated_fields}.",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"updated_fields": event.updated_fields},
        })
    except Exception as e:
        logger.warning("Subscriber _on_settings_updated failed: %s", e)


@subscriber(NewEndpointDiscovered)
async def _on_new_endpoint_discovered(event: NewEndpointDiscovered):
    """Store notification when a new API endpoint is discovered."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "NewEndpointDiscovered",
            "title": f"New API endpoint: {event.url[:80]}",
            "body": f"Discovered {event.method} {event.url} from {event.page_source}. Review and add to config.py.",
            "priority": "medium",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"url": event.url, "method": event.method, "page_source": event.page_source},
        })
    except Exception as e:
        logger.warning("Subscriber _on_new_endpoint_discovered failed: %s", e)


# ---------------------------------------------------------------------------
# Agent & scheduler event handlers
# ---------------------------------------------------------------------------

@subscriber(AgentCycleCompleted)
async def _on_agent_cycle_completed(event):
    """Store notification for completed agent cycles."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "AgentCycleCompleted",
            "title": f"Agent cycle: {event.applied_count} applied, {event.skipped_count} skipped",
            "body": f"Cycle {event.cycle_id} completed in {event.duration_ms:.0f}ms — status: {event.status}",
            "priority": "medium" if event.applied_count == 0 else "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.warning("Failed to handle AgentCycleCompleted: %s", e)


@subscriber(AgentJobApplied)
async def _on_agent_job_applied(event):
    """Store notification when agent applies to a job."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "AgentJobApplied",
            "title": f"Agent applied: {event.title} at {event.company}",
            "body": f"Fit score: {event.fit_score} — Cycle: {event.cycle_id}",
            "priority": "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.warning("Failed to handle AgentJobApplied: %s", e)


@subscriber(ScheduledTaskCompleted)
async def _on_scheduled_task_completed(event):
    """Log scheduled task completions for monitoring."""
    if event.status == "failed":
        try:
            from naukri_server.database import store_notification
            await store_notification({
                "event_type": "ScheduledTaskFailed",
                "title": f"Scheduled task failed: {event.task_name}",
                "body": f"Error: {event.error}" if event.error else f"Task {event.task_name} failed after {event.duration_ms:.0f}ms",
                "priority": "high",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.warning("Failed to handle ScheduledTaskCompleted: %s", e)


@subscriber(AgentCycleStarted)
async def _on_agent_cycle_started(event):
    """Log agent cycle start."""
    try:
        logger.info("Agent cycle %s started: mode=%s, %d searches",
                    event.cycle_id, event.mode, event.search_count)
    except Exception as e:
        logger.warning("Failed to handle AgentCycleStarted: %s", e)


@subscriber(AgentJobSkipped)
async def _on_agent_job_skipped(event):
    """Store notification for skipped jobs (aggregated)."""
    try:
        logger.info("Agent skipped job %s at %s: %s (fit: %d)",
                    event.job_id, event.company, event.reason, event.fit_score)
    except Exception as e:
        logger.warning("Failed to handle AgentJobSkipped: %s", e)


@subscriber(AgentObserveCompleted)
async def _on_agent_observe_completed(event):
    """Log agent observe step completion."""
    try:
        logger.info("Agent %s: observe done — %d applied IDs, %d remaining",
                    event.cycle_id, event.applied_ids_count, event.daily_remaining)
    except Exception as e:
        logger.warning("Failed to handle AgentObserveCompleted: %s", e)


@subscriber(AgentDecideCompleted)
async def _on_agent_decide_completed(event):
    """Log agent decide step completion."""
    try:
        logger.info("Agent %s: decide done — %d searches, %d found, %d candidates",
                    event.cycle_id, event.searches_run, event.total_found, event.candidates_count)
    except Exception as e:
        logger.warning("Failed to handle AgentDecideCompleted: %s", e)


@subscriber(AgentActCompleted)
async def _on_agent_act_completed(event):
    """Log agent act step completion."""
    try:
        logger.info("Agent %s: act done — mode=%s, applied=%d",
                    event.cycle_id, event.mode, event.applied_count)
    except Exception as e:
        logger.warning("Failed to handle AgentActCompleted: %s", e)


@subscriber(InboxMessageRead)
async def _on_inbox_message_read(event: InboxMessageRead):
    """Track last-read timestamp for inbox messages.

    Persists a tiny notification entry so the read activity is auditable
    via event_log/notifications. Lazy import + try/except — never crash caller.
    """
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "InboxMessageRead",
            "title": "Inbox message read",
            "body": f"Message {event.message_id} (thread {event.thread_id}) read",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"thread_id": event.thread_id, "message_id": event.message_id},
        })
        logger.debug("Inbox message read: thread=%s msg=%s", event.thread_id, event.message_id)
    except Exception as e:
        logger.warning("Failed to handle InboxMessageRead: %s", e)


@subscriber(InboxInviteAccepted)
async def _on_inbox_invite_accepted(event: InboxInviteAccepted):
    """React to a recruiter NVite acceptance — log + notify.

    Stores a high-priority notification so the user sees the accepted invite
    in the daily brief. Lazy import + try/except — never crash caller.
    """
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "InboxInviteAccepted",
            "title": "Recruiter invite accepted",
            "body": f"Invite {event.invite_id} accepted",
            "priority": "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"invite_id": event.invite_id, "recruiter_id": event.recruiter_id},
        })
        logger.info("Invite accepted: %s", event.invite_id)
    except Exception as e:
        logger.warning("Failed to handle InboxInviteAccepted: %s", e)


# ---------------------------------------------------------------------------
# Register all subscribers with the global EventBus
# ---------------------------------------------------------------------------

def register_all():
    """Register all subscribers. Called once at server startup.

    Replaced 36 hand-written event_bus.subscribe() calls with the unified
    @subscriber decorator framework. This function now wires the registered
    handlers to the event bus.
    """
    from naukri_server.framework.registry import wire_subscribers
    count = wire_subscribers(event_bus)
    logger.info("Registered %d reactive subscribers", count)


# Auto-register on import
register_all()
