"""Settings tools — view and update Naukri account settings and blocked companies."""

import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.browser import browser, page_goto
from naukri_server.api import NaukriAPIError
from naukri_server.interfaces import api_client
from naukri_server.config import (
    NAUKRI_BASE, logger, FORMATTED_SETTINGS_API, SETTINGS_API, BLOCKED_COMPANIES_API,
    PROFILE_API, WIDGET_HEADERS,
    BROWSER_PAGE_LOAD, BROWSER_MODAL_APPEAR, BROWSER_FORM_SAVE,
)
from naukri_server.error_handler import handle_tool_action

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


# ---------------------------------------------------------------------------
# Private helpers — implement each settings operation
# ---------------------------------------------------------------------------

async def _get_settings() -> dict:
    """Fetch all current settings (formatted + raw consent fields)."""
    data = await api_client.get(FORMATTED_SETTINGS_API, extra_headers=WIDGET_HEADERS)

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

    # Fetch raw settings for consent/WhatsApp fields not in formatted API
    try:
        raw_data = await api_client.get(SETTINGS_API)
        consent_fields = {
            "naukri_auto_apply_consent": bool(raw_data.get("naukriAutoApplyConsent", 0)),
            "linkedin_auto_apply_consent": bool(raw_data.get("linkedinAutoApplyConsent", 0)),
            "whatsapp_apply_notification": bool(raw_data.get("applyWhatsAppNotification", 0)),
            "whatsapp_profile_notification": bool(raw_data.get("profileWhatsAppNotification", 0)),
        }
    except Exception:
        consent_fields = {}

    return {
        "status": "success",
        "count": len(settings),
        "settings": settings,
        **consent_fields,
    }


