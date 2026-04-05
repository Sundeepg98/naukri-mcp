"""Event subscribers — reactive side effects triggered by domain events.

Registered at server startup via import in __init__.py.
All handlers are async, fire-and-forget, and isolated (exceptions don't propagate).
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
)

logger = logging.getLogger(__name__)


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


async def _on_application_stale(event: ApplicationStale):
    """Store notification + trigger follow-up workflow for high-priority stale apps."""
    try:
        from naukri_server.database import store_notification
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


async def _on_reminder_due(event: ReminderDue):
    """Store notification for due reminders."""
    try:
        from naukri_server.database import store_notification
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


async def _on_saved_job_expiring(event: SavedJobExpiring):
    """Store high-priority notification when a saved job is about to expire."""
    try:
        from naukri_server.database import store_notification
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


# ---------------------------------------------------------------------------
# Register all subscribers with the global EventBus
# ---------------------------------------------------------------------------

def register_all():
    """Register all subscribers. Called once at server startup."""
    event_bus.subscribe(ApplicationSubmitted, _on_application_submitted)
    event_bus.subscribe(ApplicationStatusChanged, _on_status_change)
    event_bus.subscribe(ApplicationStale, _on_application_stale)
    event_bus.subscribe(ApplicationInterviewScheduled, _on_interview_scheduled)
    event_bus.subscribe(SyncCompleted, _on_sync_completed)
    event_bus.subscribe(RecruiterEngaged, _on_recruiter_engaged)
    event_bus.subscribe(ReminderDue, _on_reminder_due)
    event_bus.subscribe(ProfileScoreChanged, _on_profile_score_changed)
    event_bus.subscribe(SavedJobExpiring, _on_saved_job_expiring)
    event_bus.subscribe(SavedJobAdded, _on_saved_job_added)
    event_bus.subscribe(SavedJobRemoved, _on_saved_job_removed)
    event_bus.subscribe(ReminderSet, _on_reminder_set)
    event_bus.subscribe(BrowserCrashed, _on_browser_crashed)
    event_bus.subscribe(BrowserRecovered, _on_browser_recovered)
    event_bus.subscribe(ProbeStateChanged, _on_probe_state_changed)
    event_bus.subscribe(ProfileUpdated, _on_profile_updated)
    event_bus.subscribe(ProfileBoosted, _on_profile_boosted)
    event_bus.subscribe(AlertCreated, _on_alert_created)
    event_bus.subscribe(AlertUpdated, _on_alert_updated)
    event_bus.subscribe(AlertDeleted, _on_alert_deleted)
    event_bus.subscribe(ApplicationsPurged, _on_applications_purged)
    event_bus.subscribe(ResumeUploaded, _on_resume_uploaded)
    event_bus.subscribe(PhotoUploaded, _on_photo_uploaded)
    event_bus.subscribe(PhotoDeleted, _on_photo_deleted)
    event_bus.subscribe(CachedAnswerUpdated, _on_cached_answer_updated)
    event_bus.subscribe(CachedAnswerDeleted, _on_cached_answer_deleted)
    event_bus.subscribe(SettingsUpdated, _on_settings_updated)
    logger.info("Registered %d reactive subscribers", 27)


# Auto-register on import
register_all()
