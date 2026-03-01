"""Settings tools — view and update Naukri account settings and blocked companies."""

import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.browser import browser, page_goto
from naukri_server.api import api_get, api_post, NaukriAPIError, api_tool
from naukri_server.config import NAUKRI_BASE, logger, FORMATTED_SETTINGS_API, SETTINGS_API, BLOCKED_COMPANIES_API, PROFILE_API

SETTINGS_PAGE = f"{NAUKRI_BASE}/mnjuser/settings/communication"

# Maps our enum names to radio button IDs on the settings page
_JOB_SEARCH_RADIO = {
    "actively_searching": "active",
    "open_to_opportunities": "notActiveButOpen",
    "not_active": "customize",
}

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
@api_tool("Get settings")
async def naukri_get_settings() -> dict:
    """Get your Naukri account settings — job search status, alert frequencies,
    notification toggles, and communication preferences.

    Returns:
        - {status: "success", count, settings: [{section, id, label, value, value_label, description}]}
        - {status: "error", message}
    """
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
    updated_fields = []
    jss_result = None

    # --- Job Search Status: use browser UI (radio buttons on settings page) ---
    if job_search_status is not None:
        key = job_search_status.lower().replace(" ", "_")
        if key not in _JOB_SEARCH_RADIO:
            return {
                "status": "error",
                "message": f"Invalid job_search_status '{job_search_status}'. "
                           f"Valid: {', '.join(_JOB_SEARCH_RADIO.keys())}",
            }
        radio_id = _JOB_SEARCH_RADIO[key]
        try:
            async with browser.page_pool.acquire() as page:
                await page_goto(page, SETTINGS_PAGE)
                await asyncio.sleep(3)

                if "/nlogin" in page.url:
                    return {"status": "error", "message": "Not logged in. Call naukri_login first."}

                # Capture settings API calls triggered by the radio click
                captured = {}

                async def on_response(response):
                    if "settings" in response.url and response.request.method == "POST":
                        captured["status"] = response.status
                        captured["url"] = response.url

                page.on("response", on_response)
                try:
                    # Click the radio button via JS (it may be out of viewport)
                    clicked = await page.evaluate("""(radioId) => {
                        const radio = document.getElementById(radioId);
                        if (!radio) return null;
                        radio.scrollIntoView({block: 'center'});
                        radio.click();
                        return radioId;
                    }""", radio_id)

                    if not clicked:
                        return {"status": "error", "message": f"Radio button #{radio_id} not found on settings page"}

                    await asyncio.sleep(2)

                    # After clicking radio, find and click the SELECT button in that row
                    select_clicked = await page.evaluate("""(radioId) => {
                        const radio = document.getElementById(radioId);
                        if (!radio) return null;
                        const row = radio.closest('.jobSearchState, [class*="jobSearchState"]');
                        if (!row) return 'no_row';
                        // Look for SELECT/SAVE button in the row
                        const btn = row.querySelector('a, button, [class*="select"], [class*="btn"]');
                        if (btn) { btn.click(); return 'select_clicked'; }
                        // Also try clicking the row itself
                        row.click();
                        return 'row_clicked';
                    }""", radio_id)

                    await asyncio.sleep(3)
                finally:
                    page.remove_listener("response", on_response)

                updated_fields.append(f"jobSearchStatus={key}")
                api_ok = captured.get("status") in (200, 201, 204)

                # Store result instead of returning early, so other settings
                # (recommended_job_frequency, notifications, etc.) can still be processed.
                jss_result = {
                    "method": "browser_ui",
                    "api_confirmed": api_ok,
                    "select_result": select_clicked,
                    "jss_message": f"Job search status set to '{key}' via settings page.",
                }
        except Exception as e:
            jss_result = {
                "method": "browser_ui",
                "jss_error": f"Failed to update job search status: {type(e).__name__}: {e}",
            }

    # --- Other settings: try API POST with full settings body ---
    try:
        body = {}

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
            if jss_result is not None:
                # Only job_search_status was requested; no API settings to update
                result = {"status": "success", "updated_fields": updated_fields}
                result.update(jss_result)
                return result
            return {"status": "error", "message": "No settings provided. Pass at least one parameter."}

        # GET current formatted settings, extract {settingId: numericValue} pairs,
        # merge user changes, POST complete settings.
        try:
            formatted = await api_get(FORMATTED_SETTINGS_API)
            raw_settings = formatted.get("settings", formatted)
            current_settings = {}
            if isinstance(raw_settings, dict):
                for sid, sval in raw_settings.items():
                    if isinstance(sval, dict) and "id" in sval:
                        current_settings[sid] = sval["id"]
                    else:
                        current_settings[sid] = sval
            merged = {**current_settings, **body}
        except Exception as e:
            logger.warning("Could not GET formatted settings (%s), posting user changes only", e)
            merged = body

        await api_post(SETTINGS_API, merged)
        result = {
            "status": "success",
            "updated_fields": updated_fields,
            "message": f"Updated {len(updated_fields)} setting(s).",
        }
        if jss_result is not None:
            result.update(jss_result)
        return result
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to update settings: {type(e).__name__}: {e}"}


@mcp.tool()
@api_tool("Get blocked companies")
async def naukri_get_blocked_companies() -> dict:
    """Get the list of companies you have blocked on Naukri.

    Blocked companies cannot see your profile or contact you.

    Returns:
        - {status: "success", count, companies: [{id, name}]}
        - {status: "error", message}
    """
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


@mcp.tool()
@api_tool("Check email verification")
async def naukri_check_email_verification() -> dict:
    """Check if your Naukri account email is verified.

    Returns:
        - {status: "success", is_verified, email, ...}
        - {status: "error", message}
    """
    # The /mail-verification endpoint returns 405. Email/mobile verification
    # status is available in the profile API's user object instead.
    data = await api_get(PROFILE_API, params={"expand_level": "1"})
    user = data.get("user", {})
    return {
        "status": "success",
        "is_email_verified": user.get("isEmailVerified", None),
        "is_mobile_verified": user.get("isMobileVerified", None),
        "email": user.get("email", user.get("username", "")),
        "mobile": user.get("mobile", ""),
    }
