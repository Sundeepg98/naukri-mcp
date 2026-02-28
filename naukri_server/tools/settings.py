"""Settings tools — view and update Naukri account settings and blocked companies."""

from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import logger, FORMATTED_SETTINGS_API, SETTINGS_API, BLOCKED_COMPANIES_API

JOB_SEARCH_STATUS = {
    "actively_searching": 1,
    "not_active": 2,
    "open_to_opportunities": 3,
}

RECOMMENDED_JOB_FREQUENCY = {
    "immediately": 2,
    "daily": 4,
    "weekly": 5,
    "no_email": 7,
}


@mcp.tool()
async def naukri_get_settings() -> dict:
    """Get your Naukri account settings — job search status, alert frequencies,
    notification toggles, and communication preferences.

    Returns:
        - {status: "success", count, settings: [{section, id, label, value, value_label, description}]}
        - {status: "error", message}
    """
    try:
        data = await api_get(FORMATTED_SETTINGS_API)

        sections = data if isinstance(data, list) else data.get("sections", data.get("settings", []))
        settings = []

        if isinstance(sections, list):
            for section in sections:
                if not isinstance(section, dict):
                    continue
                section_name = section.get("sectionName", section.get("name", ""))
                section_settings = section.get("settings", section.get("items", []))
                if isinstance(section_settings, list):
                    for s in section_settings:
                        if not isinstance(s, dict):
                            continue
                        settings.append({
                            "section": section_name,
                            "id": s.get("settingId") or s.get("id", ""),
                            "label": s.get("settingLabel") or s.get("label") or s.get("name", ""),
                            "value": s.get("settingValue") or s.get("value", ""),
                            "value_label": s.get("settingValueLabel") or s.get("valueLabel", ""),
                            "description": s.get("description", ""),
                        })
        elif isinstance(sections, dict):
            for key, val in sections.items():
                settings.append({"id": key, "label": key, "value": val})

        return {
            "status": "success",
            "count": len(settings),
            "settings": settings,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get settings: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_update_settings(
    job_search_status: Optional[str] = None,
    recommended_job_frequency: Optional[str] = None,
    recommended_job_notification: Optional[bool] = None,
    recruiter_notification: Optional[bool] = None,
    promotional_notification: Optional[bool] = None,
) -> dict:
    """Update your Naukri account settings. Pass only the settings you want to change.

    Args:
        job_search_status: One of "actively_searching", "not_active", "open_to_opportunities"
        recommended_job_frequency: One of "immediately", "daily", "weekly", "no_email"
        recommended_job_notification: Enable/disable recommended job push notifications
        recruiter_notification: Enable/disable recruiter activity push notifications
        promotional_notification: Enable/disable promotional push notifications

    Returns:
        - {status: "success", updated_fields: [...]}
        - {status: "error", message}
    """
    try:
        body = {}
        updated_fields = []

        if job_search_status is not None:
            key = job_search_status.lower().replace(" ", "_")
            if key not in JOB_SEARCH_STATUS:
                return {
                    "status": "error",
                    "message": f"Invalid job_search_status '{job_search_status}'. "
                               f"Valid: {', '.join(JOB_SEARCH_STATUS.keys())}",
                }
            body["jobSearchStatus"] = JOB_SEARCH_STATUS[key]
            updated_fields.append(f"jobSearchStatus={key}")

        if recommended_job_frequency is not None:
            key = recommended_job_frequency.lower().replace(" ", "_")
            if key not in RECOMMENDED_JOB_FREQUENCY:
                return {
                    "status": "error",
                    "message": f"Invalid recommended_job_frequency '{recommended_job_frequency}'. "
                               f"Valid: {', '.join(RECOMMENDED_JOB_FREQUENCY.keys())}",
                }
            body["recommendedJob"] = RECOMMENDED_JOB_FREQUENCY[key]
            updated_fields.append(f"recommendedJob={key}")

        if recommended_job_notification is not None:
            body["recommendedJobNotification"] = recommended_job_notification
            updated_fields.append(f"recommendedJobNotification={recommended_job_notification}")

        if recruiter_notification is not None:
            body["recruiterNotification"] = recruiter_notification
            updated_fields.append(f"recruiterNotification={recruiter_notification}")

        if promotional_notification is not None:
            body["promotionalNotification"] = promotional_notification
            updated_fields.append(f"promotionalNotification={promotional_notification}")

        if not body:
            return {"status": "error", "message": "No settings provided. Pass at least one parameter."}

        # Build complete settings body from formatted settings
        # The POST endpoint rejects partial updates ("Partial data provided")
        # and raw GET response metadata causes insert errors.
        # Solution: GET formatted settings, extract clean key-value pairs, merge changes.
        try:
            formatted = await api_get(FORMATTED_SETTINGS_API)
            sections = formatted if isinstance(formatted, list) else formatted.get("sections", formatted.get("settings", []))
            current_settings = {}
            if isinstance(sections, list):
                for section in sections:
                    if not isinstance(section, dict):
                        continue
                    for s in section.get("settings", section.get("items", [])):
                        if not isinstance(s, dict):
                            continue
                        sid = s.get("settingId") or s.get("id", "")
                        sval = s.get("settingValue") if s.get("settingValue") is not None else s.get("value", "")
                        if sid:
                            current_settings[sid] = sval
            # Merge user's changes into the full settings dict
            merged = {**current_settings, **body}
        except Exception as e:
            logger.warning("Could not GET formatted settings (%s), posting user changes only", e)
            merged = body

        await api_post(SETTINGS_API, merged)
        return {
            "status": "success",
            "updated_fields": updated_fields,
            "message": f"Updated {len(updated_fields)} setting(s).",
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to update settings: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_get_blocked_companies() -> dict:
    """Get the list of companies you have blocked on Naukri.

    Blocked companies cannot see your profile or contact you.

    Returns:
        - {status: "success", count, companies: [{id, name}]}
        - {status: "error", message}
    """
    try:
        data = await api_get(BLOCKED_COMPANIES_API)
        companies_raw = data if isinstance(data, list) else data.get("blockedCompanies", data.get("companies", []))

        companies = []
        for c in companies_raw:
            if isinstance(c, dict):
                companies.append({
                    "id": c.get("id", ""),
                    "name": c.get("value") or c.get("name") or c.get("companyName", ""),
                })

        return {
            "status": "success",
            "count": len(companies),
            "companies": companies,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get blocked companies: {type(e).__name__}: {e}"}
