import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.browser import browser, page_goto
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import NAUKRI_BASE, DASHBOARD_API, PROFILE_API, FULLPROFILES_API, logger
from naukri_server.validation import validate_profile

import time as _time

# --- Profile TTL cache (30s) for composite tools ---
_profile_cache: dict = {}
_PROFILE_TTL = 30  # seconds


async def get_cached_profile(ttl: int = _PROFILE_TTL) -> dict:
    """Return cached profile if fresh, otherwise fetch and cache.

    Used by composite tools (smart_apply, auto_hunt, compare, skill_gap,
    resume_tailor, daily_brief) to avoid redundant profile API calls.
    """
    now = _time.monotonic()
    if _profile_cache.get("data") and (now - _profile_cache.get("ts", 0)) < ttl:
        return _profile_cache["data"]
    result = await naukri_get_profile()
    if isinstance(result, dict) and result.get("status") != "error":
        _profile_cache["data"] = result
        _profile_cache["ts"] = now
    return result


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
        - {status: "success", name, current_ctc, expected_ctc, notice_period, total_experience, skills_with_experience, employment, education, summary, certifications, projects, languages, online_profiles}
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
                "description": emp.get("description"),
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
            "resume_headline": profile.get("resumeHeadline"),
            "current_ctc": profile.get("absoluteCtc"),
            "expected_ctc": profile.get("absoluteExpectedCtc"),
            "notice_period": profile.get("noticePeriod", {}).get("value"),
            "total_experience": f"{exp.get('year', 0)} years {exp.get('month', 0)} months",
            "current_location": profile.get("city", {}).get("value"),
            "gender": {"M": "Male", "F": "Female"}.get(profile.get("gender"), profile.get("gender")),
            "key_skills": profile.get("keySkills"),
            "summary": profile.get("summary"),
            "certifications": data.get("certifications", []),
            "projects": data.get("projects", []),
            "languages": data.get("languages", []),
            "online_profiles": data.get("onlineProfiles", []),
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
    "resumeHeadline", "keySkills", "noticePeriod", "expectedCtc", "currentCtc",
    # These fields are accepted by the REST API but may not persist:
    # "summary", "locationPrefId", "experience", "absoluteCtc", "absoluteExpectedCtc",
    # "name", "gender", "maritalStatus", "dateOfBirth", "homeTown", "pinCode",
}

BROWSER_SUPPORTED_FIELDS = {"resumeHeadline", "keySkills", "noticePeriod", "expectedCtc", "currentCtc"}


