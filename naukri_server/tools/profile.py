import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.browser import browser, page_goto
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import NAUKRI_BASE, DASHBOARD_API, PROFILE_API, FULLPROFILES_API, logger
from naukri_server.validation import validate_profile


# ============================================================================
# Tool 6: Refresh Profile (Playwright — needs browser interaction)
# ============================================================================


@mcp.tool()
async def naukri_refresh_profile(randomize: bool = False) -> dict:
    """Refresh Naukri profile to boost visibility (daily trick).

    Re-saves your resume headline via REST API to trigger Naukri's
    'recently active' signal — recruiters see you first.

    Args:
        randomize: If True, wait a random 0-300 seconds before refreshing
                   (useful for scheduled/cron-based calls to look natural)

    Returns:
        - {status: "refreshed", method, message}
        - {status: "partial", method, message}
        - {status: "error", message}
    """
    if randomize:
        import random
        delay = random.randint(0, 300)
        logger.info("Randomized delay: %d seconds", delay)
        await asyncio.sleep(delay)

    # Strategy 1: REST API (fast, no browser interaction)
    try:
        profile_data = await api_get(
            PROFILE_API,
            {"expand_level": "4"},
        )
        profiles = profile_data.get("profile", [])
        if profiles and isinstance(profiles[0], dict):
            headline = profiles[0].get("resumeHeadline", "")
            if headline:
                for version in ("v0", "v2"):
                    try:
                        endpoint = FULLPROFILES_API if version == "v0" else FULLPROFILES_API.replace("/v0/", f"/{version}/")
                        await api_post(endpoint, {"resumeHeadline": headline})
                        return {
                            "status": "refreshed",
                            "method": f"rest_api_{version}",
                            "headline_length": len(headline),
                            "message": f"Profile refreshed via REST API ({version}). You appear as 'recently active'.",
                        }
                    except Exception:
                        continue
    except Exception as e:
        logger.warning("REST refresh failed, falling back to browser: %s", e)

    # Strategy 2: Browser fallback (original approach)
    async with browser.page_pool.acquire() as page:
        await page_goto(page, f"{NAUKRI_BASE}/mnjuser/profile")
        await asyncio.sleep(3)

        if "/nlogin" in page.url:
            return {"status": "error", "message": "Not logged in. Call naukri_login first."}

        edit_clicked = await page.evaluate("""() => {
            const editIcons = Array.from(document.querySelectorAll('*')).filter(el =>
                el.children.length === 0 && el.textContent.trim() === 'editOneTheme'
            );
            if (editIcons.length === 0) return null;
            for (const icon of editIcons) {
                const section = icon.closest('[class*="headline"], [class*="resumeHeadline"]');
                if (section) { icon.click(); return 'headline_edit'; }
            }
            for (const icon of editIcons) {
                const parent = icon.closest('div, section');
                if (parent && parent.textContent.includes('Resume headline')) {
                    icon.click();
                    return 'headline_parent';
                }
            }
            editIcons[0].click();
            return 'first_edit';
        }""")

        if not edit_clicked:
            return {"status": "error", "message": "Could not find edit button on profile"}

        await asyncio.sleep(2)

        api_confirmed = {}

        async def on_response(response):
            if "fullprofiles" in response.url and response.request.method == "POST":
                api_confirmed["status"] = response.status

        page.on("response", on_response)

        try:
            save_result = await page.evaluate("""() => {
                const modal = document.querySelector('[class*="modal"], [class*="dialog"], [class*="overlay"], [role="dialog"]');
                if (modal) {
                    const btn = Array.from(modal.querySelectorAll('button')).find(
                        b => b.textContent.trim().toLowerCase() === 'save'
                    );
                    if (btn) { btn.click(); return 'modal_save'; }
                }
                const exactSave = Array.from(document.querySelectorAll('button')).find(
                    b => b.textContent.trim() === 'Save' && b.offsetParent !== null
                );
                if (exactSave) { exactSave.click(); return 'exact_save'; }
                return null;
            }""")

            if not save_result:
                return {"status": "partial", "method": edit_clicked, "message": "Edit opened but Save not found."}

            await asyncio.sleep(3)
        finally:
            page.remove_listener("response", on_response)

        return {
            "status": "refreshed",
            "method": f"browser_{edit_clicked}",
            "save": save_result,
            "api_confirmed": api_confirmed.get("status") == 200,
            "message": "Profile refreshed via browser. You appear as 'recently active'.",
        }


