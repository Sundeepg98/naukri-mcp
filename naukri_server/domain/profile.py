"""Profile parsing — centralized field extraction with missing-field logging.

Provides a ``_safe_get`` helper that tries multiple API key variants and logs
when all fallbacks are exhausted.  ``ProfileParser`` groups extraction logic
for the most critical profile sections (skills, employment, education, CTC,
communication settings, resdex visibility) so the heavy .get() chains in
profile_service.py are documented, reusable, and observable.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core helper
# ---------------------------------------------------------------------------

def _safe_get(
    data: dict,
    *keys: str,
    default: Any = None,
    field_name: str = "",
    warn: bool = False,
) -> Any:
    """Try *keys* in order on *data*, return first non-None hit.

    Args:
        data: Source dictionary (typically a raw API response fragment).
        *keys: One or more dict keys to try, in priority order.
        default: Fallback when every key misses.
        field_name: Human label for log messages (only used when *warn* is True).
        warn: If True **and** every key misses, emit a WARNING log line.

    Returns:
        The first non-None value found, or *default*.
    """
    for key in keys:
        val = data.get(key)
        if val is not None:
            return val
    if warn and field_name:
        logger.warning(
            "Profile field '%s' missing — tried keys %s", field_name, keys,
        )
    return default


# ---------------------------------------------------------------------------
# Nested sub-dict helper  (e.g.  data["noticePeriod"]["value"])
# ---------------------------------------------------------------------------

def _nested_get(
    data: dict,
    outer_key: str,
    inner_key: str,
    default: Any = None,
    field_name: str = "",
    warn: bool = False,
) -> Any:
    """Get ``data[outer_key][inner_key]`` safely.

    Returns *default* (and optionally warns) if either level is missing.
    """
    outer = data.get(outer_key)
    if isinstance(outer, dict):
        val = outer.get(inner_key)
        if val is not None:
            return val
    if warn and field_name:
        logger.warning(
            "Profile field '%s' missing — path %s.%s",
            field_name, outer_key, inner_key,
        )
    return default


# ---------------------------------------------------------------------------
# ProfileParser  —  section-level extraction with logging
# ---------------------------------------------------------------------------

class ProfileParser:
    """Centralized profile API response parsing with resilience logging.

    Class methods return plain dicts/lists matching the existing return shapes
    in ``profile_service._get_profile`` / ``_get_dashboard``.  This avoids a
    large-scale refactor while centralising field extraction and making
    missing-field diagnostics observable via the ``naukri_server.domain.profile``
    logger.
    """

    # ── Profile API sections ──────────────────────────────────────────────

    @staticmethod
    def extract_skills(data: dict) -> list[dict]:
        """Extract skills-with-experience from raw profile API response.

        Source key: ``data["itskills"]`` (array).

        Returns:
            List of ``{skill, experience_years, experience_months}`` dicts.
            Logs a warning when the list is empty.
        """
        raw = data.get("itskills", [])
        skills: list[dict] = []
        for s in raw:
            exp_time = s.get("experienceTime") or {}
            skills.append({
                "skill": s.get("skill"),
                "experience_years": exp_time.get("year", 0),
                "experience_months": exp_time.get("month", 0),
            })
        if not skills:
            logger.warning(
                "Profile has zero IT-skills — impacts fit scoring and recruiter visibility",
            )
        return skills

    @staticmethod
    def extract_employment(data: dict) -> list[dict]:
        """Extract employment history from raw profile API response.

        Source key: ``data["employments"]`` (array).
        """
        raw = data.get("employments", [])
        employment: list[dict] = []
        for emp in raw:
            employment.append({
                "designation": emp.get("designation"),
                "organization": emp.get("organization"),
                "description": emp.get("description"),
                "start_date": emp.get("startDate"),
                "end_date": emp.get("endDate") or "Present",
            })
        if not employment:
            logger.warning("Profile has zero employment entries")
        return employment

    @staticmethod
    def extract_education(data: dict) -> list[dict]:
        """Extract education from raw profile API response.

        Source key: ``data["educations"]`` (array).
        """
        raw = data.get("educations", [])
        education: list[dict] = []
        for edu in raw:
            education.append({
                "degree": _nested_get(edu, "course", "value"),
                "specialization": _nested_get(edu, "specialisation", "value"),
                "institute": edu.get("institute"),
                "year": edu.get("yearOfCompletion"),
            })
        if not education:
            logger.warning("Profile has zero education entries")
        return education

    @staticmethod
    def extract_ctc(profile: dict) -> dict:
        """Extract current + expected CTC from profile sub-dict.

        Args:
            profile: ``data["profile"][0]`` (the profile sub-object).

        Returns:
            ``{current_ctc, expected_ctc}`` — values may be None.
        """
        current = _safe_get(
            profile, "absoluteCtc",
            field_name="current_ctc", warn=True,
        )
        expected = _safe_get(
            profile, "absoluteExpectedCtc",
            field_name="expected_ctc", warn=True,
        )
        return {"current_ctc": current, "expected_ctc": expected}

    @staticmethod
    def extract_notice_period(profile: dict) -> str | None:
        """Extract notice period value from profile sub-dict."""
        return _nested_get(
            profile, "noticePeriod", "value",
            field_name="notice_period", warn=True,
        )

    @staticmethod
    def extract_resume_headline(profile: dict) -> str | None:
        """Extract resume headline from profile sub-dict."""
        val = _safe_get(
            profile, "resumeHeadline",
            field_name="resume_headline", warn=True,
        )
        return val

    @staticmethod
    def extract_communication_settings(data: dict, profile: dict) -> dict | None:
        """Extract communication/notification preferences.

        Tries three source locations (``communicationSettings``,
        ``communicationPreferences`` on *data*, or ``communicationSettings``
        on the profile sub-dict).

        Returns:
            Dict of non-None settings, or None if nothing found.
        """
        comm = (
            _safe_get(
                data,
                "communicationSettings",
                "communicationPreferences",
                field_name="communication_settings",
            )
            or profile.get("communicationSettings")
        )
        if not comm or not isinstance(comm, dict):
            return None

        settings = {
            "email_notifications": _safe_get(comm, "emailNotifications", "emailAlerts"),
            "sms_notifications": _safe_get(comm, "smsNotifications", "smsAlerts"),
            "push_notifications": comm.get("pushNotifications"),
            "job_alerts": _safe_get(comm, "jobAlerts", "jobAlert"),
            "recruiter_messages": _safe_get(comm, "recruiterMessages", "recruiterMail"),
            "promotional": _safe_get(comm, "promotional", "marketingMail"),
            "whatsapp_notifications": _safe_get(comm, "whatsappNotifications", "whatsappAlerts"),
        }
        settings = {k: v for k, v in settings.items() if v is not None}
        return settings or None

    @staticmethod
    def extract_resdex_visibility(data: dict, additional: dict, profile: dict) -> dict | str | None:
        """Extract resdex / profile-visibility flags.

        Tries four source locations.

        Returns:
            Dict of non-None flags, a plain string, or None.
        """
        resdex = _safe_get(
            data,
            "resdexVisibility",
            "profileVisibility",
            field_name="resdex_visibility",
        ) or additional.get("resdexVisibility") or profile.get("resdexVisibility")

        if resdex and isinstance(resdex, dict):
            vis = {
                "show_profile": _safe_get(resdex, "showProfile", "profileVisible"),
                "show_current_employer": _safe_get(resdex, "showCurrentEmployer", "currentEmployerVisible"),
                "show_email": _safe_get(resdex, "showEmail", "emailVisible"),
                "show_phone": _safe_get(resdex, "showPhone", "mobileVisible"),
                "show_resume": _safe_get(resdex, "showResume", "resumeVisible"),
                "hide_from_companies": _safe_get(resdex, "hideFromCompanies", "blockedCompanies"),
                "search_visible": _safe_get(resdex, "searchVisible", "isSearchable"),
                "active_job_seeker": _safe_get(resdex, "activeJobSeeker", "isActivelySearching"),
            }
            vis = {k: v for k, v in vis.items() if v is not None}
            return vis or None
        elif resdex and isinstance(resdex, str):
            return resdex
        return None

    # ── Dashboard API sections ────────────────────────────────────────────

    @staticmethod
    def extract_dashboard_resume(db: dict) -> dict:
        """Extract resume details from dashboard response.

        Handles both nested ``resumeDetails``/``resume`` dict and flat keys.

        Returns:
            ``{resume_name, resume_last_updated, resume_headline, resume_id}``.
        """
        resume = _safe_get(db, "resumeDetails", "resume") or {}
        if isinstance(resume, dict):
            return {
                "resume_name": _safe_get(resume, "name", "resumeName") or db.get("resumeName"),
                "resume_last_updated": _safe_get(resume, "lastUpdated", "modifiedDate") or db.get("resumeLastUpdated"),
                "resume_headline": resume.get("resumeHeadline") or db.get("resumeHeadline"),
                "resume_id": _safe_get(resume, "resumeId", "id"),
            }
        return {
            "resume_name": db.get("resumeName"),
            "resume_last_updated": db.get("resumeLastUpdated"),
            "resume_headline": db.get("resumeHeadline"),
            "resume_id": None,
        }

    @staticmethod
    def extract_dashboard_profile_completeness(db: dict) -> Any:
        """Extract profile completeness from dashboard.

        Tries ``profileCompletion``, ``completeness``, ``profileCompleteness``.
        """
        return _safe_get(
            db, "profileCompletion", "completeness", "profileCompleteness",
            field_name="profile_completion", warn=True,
        )

    @staticmethod
    def extract_dashboard_search_impressions(db: dict) -> Any:
        """Extract search impressions / appearances from dashboard."""
        return _safe_get(
            db, "searchImpressions", "searchAppearances", "searchCount",
            field_name="search_impressions",
        )