@mcp.tool()
async def naukri_update_profile(
    fields: dict,
    notice_period: Optional[str] = None,
    expected_ctc: Optional[float] = None,
    current_ctc: Optional[float] = None,
) -> dict:
    """Update your Naukri profile fields via browser UI automation.

    Currently supported fields: resumeHeadline, keySkills, noticePeriod,
    expectedCtc, currentCtc (browser-based update).
    Other fields in UPDATABLE_FIELDS are accepted but may not persist —
    Naukri's API support for direct field updates varies.

    Use naukri_get_profile first to see current values.

    Args:
        fields: Dict of fields to update. Currently supported:
            - resumeHeadline: str — your profile headline
            - keySkills: str — comma-separated full skill set (replaces current skills, 250 char limit)
            Other fields in UPDATABLE_FIELDS are accepted but may not persist.
        notice_period: Notice period — "Serving Notice Period", "15 Days or less",
            "1 Month", "2 Months", "3 Months", "More than 3 Months"
        expected_ctc: Expected CTC in lakhs (e.g., 15 for 15 LPA)
        current_ctc: Current CTC in lakhs (e.g., 12 for 12 LPA)

    Returns:
        - {status: "updated", updated_fields: [...], method, api_confirmed, message}
        - {status: "error", message}
    """
    # Merge convenience parameters into fields dict
    if notice_period is not None:
        fields["noticePeriod"] = notice_period
    if expected_ctc is not None:
        fields["expectedCtc"] = expected_ctc
    if current_ctc is not None:
        fields["currentCtc"] = current_ctc
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

                    # Dismiss any chat/notification overlays blocking interaction
                    await page.evaluate("""() => {
                        for (const sel of ['.ltLayer', '.ltCont', '[class*="chatWidget"]',
                                           '[class*="ChatWidget"]', '[class*="livechat"]']) {
                            const el = document.querySelector(sel);
                            if (el) el.style.display = 'none';
                        }
                    }""")
                    await asyncio.sleep(0.5)

                    # Read current skills from the hidden #chipsVal input
                    current_skills_str = await page.evaluate("""() => {
                        const hidden = document.querySelector('#chipsVal');
                        return hidden ? hidden.value : '';
                    }""")
                    current_skills = [s.strip() for s in (current_skills_str or "").split(",") if s.strip()]
                    current_lower = {s.lower(): s for s in current_skills}

                    # Parse desired skills (full replacement set)
                    desired_skills = [s.strip() for s in fields["keySkills"].split(",") if s.strip()]
                    desired_lower = {s.lower(): s for s in desired_skills}

                    # Validate 250 character limit before doing anything
                    new_csv = ",".join(desired_skills)
                    if len(new_csv) > 250:
                        # Cancel the editor
                        await page.evaluate("""() => {
                            const lb = document.querySelector('.keySkillsEdit');
                            if (lb) {
                                const cancel = lb.querySelector('a');
                                if (cancel && cancel.textContent.trim() === 'Cancel') cancel.click();
                            }
                        }""")
                        return {
                            "status": "error",
                            "message": f"keySkills total length is {len(new_csv)} chars, exceeds 250 char limit. "
                                       f"Reduce skills or shorten names. Current CSV: {new_csv}",
                        }

                    # Determine removals and additions
                    to_remove = [current_lower[k] for k in current_lower if k not in desired_lower]
                    to_add = [desired_lower[k] for k in desired_lower if k not in current_lower]

                    logger.info(f"Skills update: current={current_skills}, desired={desired_skills}, "
                                f"removing={to_remove}, adding={to_add}")

                    # Remove skills: click the X on each chip to remove
                    for skill in to_remove:
                        removed = await page.evaluate("""(skillName) => {
                            const container = document.querySelector('.chipsContainer');
                            if (!container) return 'no_container';
                            const chips = container.querySelectorAll('.chip');
                            for (const chip of chips) {
                                const txt = chip.querySelector('span.tagTxt');
                                if (txt && txt.textContent.trim().toLowerCase() === skillName.toLowerCase()) {
                                    const closeBtn = chip.querySelector('a.close');
                                    if (closeBtn) { closeBtn.click(); return 'removed'; }
                                }
                            }
                            return 'not_found';
                        }""", skill)
                        logger.info(f"Remove skill '{skill}': {removed}")
                        await asyncio.sleep(0.3)

                    # Add skills: create chip DOM elements and update #chipsVal
                    if to_add:
                        add_result = await page.evaluate("""(newSkills) => {
                            const container = document.querySelector('.chipsContainer');
                            if (!container) return 'no_container';
                            const hiddenInput = document.querySelector('#chipsVal');
                            if (!hiddenInput) return 'no_hidden_input';

                            // Add chip DOM elements for each new skill
                            for (const skill of newSkills) {
                                const chip = document.createElement('div');
                                chip.className = 'waves-effect chip';
                                const dataId = skill + '_' + skill.toLowerCase().replace(/\\s+/g, '_');
                                chip.setAttribute('data-id', dataId);
                                chip.setAttribute('title', skill);

                                const span = document.createElement('span');
                                span.className = 'tagTxt';
                                span.textContent = skill;
                                chip.appendChild(span);

                                const closeLink = document.createElement('a');
                                closeLink.className = 'material-icons close';
                                closeLink.href = 'javascript:void(0)';
                                closeLink.textContent = 'Cross';
                                chip.appendChild(closeLink);

                                // Insert before the input wrapper
                                const inputWrap = container.querySelector('.input-wrap, .inputWrap, [class*="input"]');
                                if (inputWrap) {
                                    container.insertBefore(chip, inputWrap);
                                } else {
                                    container.appendChild(chip);
                                }
                            }

                            // Read current chips from DOM to rebuild the CSV
                            const allChips = container.querySelectorAll('.chip span.tagTxt');
                            const allSkills = [];
                            for (const c of allChips) {
                                const t = c.textContent.trim();
                                if (t) allSkills.push(t);
                            }
                            const newValue = allSkills.join(',');

                            // Use native setter to trigger React change detection
                            const nativeSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            ).set;
                            nativeSetter.call(hiddenInput, newValue);
                            hiddenInput.dispatchEvent(new Event('input', { bubbles: true }));
                            hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));

                            return 'added_' + newSkills.length;
                        }""", to_add)
                        logger.info(f"Add skills result: {add_result}")
                    else:
                        # Even if no additions, update #chipsVal to reflect removals
                        if to_remove:
                            await page.evaluate("""(newCsv) => {
                                const hiddenInput = document.querySelector('#chipsVal');
                                if (!hiddenInput) return;
                                const nativeSetter = Object.getOwnPropertyDescriptor(
                                    window.HTMLInputElement.prototype, 'value'
                                ).set;
                                nativeSetter.call(hiddenInput, newCsv);
                                hiddenInput.dispatchEvent(new Event('input', { bubbles: true }));
                                hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
                            }""", new_csv)

                    # Set form chk attribute to "true" so save is accepted
                    await page.evaluate("""() => {
                        const form = document.querySelector('form[name="keySkillsForm"]');
                        if (form) form.setAttribute('chk', 'true');
                    }""")

                    if to_remove or to_add:
                        ui_updated.append("keySkills")

                    # Click Save inside the .keySkillsEdit lightbox
                    save_result = await page.evaluate("""() => {
                        const lightbox = document.querySelector('.keySkillsEdit');
                        if (!lightbox) return 'no_lightbox';
                        const btns = lightbox.querySelectorAll('button');
                        for (const btn of btns) {
                            if (btn.textContent.trim() === 'Save') {
                                btn.click();
                                return 'save_clicked';
                            }
                        }
                        return 'no_save_button';
                    }""")

                    if save_result != "save_clicked":
                        return {"status": "error", "message": f"Skills updated in DOM but Save failed: {save_result}"}

                    await asyncio.sleep(3)

                    # Verify: check if the editor closed (success) or validation error appeared
                    post_save = await page.evaluate("""() => {
                        const lightbox = document.querySelector('.keySkillsEdit');
                        if (!lightbox) return 'closed';
                        const style = window.getComputedStyle(lightbox);
                        if (style.display === 'none') return 'closed';
                        // Check for validation error message
                        const errEl = lightbox.querySelector('.error, .errorMsg, [class*="error"]');
                        if (errEl && errEl.textContent.trim()) return 'error: ' + errEl.textContent.trim();
                        return 'still_open';
                    }""")
                    logger.info(f"Skills save post-check: {post_save}")

                    if post_save.startswith("error:"):
                        return {"status": "error", "message": f"Skills save validation failed: {post_save}"}

                # --- Notice Period / Expected CTC / Current CTC (Career Profile section) ---
                # These three fields share the same "Career profile" edit modal on Naukri.
                # We open the modal once and update whichever fields are requested.
                # NOTE: Selectors are best-effort based on common Naukri UI patterns;
                # they may need adjustment based on live browser testing.
                career_fields = {
                    k: fields[k] for k in ("noticePeriod", "expectedCtc", "currentCtc")
                    if k in fields
                }
                if career_fields:
                    # Open the Career Profile edit modal
                    edit_clicked = await page.evaluate("""() => {
                        const editIcons = Array.from(document.querySelectorAll('*')).filter(el =>
                            el.children.length === 0 && el.textContent.trim() === 'editOneTheme'
                        );
                        // Strategy 1: find edit icon near "Career profile" or "Notice period" text
                        for (const icon of editIcons) {
                            const parent = icon.closest('div, section');
                            if (parent && (
                                parent.textContent.includes('Career profile') ||
                                parent.textContent.includes('career profile') ||
                                parent.textContent.includes('Notice period') ||
                                parent.textContent.includes('notice period') ||
                                parent.textContent.includes('Current CTC') ||
                                parent.textContent.includes('Expected CTC')
                            )) {
                                icon.click();
                                return 'career_profile_edit';
                            }
                        }
                        // Strategy 2: look for section with class containing "careerProfile"
                        for (const icon of editIcons) {
                            const section = icon.closest('[class*="careerProfile"], [class*="career-profile"], [class*="CareerProfile"]');
                            if (section) { icon.click(); return 'career_section_edit'; }
                        }
                        return null;
                    }""")

                    if not edit_clicked:
                        return {
                            "status": "error",
                            "message": "Could not find edit button for Career Profile section "
                                       "(needed for noticePeriod/expectedCtc/currentCtc)",
                        }

                    await asyncio.sleep(2)

                    # --- Notice Period ---
                    if "noticePeriod" in career_fields:
                        try:
                            VALID_NOTICE_PERIODS = {
                                "Serving Notice Period",
                                "15 Days or less",
                                "1 Month",
                                "2 Months",
                                "3 Months",
                                "More than 3 Months",
                            }
                            notice_val = career_fields["noticePeriod"]
                            if notice_val not in VALID_NOTICE_PERIODS:
                                return {
                                    "status": "error",
                                    "message": f"Invalid noticePeriod: '{notice_val}'. "
                                               f"Valid options: {', '.join(sorted(VALID_NOTICE_PERIODS))}",
                                }

                            # Try to find and set the notice period dropdown/select
                            notice_set = await page.evaluate("""(targetValue) => {
                                // Strategy 1: native <select> element
                                const selectors = [
                                    'select[name*="notice"]', 'select[name*="Notice"]',
                                    '[class*="notice"] select', '[class*="Notice"] select',
                                    '#noticePeriod', 'select[id*="notice"]', 'select[id*="Notice"]'
                                ];
                                for (const sel of selectors) {
                                    const select = document.querySelector(sel);
                                    if (select) {
                                        // Find matching option
                                        const options = Array.from(select.options);
                                        const match = options.find(o =>
                                            o.text.trim() === targetValue || o.value === targetValue
                                        );
                                        if (match) {
                                            select.value = match.value;
                                            select.dispatchEvent(new Event('change', {bubbles: true}));
                                            return 'select_set';
                                        }
                                    }
                                }

                                // Strategy 2: custom dropdown (React/Naukri style)
                                // Find the notice period label, then the dropdown trigger near it
                                const labels = Array.from(document.querySelectorAll('label, span, div')).filter(el =>
                                    el.textContent.trim().toLowerCase().includes('notice period') &&
                                    el.children.length <= 2
                                );
                                for (const label of labels) {
                                    const container = label.closest('div[class*="field"], div[class*="form"], div[class*="row"]');
                                    if (container) {
                                        // Look for a clickable dropdown trigger
                                        const trigger = container.querySelector(
                                            '[class*="dropdown"], [class*="select"], [class*="Dropdown"], ' +
                                            '[class*="Select"], [role="listbox"], [role="combobox"]'
                                        );
                                        if (trigger) {
                                            trigger.click();
                                            return 'custom_dropdown_clicked';
                                        }
                                    }
                                }
                                return null;
                            }""", career_fields["noticePeriod"])

                            if notice_set == "custom_dropdown_clicked":
                                # Wait for dropdown options to appear, then click the matching one
                                await asyncio.sleep(1)
                                option_clicked = await page.evaluate("""(targetValue) => {
                                    // Look for dropdown options/list items
                                    const items = Array.from(document.querySelectorAll(
                                        '[class*="option"], [class*="Option"], [role="option"], ' +
                                        'li[class*="dropdown"], li[class*="Dropdown"], ' +
                                        'ul[class*="dropdown"] li, ul[class*="Dropdown"] li, ' +
                                        '[class*="listItem"], [class*="list-item"]'
                                    ));
                                    const match = items.find(item =>
                                        item.textContent.trim() === targetValue
                                    );
                                    if (match) { match.click(); return 'option_selected'; }

                                    // Broader search: any visible element with exact text
                                    const allVisible = Array.from(document.querySelectorAll('*')).filter(el =>
                                        el.offsetParent !== null &&
                                        el.children.length === 0 &&
                                        el.textContent.trim() === targetValue
                                    );
                                    if (allVisible.length > 0) {
                                        allVisible[0].click();
                                        return 'text_match_selected';
                                    }
                                    return null;
                                }""", career_fields["noticePeriod"])

                                if option_clicked:
                                    ui_updated.append("noticePeriod")
                                else:
                                    return {
                                        "status": "error",
                                        "message": f"Notice period dropdown opened but could not select '{notice_val}'",
                                    }
                            elif notice_set == "select_set":
                                ui_updated.append("noticePeriod")
                            else:
                                return {
                                    "status": "error",
                                    "message": "Could not find notice period dropdown in Career Profile modal",
                                }

                            await asyncio.sleep(0.5)
                        except Exception as e:
                            return {
                                "status": "error",
                                "message": f"Failed to update noticePeriod: {type(e).__name__}: {e}",
                            }

                    # --- Current CTC ---
                    if "currentCtc" in career_fields:
                        try:
                            ctc_val = str(career_fields["currentCtc"])
                            ctc_input = await page.evaluate("""() => {
                                // Try specific selectors for current CTC input
                                const selectors = [
                                    'input[name*="currentCtc"]', 'input[name*="CurrentCtc"]',
                                    'input[name*="current_ctc"]', 'input[id*="currentCtc"]',
                                    'input[id*="CurrentCtc"]', 'input[id*="current_ctc"]',
                                ];
                                for (const sel of selectors) {
                                    const el = document.querySelector(sel);
                                    if (el) return sel;
                                }

                                // Fallback: find label "Current CTC" and get nearby input
                                const labels = Array.from(document.querySelectorAll('label, span, div')).filter(el =>
                                    el.textContent.trim().toLowerCase().includes('current ctc') &&
                                    el.children.length <= 2
                                );
                                for (const label of labels) {
                                    const container = label.closest('div[class*="field"], div[class*="form"], div[class*="row"]');
                                    if (container) {
                                        const input = container.querySelector('input[type="text"], input[type="number"], input:not([type])');
                                        if (input) {
                                            input.setAttribute('data-ctc-found', 'current');
                                            return 'input[data-ctc-found="current"]';
                                        }
                                    }
                                }
                                return null;
                            }""")

                            if ctc_input:
                                input_el = await page.query_selector(ctc_input)
                                if input_el:
                                    await input_el.click(click_count=3)  # Select all existing text
                                    await input_el.fill("")
                                    await input_el.fill(ctc_val)
                                    await input_el.dispatch_event("change")
                                    await asyncio.sleep(0.5)
                                    ui_updated.append("currentCtc")
                                else:
                                    return {"status": "error", "message": "Current CTC input found in DOM but not queryable"}
                            else:
                                return {"status": "error", "message": "Could not find Current CTC input in Career Profile modal"}
                        except Exception as e:
                            return {
                                "status": "error",
                                "message": f"Failed to update currentCtc: {type(e).__name__}: {e}",
                            }

                    # --- Expected CTC ---
                    if "expectedCtc" in career_fields:
                        try:
                            ctc_val = str(career_fields["expectedCtc"])
                            ctc_input = await page.evaluate("""() => {
                                // Try specific selectors for expected CTC input
                                const selectors = [
                                    'input[name*="expectedCtc"]', 'input[name*="ExpectedCtc"]',
                                    'input[name*="expected_ctc"]', 'input[id*="expectedCtc"]',
                                    'input[id*="ExpectedCtc"]', 'input[id*="expected_ctc"]',
                                ];
                                for (const sel of selectors) {
                                    const el = document.querySelector(sel);
                                    if (el) return sel;
                                }

                                // Fallback: find label "Expected CTC" and get nearby input
                                const labels = Array.from(document.querySelectorAll('label, span, div')).filter(el =>
                                    el.textContent.trim().toLowerCase().includes('expected ctc') &&
                                    el.children.length <= 2
                                );
                                for (const label of labels) {
                                    const container = label.closest('div[class*="field"], div[class*="form"], div[class*="row"]');
                                    if (container) {
                                        const input = container.querySelector('input[type="text"], input[type="number"], input:not([type])');
                                        if (input) {
                                            input.setAttribute('data-ctc-found', 'expected');
                                            return 'input[data-ctc-found="expected"]';
                                        }
                                    }
                                }
                                return null;
                            }""")

                            if ctc_input:
                                input_el = await page.query_selector(ctc_input)
                                if input_el:
                                    await input_el.click(click_count=3)  # Select all existing text
                                    await input_el.fill("")
                                    await input_el.fill(ctc_val)
                                    await input_el.dispatch_event("change")
                                    await asyncio.sleep(0.5)
                                    ui_updated.append("expectedCtc")
                                else:
                                    return {"status": "error", "message": "Expected CTC input found in DOM but not queryable"}
                            else:
                                return {"status": "error", "message": "Could not find Expected CTC input in Career Profile modal"}
                        except Exception as e:
                            return {
                                "status": "error",
                                "message": f"Failed to update expectedCtc: {type(e).__name__}: {e}",
                            }

                    # Click save on the Career Profile modal
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
                        return {"status": "error", "message": "Career Profile modal opened but Save button not found"}

                    await asyncio.sleep(3)

                # Check for any remaining unsupported fields
                all_browser_handled = {"resumeHeadline", "keySkills", "noticePeriod", "expectedCtc", "currentCtc"}
                unsupported = [k for k in fields.keys() if k not in all_browser_handled]
                if unsupported and not ui_updated:
                    return {
                        "status": "error",
                        "message": f"Browser UI update not yet supported for: {', '.join(unsupported)}. "
                                   f"Currently supported: {', '.join(sorted(all_browser_handled))}",
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