async def _update_settings(
    job_search_status: Optional[str] = None,
    recommended_job_frequency: Optional[str] = None,
    recommended_job_notification: Optional[bool] = None,
    recruiter_notification: Optional[bool] = None,
    promotional_notification: Optional[bool] = None,
) -> dict:
    """Update one or more account settings. Returns merged results from browser + API paths."""
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
                "error_code": "VALIDATION_ERROR",
            }
        radio_id = _JOB_SEARCH_RADIO[key]
        try:
            async with browser.page_pool.acquire() as page:
                await page_goto(page, SETTINGS_PAGE)
                await asyncio.sleep(BROWSER_PAGE_LOAD)

                if "/nlogin" in page.url:
                    return {"status": "error", "message": "Not logged in. Call naukri_login first.", "error_code": "AUTH_ERROR"}

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
                        return {"status": "error", "message": f"Radio button #{radio_id} not found on settings page", "error_code": "NOT_FOUND"}

                    await asyncio.sleep(BROWSER_MODAL_APPEAR)

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

                    await asyncio.sleep(BROWSER_FORM_SAVE)
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
    body = {}

    if recommended_job_frequency is not None:
        key = recommended_job_frequency.lower().replace(" ", "_")
        if key not in RECOMMENDED_JOB_FREQUENCY:
            return {
                "status": "error",
                "message": f"Invalid recommended_job_frequency '{recommended_job_frequency}'. "
                           f"Valid: {', '.join(RECOMMENDED_JOB_FREQUENCY.keys())}",
                "error_code": "VALIDATION_ERROR",
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
            try:
                from naukri_server.events import event_bus, SettingsUpdated
                await event_bus.emit(SettingsUpdated(updated_fields=", ".join(updated_fields)))
            except Exception:
                pass
            result = {"status": "success", "updated_fields": updated_fields}
            result.update(jss_result)
            return result
        return {"status": "error", "message": "No settings provided. Pass at least one parameter.", "error_code": "VALIDATION_ERROR"}

    # GET current formatted settings, extract {settingId: numericValue} pairs,
    # merge user changes, POST complete settings.
    try:
        formatted = await api_client.get(FORMATTED_SETTINGS_API, extra_headers=WIDGET_HEADERS)
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

    await api_client.post(SETTINGS_API, merged)

    try:
        from naukri_server.events import event_bus, SettingsUpdated
        await event_bus.emit(SettingsUpdated(updated_fields=", ".join(updated_fields)))
    except Exception:
        pass

    result = {
        "status": "success",
        "updated_fields": updated_fields,
        "message": f"Updated {len(updated_fields)} setting(s).",
    }
    if jss_result is not None:
        result.update(jss_result)
    return result


async def _blocked_companies() -> dict:
    """List companies you've blocked."""
    data = await api_client.get(BLOCKED_COMPANIES_API)
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


async def _check_email() -> dict:
    """Check email/mobile verification status from profile API."""
    # The /mail-verification endpoint returns 405. Email/mobile verification
    # status is available in the profile API's user object instead.
    data = await api_client.get(PROFILE_API, params={"expand_level": "1"})
    user = data.get("user", {})
    return {
        "status": "success",
        "is_email_verified": user.get("isEmailVerified", None),
        "is_mobile_verified": user.get("isMobileVerified", None),
        "email": user.get("email", user.get("username", "")),
        "mobile": user.get("mobile", ""),
    }


async def _visibility() -> dict:
    """Show profile visibility toggles (Resdex visibility, search visibility, etc.)."""
    data = await api_client.get(PROFILE_API, params={"expand_level": "4"})

    # resdexVisibility lives at top-level, inside profile[0], or inside user
    resdex = (
        data.get("resdexVisibility")
        or data.get("profileVisibility")
        or (data.get("profile", [{}])[0] if isinstance(data.get("profile"), list) else {}).get("resdexVisibility")
        or data.get("profileAdditional", {}).get("resdexVisibility")
        or data.get("user", {}).get("resdexVisibility")
    )

    if resdex and isinstance(resdex, dict):
        visibility = {
            "show_profile": resdex.get("showProfile") or resdex.get("profileVisible"),
            "show_current_employer": resdex.get("showCurrentEmployer") or resdex.get("currentEmployerVisible"),
            "show_email": resdex.get("showEmail") or resdex.get("emailVisible"),
            "show_phone": resdex.get("showPhone") or resdex.get("mobileVisible"),
            "show_resume": resdex.get("showResume") or resdex.get("resumeVisible"),
            "hide_from_companies": resdex.get("hideFromCompanies") or resdex.get("blockedCompanies"),
            "search_visible": resdex.get("searchVisible") or resdex.get("isSearchable"),
            "active_job_seeker": resdex.get("activeJobSeeker") or resdex.get("isActivelySearching"),
        }
        # Strip None values for cleaner output
        visibility = {k: v for k, v in visibility.items() if v is not None}

        if not visibility:
            return {
                "status": "success",
                "message": "resdexVisibility object found but all fields are null — API may have changed.",
                "raw_keys": list(resdex.keys()),
                "visibility": {},
            }

        return {
            "status": "success",
            "visibility": visibility,
            "hint": "These reflect Resdex (recruiter database) visibility. Use naukri_get_profile() for full profile context.",
        }
    elif resdex and isinstance(resdex, str):
        # Sometimes returned as a simple string like "visible" / "hidden"
        return {
            "status": "success",
            "visibility": resdex,
            "hint": "Visibility returned as a simple status string.",
        }
    else:
        return {
            "status": "success",
            "message": "No resdexVisibility data found in profile API response. "
                       "This field may not be populated for all accounts.",
            "visibility": {},
            "api_top_keys": list(data.keys())[:20],
        }


async def _notification_prefs() -> dict:
    """Show notification preferences (email, SMS, push, WhatsApp toggles)."""
    data = await api_client.get(PROFILE_API, params={"expand_level": "4"})

    # communicationSettings can be at top level, inside profile[0], or inside user
    comm = (
        data.get("communicationSettings")
        or data.get("communicationPreferences")
        or (data.get("profile", [{}])[0] if isinstance(data.get("profile"), list) else {}).get("communicationSettings")
        or data.get("user", {}).get("communicationSettings")
    )

    if comm and isinstance(comm, dict):
        prefs = {
            "email_notifications": comm.get("emailNotifications") or comm.get("emailAlerts"),
            "sms_notifications": comm.get("smsNotifications") or comm.get("smsAlerts"),
            "push_notifications": comm.get("pushNotifications"),
            "job_alerts": comm.get("jobAlerts") or comm.get("jobAlert"),
            "recruiter_messages": comm.get("recruiterMessages") or comm.get("recruiterMail"),
            "promotional": comm.get("promotional") or comm.get("marketingMail"),
            "whatsapp_notifications": comm.get("whatsappNotifications") or comm.get("whatsappAlerts"),
        }
        # Strip None values for cleaner output
        prefs = {k: v for k, v in prefs.items() if v is not None}

        if not prefs:
            return {
                "status": "success",
                "message": "communicationSettings object found but all fields are null — API may have changed.",
                "raw_keys": list(comm.keys()),
                "notification_prefs": {},
            }

        return {
            "status": "success",
            "notification_prefs": prefs,
            "hint": "Use naukri_update_settings to toggle recruiter_notification, promotional_notification, etc.",
        }
    else:
        return {
            "status": "success",
            "message": "No communicationSettings data found in profile API response. "
                       "This field may not be populated for all accounts.",
            "notification_prefs": {},
            "api_top_keys": list(data.keys())[:20],
        }


# ---------------------------------------------------------------------------
# Atomic single-purpose MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_get_settings() -> dict:
    """Fetch all current Naukri account settings (job-search status, notifications, consent flags).

    Returns the raw formatted-settings structure plus consent fields (auto-apply, WhatsApp).
    For a human-readable visibility summary, use naukri_visibility() instead.

    Returns:
        {status, count, settings: [{section, id, label, value, value_label, description}],
         naukri_auto_apply_consent, linkedin_auto_apply_consent,
         whatsapp_apply_notification, whatsapp_profile_notification}
    """
    return await handle_tool_action(_get_settings, "settings.get")


@mcp.tool()
async def naukri_update_settings(
    job_search_status: Optional[str] = None,
    recommended_job_frequency: Optional[str] = None,
    recommended_job_notification: Optional[bool] = None,
    recruiter_notification: Optional[bool] = None,
    promotional_notification: Optional[bool] = None,
) -> dict:
    """Modify Naukri account settings. Pass only fields you want to change.

    job_search_status uses browser UI (radio buttons); other fields use the REST API.

    Args:
        job_search_status: "actively_searching" | "open_to_opportunities" | "not_active"
        recommended_job_frequency: "immediately" | "daily" | "weekly" | "no_email"
        recommended_job_notification: enable/disable recommended job emails
        recruiter_notification: enable/disable recruiter contact emails
        promotional_notification: enable/disable promotional emails

    Returns:
        {status, updated_fields, message, [browser-specific fields]}
    """
    return await handle_tool_action(
        lambda: _update_settings(
            job_search_status=job_search_status,
            recommended_job_frequency=recommended_job_frequency,
            recommended_job_notification=recommended_job_notification,
            recruiter_notification=recruiter_notification,
            promotional_notification=promotional_notification,
        ),
        "settings.update",
    )


@mcp.tool()
async def naukri_blocked_companies() -> dict:
    """List companies you've blocked from contacting you.

    Returns:
        {status, count, companies: [{id, name}]}
    """
    return await handle_tool_action(_blocked_companies, "settings.blocked_companies")


@mcp.tool()
async def naukri_check_email() -> dict:
    """Check email and mobile verification status from your profile.

    Returns:
        {status, is_email_verified, is_mobile_verified, email, mobile}
    """
    return await handle_tool_action(_check_email, "settings.check_email")


@mcp.tool()
async def naukri_visibility() -> dict:
    """Show profile visibility toggles (Resdex visibility, search visibility, contact info exposure).

    Returns a human-readable summary of who can see your profile in recruiter searches.

    Returns:
        {status, visibility: {show_profile, show_email, show_phone, ...}, hint}
        Or {status, message, visibility: {}, api_top_keys} if data not found.
    """
    return await handle_tool_action(_visibility, "settings.visibility")


@mcp.tool()
async def naukri_notification_prefs() -> dict:
    """Show notification preferences (email, SMS, push, WhatsApp toggles).

    Returns:
        {status, notification_prefs: {email_notifications, sms_notifications, ...}, hint}
        Or {status, message, notification_prefs: {}, api_top_keys} if data not found.
    """
    return await handle_tool_action(_notification_prefs, "settings.notification_prefs")


@mcp.tool()
async def naukri_subscription_status() -> dict:
    """Get Naukri 360 subscription status, expiry, and feature flags.

    Returns:
        {status, is_subscribed, expiry_date, features: {...}}
    """
    from naukri_server.tools.subscription import _get_subscription_status
    return await handle_tool_action(_get_subscription_status, "settings.subscription")