# ============================================================================
# Tool 8: Get Profile (REST API)
# ============================================================================


@mcp.tool()
async def naukri_get_profile() -> dict:
    """Get your full Naukri profile via API.

    Returns skills (with experience years), employment history, education,
    current CTC, expected CTC, notice period, location -- everything needed
    for Claude to auto-answer screening questions intelligently.

    Returns:
        - {status: "success", name, current_ctc, expected_ctc, notice_period, total_experience, skills_with_experience, employment, education}
        - {status: "error", message}
    """
    try:
        data = await api_get(
            PROFILE_API,
            {"expand_level": "4"},
        )

        profile = data.get("profile", [{}])[0]
        additional = data.get("profileAdditional", {})

        skills = []
        for s in data.get("itskills", []):
            exp_time = s.get("experienceTime", {})
            skills.append({
                "skill": s.get("skill"),
                "experience_years": exp_time.get("year", 0),
                "experience_months": exp_time.get("month", 0),
            })

        employment = []
        for emp in data.get("employments", []):
            employment.append({
                "designation": emp.get("designation"),
                "organization": emp.get("organization"),
                "start_date": emp.get("startDate"),
                "end_date": emp.get("endDate") or "Present",
            })

        education = []
        for edu in data.get("educations", []):
            education.append({
                "degree": edu.get("course", {}).get("value"),
                "specialization": edu.get("specialisation", {}).get("value"),
                "institute": edu.get("institute"),
                "year": edu.get("yearOfCompletion"),
            })

        exp = profile.get("experience", {})

        result = {
            "status": "success",
            "name": profile.get("name"),
            "current_ctc": profile.get("absoluteCtc"),
            "expected_ctc": profile.get("absoluteExpectedCtc"),
            "notice_period": profile.get("noticePeriod", {}).get("value"),
            "total_experience": f"{exp.get('year', 0)} years {exp.get('month', 0)} months",
            "current_location": profile.get("city", {}).get("value"),
            "gender": "Male" if profile.get("gender") == "M" else "Female",
            "key_skills": profile.get("keySkills"),
            "skills_with_experience": skills,
            "employment": employment,
            "education": education,
            "profile_id": additional.get("profileId"),
        }
        warnings = validate_profile(result)
        if warnings:
            result["warnings"] = warnings
        return result
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Profile API failed: {type(e).__name__}: {e!r}"}


# ============================================================================
# Tool: Get Dashboard (REST API)
# ============================================================================


