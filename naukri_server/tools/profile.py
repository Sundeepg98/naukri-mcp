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
async def naukri_boost_visibility(randomize: bool = False) -> dict:
    """Boost your profile visibility by refreshing your headline — re-saves your current headline to bump your profile in recruiter searches.

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
    try:
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
    except Exception as e:
        return {"status": "error", "message": f"Browser fallback failed: {type(e).__name__}: {e}"}


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

BROWSER_SUPPORTED_FIELDS = {"resumeHeadline", "keySkills"}


@mcp.tool()
async def naukri_update_profile(fields: dict) -> dict:
    """Update your Naukri profile fields via browser UI automation.

    Currently supported fields: resumeHeadline, keySkills (browser-based update).
    Other fields in UPDATABLE_FIELDS are accepted but may not persist —
    Naukri's API support for direct field updates varies.

    Use naukri_get_profile first to see current values.

    Args:
        fields: Dict of fields to update. Currently supported:
            - resumeHeadline: str — your profile headline
            - keySkills: str — comma-separated skills
            Other fields in UPDATABLE_FIELDS are accepted but may not persist.

    Returns:
        - {status: "updated", updated_fields: [...], method, api_confirmed, message}
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
                if "keySkills" in fields:
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


# ============================================================================
# Tool: Audit Profile — actionable improvement suggestions
# ============================================================================


@mcp.tool()
async def naukri_audit_profile() -> dict:
    """Audit your Naukri profile and get actionable improvement suggestions.

    Checks profile completeness, identifies missing sections, and provides
    specific tips to improve recruiter visibility.

    For raw profile data, use naukri_get_profile instead.
    For just the completeness percentage, use naukri_get_profile_completeness.

    Returns:
        - {status: "success", completeness_pct, grade,
           strengths: [...], gaps: [{section, action, impact}, ...],
           tips: [...]}
        - {status: "error", message}
    """
    try:
        # Get profile data
        profile_result = await naukri_get_profile()
        if profile_result.get("status") != "success":
            return profile_result

        # Get completeness percentage
        from naukri_server.tools.assessments import naukri_get_profile_completeness
        completeness_result = await naukri_get_profile_completeness()
        completeness_pct = None
        if completeness_result.get("status") == "success":
            completeness_pct = completeness_result.get("completeness_percent")

        # Analyze sections
        strengths = []
        gaps = []

        # Check resume headline
        # Note: headline is not directly in profile API but we can check key_skills
        if profile_result.get("key_skills"):
            skills = profile_result["key_skills"]
            if isinstance(skills, str):
                skill_count = len([s.strip() for s in skills.split(",") if s.strip()])
            elif isinstance(skills, list):
                skill_count = len(skills)
            else:
                skill_count = 0

            if skill_count >= 10:
                strengths.append(f"Good skill coverage ({skill_count} skills listed)")
            elif skill_count > 0:
                gaps.append({
                    "section": "Key Skills",
                    "action": f"Add more skills (currently {skill_count}, aim for 15+)",
                    "impact": "high",
                })
        else:
            gaps.append({
                "section": "Key Skills",
                "action": "Add relevant skills to your profile",
                "impact": "high",
            })

        # Check employment history
        employment = profile_result.get("employment", [])
        if employment:
            strengths.append(f"Employment history present ({len(employment)} entries)")
            # Check if current job has end_date "Present"
            current = [e for e in employment if e.get("end_date") == "Present"]
            if not current:
                gaps.append({
                    "section": "Employment",
                    "action": "Mark your current job (no entry shows 'Present')",
                    "impact": "medium",
                })
        else:
            gaps.append({
                "section": "Employment",
                "action": "Add your employment history",
                "impact": "high",
            })

        # Check education
        education = profile_result.get("education", [])
        if education:
            strengths.append(f"Education details present ({len(education)} entries)")
        else:
            gaps.append({
                "section": "Education",
                "action": "Add your educational qualifications",
                "impact": "medium",
            })

        # Check CTC info
        if profile_result.get("current_ctc"):
            strengths.append("Current CTC specified")
        else:
            gaps.append({
                "section": "Current CTC",
                "action": "Add your current CTC for better job matching",
                "impact": "medium",
            })

        if profile_result.get("expected_ctc"):
            strengths.append("Expected CTC specified")
        else:
            gaps.append({
                "section": "Expected CTC",
                "action": "Add your expected CTC to filter relevant jobs",
                "impact": "medium",
            })

        # Check notice period
        if profile_result.get("notice_period"):
            strengths.append(f"Notice period set: {profile_result['notice_period']}")
        else:
            gaps.append({
                "section": "Notice Period",
                "action": "Set your notice period — recruiters filter by availability",
                "impact": "high",
            })

        # Check skills with experience
        skills_exp = profile_result.get("skills_with_experience", [])
        if skills_exp:
            with_years = [s for s in skills_exp if s.get("experience_years", 0) > 0]
            if with_years:
                strengths.append(f"{len(with_years)} skills have experience years specified")
            if len(skills_exp) > len(with_years):
                gaps.append({
                    "section": "IT Skills",
                    "action": f"Add experience years for {len(skills_exp) - len(with_years)} skills missing them",
                    "impact": "medium",
                })

        # Calculate grade
        if completeness_pct is not None:
            if completeness_pct >= 80:
                grade = "A"
            elif completeness_pct >= 60:
                grade = "B"
            elif completeness_pct >= 40:
                grade = "C"
            else:
                grade = "D"
        else:
            # Estimate from gaps
            if len(gaps) == 0:
                grade = "A"
            elif len(gaps) <= 2:
                grade = "B"
            elif len(gaps) <= 4:
                grade = "C"
            else:
                grade = "D"

        # Tips
        tips = []
        if grade in ("C", "D"):
            tips.append("Profiles with 80%+ completeness get 3x more recruiter views")
        if not any(g["section"] == "Key Skills" for g in gaps):
            tips.append("Update your skills regularly to match trending job requirements")
        else:
            tips.append("Profiles with 15+ skills appear in more search results")
        tips.append("Use naukri_boost_visibility daily to stay in 'recently active' searches")
        if gaps:
            high_impact = [g for g in gaps if g["impact"] == "high"]
            if high_impact:
                tips.append(f"Priority: Fix {len(high_impact)} high-impact gap(s) first")

        return {
            "status": "success",
            "completeness_pct": completeness_pct,
            "grade": grade,
            "strengths": strengths,
            "gaps": gaps,
            "tips": tips,
        }
    except Exception as e:
        return {"status": "error", "message": f"Profile audit failed: {type(e).__name__}: {e}"}