@mcp.tool()
async def naukri_get_dashboard() -> dict:
    """Get your Naukri dashboard summary via API.

    Returns profile views, recruiter activity, CTC, experience,
    recruiter invites, and unread mail counts — a quick health check
    of your Naukri presence.

    Returns:
        - {status: "success", profile_views, recruiter_activity_date, ctc_lpa, experience_years, unread_invites, total_invites, unread_relevant_mail, has_inbox, total_matches}
        - {status: "error", message}
    """
    try:
        data = await api_get(DASHBOARD_API)
        db = data.get("dashBoard", {})

        return {
            "status": "success",
            "profile_views": db.get("profileViewCount"),
            "recruiter_activity_date": db.get("recruiterActionsLatestDate"),
            "ctc_lpa": db.get("rawCtc"),
            "experience_years": db.get("rawTotalExperience"),
            "unread_invites": db.get("unreadPowerNvite"),
            "total_invites": db.get("totalPowerNvite"),
            "unread_relevant_mail": db.get("unreadMostRelevantMail"),
            "has_inbox": db.get("hasInboxFlag") == "Y",
            "total_matches": db.get("mrt"),
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get dashboard: {type(e).__name__}: {e}"}


# ============================================================================
# Tool: Update Profile (REST API — partial update)
# ============================================================================

UPDATABLE_FIELDS = {
    "resumeHeadline", "keySkills", "summary", "noticePeriod",
    "expectedCtc", "locationPrefId", "experience", "currentCtc",
    "absoluteCtc", "absoluteExpectedCtc", "name", "gender",
    "maritalStatus", "dateOfBirth", "homeTown", "pinCode",
}


@mcp.tool()
async def naukri_update_profile(fields: dict) -> dict:
    """Update your Naukri profile fields via API (partial update).

    Sends only the fields you specify — everything else stays unchanged.
    Use naukri_get_profile first to see current values.

    Args:
        fields: Dict of fields to update. Supported keys:
            - resumeHeadline: str — your profile headline
            - keySkills: str — comma-separated skills
            - summary: str — profile summary / about me
            - noticePeriod: dict — {"id": "1", "value": "15 Days or less"}
            - expectedCtc: str — e.g. "20" (in lakhs)
            - currentCtc: str — e.g. "16" (in lakhs)
            - experience: dict — {"year": 8, "month": 0}
            - locationPrefId: list — location preference IDs
            - name: str — full name
            - gender: str — "M" or "F"

    Returns:
        - {status: "updated", updated_fields: [...], response: {...}}
        - {status: "error", message}
    """
    if not fields:
        return {"status": "error", "message": "No fields provided. Pass at least one field to update."}

    unknown = set(fields.keys()) - UPDATABLE_FIELDS
    if unknown:
        return {
            "status": "error",
            "message": f"Unknown fields: {', '.join(sorted(unknown))}. "
                       f"Supported: {', '.join(sorted(UPDATABLE_FIELDS))}",
        }

    # The fullprofiles endpoint rejects all external API calls (405 from CDN).
    # Only the React app's own XHR works. Use the browser UI approach:
    # navigate to profile → click edit → modify form fields → click save.
    async with browser.page_pool.acquire() as page:
        try:
            await page_goto(page, f"{NAUKRI_BASE}/mnjuser/profile")
            await asyncio.sleep(3)

            if "/nlogin" in page.url:
                return {"status": "error", "message": "Not logged in. Call naukri_login first."}

            # Track which fields were updated via UI
            ui_updated = []
            api_confirmed = {}

            async def on_response(response):
                if "fullprofiles" in response.url and response.request.method == "POST":
                    api_confirmed["status"] = response.status

            page.on("response", on_response)

            try:
                # --- Resume Headline ---
                if "resumeHeadline" in fields:
                    edit_clicked = await page.evaluate("""() => {
                        const editIcons = Array.from(document.querySelectorAll('*')).filter(el =>
                            el.children.length === 0 && el.textContent.trim() === 'editOneTheme'
                        );
                        if (editIcons.length === 0) return null;
                        for (const icon of editIcons) {
                            const section = icon.closest('[class*="headline"], [class*="resumeHeadline"]');
                            if (section) { icon.click(); return 'headline_edit'; }
                        }
                        for (const icon of editIcons) {
                            const parent = icon.closest('div, section');
                            if (parent && parent.textContent.includes('Resume headline')) {
                                icon.click();
                                return 'headline_parent';
                            }
                        }
                        editIcons[0].click();
                        return 'first_edit';
                    }""")

                    if not edit_clicked:
                        return {"status": "error", "message": "Could not find edit button for resume headline"}

                    await asyncio.sleep(2)

                    # Find textarea in the modal and fill with new headline
                    textarea = await page.query_selector(
                        '[class*="modal"] textarea, [class*="dialog"] textarea, '
                        '[role="dialog"] textarea, textarea[class*="headline"], '
                        'textarea'
                    )
                    if textarea:
                        await textarea.click()
                        await textarea.fill("")
                        await textarea.fill(fields["resumeHeadline"])
                        await asyncio.sleep(0.5)
                        ui_updated.append("resumeHeadline")
                    else:
                        return {"status": "error", "message": "Edit modal opened but textarea not found"}

                    # Click save
                    save_result = await page.evaluate("""() => {
                        const modal = document.querySelector('[class*="modal"], [class*="dialog"], [class*="overlay"], [role="dialog"]');
                        if (modal) {
                            const btn = Array.from(modal.querySelectorAll('button')).find(
                                b => b.textContent.trim().toLowerCase() === 'save'
                            );
                            if (btn) { btn.click(); return 'modal_save'; }
                        }
                        const exactSave = Array.from(document.querySelectorAll('button')).find(
                            b => b.textContent.trim() === 'Save' && b.offsetParent !== null
                        );
                        if (exactSave) { exactSave.click(); return 'exact_save'; }
                        return null;
                    }""")

                    if not save_result:
                        return {"status": "error", "message": "Edit modal opened but Save button not found"}

                    await asyncio.sleep(3)

                # --- Key Skills ---
                elif "keySkills" in fields:
                    # Navigate to key skills section and click edit
                    edit_clicked = await page.evaluate("""() => {
                        const editIcons = Array.from(document.querySelectorAll('*')).filter(el =>
                            el.children.length === 0 && el.textContent.trim() === 'editOneTheme'
                        );
                        for (const icon of editIcons) {
                            const parent = icon.closest('div, section');
                            if (parent && parent.textContent.includes('Key skills')) {
                                icon.click();
                                return 'skills_edit';
                            }
                        }
                        return null;
                    }""")

                    if not edit_clicked:
                        return {"status": "error", "message": "Could not find edit button for key skills"}

                    await asyncio.sleep(2)

                    # Find the skills input and update
                    skills_input = await page.query_selector(
                        '[class*="modal"] input[type="text"], [class*="dialog"] input, '
                        '[role="dialog"] input[type="text"], input[class*="skill"]'
                    )
                    if skills_input:
                        await skills_input.click()
                        await skills_input.fill("")
                        await skills_input.fill(fields["keySkills"])
                        await asyncio.sleep(0.5)
                        ui_updated.append("keySkills")
                    else:
                        return {"status": "error", "message": "Skills edit opened but input not found"}

                    # Click save
                    save_result = await page.evaluate("""() => {
                        const modal = document.querySelector('[class*="modal"], [class*="dialog"], [class*="overlay"], [role="dialog"]');
                        if (modal) {
                            const btn = Array.from(modal.querySelectorAll('button')).find(
                                b => b.textContent.trim().toLowerCase() === 'save'
                            );
                            if (btn) { btn.click(); return 'modal_save'; }
                        }
                        const exactSave = Array.from(document.querySelectorAll('button')).find(
                            b => b.textContent.trim() === 'Save' && b.offsetParent !== null
                        );
                        if (exactSave) { exactSave.click(); return 'exact_save'; }
                        return null;
                    }""")

                    if not save_result:
                        return {"status": "error", "message": "Skills edit opened but Save button not found"}

                    await asyncio.sleep(3)

                else:
                    # For other fields, no browser UI support yet
                    unsupported = [k for k in fields.keys() if k not in ("resumeHeadline", "keySkills")]
                    return {
                        "status": "error",
                        "message": f"Browser UI update not yet supported for: {', '.join(unsupported)}. "
                                   f"Currently supported: resumeHeadline, keySkills",
                    }

            finally:
                page.remove_listener("response", on_response)

            return {
                "status": "updated",
                "method": "browser_ui",
                "updated_fields": ui_updated,
                "api_confirmed": api_confirmed.get("status") == 200,
                "message": f"Profile updated via browser UI. Fields: {', '.join(ui_updated)}",
            }
        except Exception as e:
            return {"status": "error", "message": f"Profile update failed: {type(e).__name__}: {e}"}
