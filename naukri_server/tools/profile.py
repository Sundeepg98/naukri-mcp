"""Profile tools — get, update, audit, boost, and dashboard."""

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
_profile_lock = asyncio.Lock()


async def get_cached_profile(ttl: int = _PROFILE_TTL) -> dict:
    """Return cached profile if fresh, otherwise fetch and cache.

    Used by composite tools (smart_apply, auto_hunt, compare, skill_gap,
    resume_tailor, daily_brief) to avoid redundant profile API calls.
    """
    now = _time.monotonic()
    if _profile_cache.get("data") and (now - _profile_cache.get("ts", 0)) < ttl:
        return _profile_cache["data"]
    async with _profile_lock:
        # Double-check after acquiring lock (another caller may have fetched)
        now = _time.monotonic()
        if _profile_cache.get("data") and (now - _profile_cache.get("ts", 0)) < ttl:
            return _profile_cache["data"]
        result = await _get_profile()
        if isinstance(result, dict) and result.get("status") != "error":
            _profile_cache["data"] = result
            _profile_cache["ts"] = now
        return result


# ---------------------------------------------------------------------------
# Browser helpers for profile update modals
# ---------------------------------------------------------------------------

_SAVE_MODAL_JS = """() => {
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
}"""


def _ctc_find_js(field_key: str, label_text: str) -> str:
    """Generate JS to find a CTC input field by name/id selectors or label fallback."""
    return f"""() => {{
        const selectors = [
            'input[name*="{field_key}"]', 'input[name*="{field_key[0].upper() + field_key[1:]}"]',
            'input[name*="{field_key.replace("Ctc", "_ctc")}"]', 'input[id*="{field_key}"]',
            'input[id*="{field_key[0].upper() + field_key[1:]}"]', 'input[id*="{field_key.replace("Ctc", "_ctc")}"]',
        ];
        for (const sel of selectors) {{
            const el = document.querySelector(sel);
            if (el) return sel;
        }}
        const labels = Array.from(document.querySelectorAll('label, span, div')).filter(el =>
            el.textContent.trim().toLowerCase().includes('{label_text.lower()}') &&
            el.children.length <= 2
        );
        for (const label of labels) {{
            const container = label.closest('div[class*="field"], div[class*="form"], div[class*="row"]');
            if (container) {{
                const input = container.querySelector('input[type="text"], input[type="number"], input:not([type])');
                if (input) {{
                    input.setAttribute('data-ctc-found', '{field_key}');
                    return 'input[data-ctc-found="{field_key}"]';
                }}
            }}
        }}
        return null;
    }}"""


async def _fill_ctc_input(page, field_key: str, value: str, label_text: str) -> str | None:
    """Find and fill a CTC input field in a modal. Returns field_key on success, None on failure."""
    ctc_input = await page.evaluate(_ctc_find_js(field_key, label_text))
    if not ctc_input:
        return None
    input_el = await page.query_selector(ctc_input)
    if not input_el:
        return None
    await input_el.click(click_count=3)
    await input_el.fill("")
    await input_el.fill(value)
    await input_el.dispatch_event("change")
    await asyncio.sleep(0.5)
    return field_key


async def _click_save_modal(page) -> str | None:
    """Click Save in the current modal. Returns save method or None."""
    return await page.evaluate(_SAVE_MODAL_JS)


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

UPDATABLE_FIELDS = {
    "resumeHeadline", "keySkills", "noticePeriod", "expectedCtc", "currentCtc",
    # These fields are accepted by the REST API but may not persist:
    # "summary", "locationPrefId", "experience", "absoluteCtc", "absoluteExpectedCtc",
    # "name", "gender", "maritalStatus", "dateOfBirth", "homeTown", "pinCode",
}

BROWSER_SUPPORTED_FIELDS = {"resumeHeadline", "keySkills", "noticePeriod", "expectedCtc", "currentCtc"}


async def _get_profile() -> dict:
    """Fetch full Naukri profile via API (internal helper)."""
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

        # --- Communication settings (notification preferences) ---
        comm_settings = data.get("communicationSettings") or data.get("communicationPreferences") or profile.get("communicationSettings")
        if comm_settings and isinstance(comm_settings, dict):
            result["communication_settings"] = {
                "email_notifications": comm_settings.get("emailNotifications") or comm_settings.get("emailAlerts"),
                "sms_notifications": comm_settings.get("smsNotifications") or comm_settings.get("smsAlerts"),
                "push_notifications": comm_settings.get("pushNotifications"),
                "job_alerts": comm_settings.get("jobAlerts") or comm_settings.get("jobAlert"),
                "recruiter_messages": comm_settings.get("recruiterMessages") or comm_settings.get("recruiterMail"),
                "promotional": comm_settings.get("promotional") or comm_settings.get("marketingMail"),
                "whatsapp_notifications": comm_settings.get("whatsappNotifications") or comm_settings.get("whatsappAlerts"),
            }
            # Strip None values
            result["communication_settings"] = {k: v for k, v in result["communication_settings"].items() if v is not None}
            if not result["communication_settings"]:
                del result["communication_settings"]

        # --- Resdex visibility (profile visibility flags) ---
        resdex = data.get("resdexVisibility") or data.get("profileVisibility") or additional.get("resdexVisibility") or profile.get("resdexVisibility")
        if resdex and isinstance(resdex, dict):
            result["resdex_visibility"] = {
                "show_profile": resdex.get("showProfile") or resdex.get("profileVisible"),
                "show_current_employer": resdex.get("showCurrentEmployer") or resdex.get("currentEmployerVisible"),
                "show_email": resdex.get("showEmail") or resdex.get("emailVisible"),
                "show_phone": resdex.get("showPhone") or resdex.get("mobileVisible"),
                "show_resume": resdex.get("showResume") or resdex.get("resumeVisible"),
                "hide_from_companies": resdex.get("hideFromCompanies") or resdex.get("blockedCompanies"),
                "search_visible": resdex.get("searchVisible") or resdex.get("isSearchable"),
                "active_job_seeker": resdex.get("activeJobSeeker") or resdex.get("isActivelySearching"),
            }
            # Strip None values
            result["resdex_visibility"] = {k: v for k, v in result["resdex_visibility"].items() if v is not None}
            if not result["resdex_visibility"]:
                del result["resdex_visibility"]
        elif resdex and isinstance(resdex, str):
            # Sometimes returned as a simple string like "visible" / "hidden"
            result["resdex_visibility"] = resdex

        warnings = validate_profile(result)
        if warnings:
            result["warnings"] = warnings
        return result
    except ValueError as e:
        return {"status": "error", "message": str(e), "error_code": "API_ERROR"}
    except Exception as e:
        return {"status": "error", "message": f"Profile API failed: {type(e).__name__}: {e!r}", "error_code": "API_ERROR"}


async def _boost_visibility(randomize: bool = False) -> dict:
    """Boost profile visibility by re-saving headline (internal helper)."""
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
                        _profile_cache.clear()
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
                return {"status": "error", "message": "Not logged in. Call naukri_login first.", "error_code": "AUTH_ERROR"}

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
                return {"status": "error", "message": "Could not find edit button on profile", "error_code": "BROWSER_ERROR"}

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

            _profile_cache.clear()
            return {
                "status": "refreshed",
                "method": f"browser_{edit_clicked}",
                "save": save_result,
                "api_confirmed": api_confirmed.get("status") == 200,
                "message": "Profile refreshed via browser. You appear as 'recently active'.",
            }
    except Exception as e:
        return {"status": "error", "message": f"Browser fallback failed: {type(e).__name__}: {e}", "error_code": "BROWSER_ERROR"}


async def _update_profile(
    fields: dict,
    notice_period: Optional[str] = None,
    expected_ctc: Optional[float] = None,
    current_ctc: Optional[float] = None,
) -> dict:
    """Update Naukri profile fields via browser UI automation (internal helper)."""
    # Merge convenience parameters into fields dict
    if notice_period is not None:
        fields["noticePeriod"] = notice_period
    if expected_ctc is not None:
        fields["expectedCtc"] = expected_ctc
    if current_ctc is not None:
        fields["currentCtc"] = current_ctc
    if not fields:
        return {"status": "error", "message": "No fields provided. Pass at least one field to update.", "error_code": "VALIDATION_ERROR"}

    unknown = set(fields.keys()) - UPDATABLE_FIELDS
    if unknown:
        return {
            "status": "error",
            "message": f"Unknown fields: {', '.join(sorted(unknown))}. "
                       f"Supported: {', '.join(sorted(UPDATABLE_FIELDS))}",
            "error_code": "VALIDATION_ERROR",
        }

    # The fullprofiles endpoint rejects all external API calls (405 from CDN).
    # Only the React app's own XHR works. Use the browser UI approach:
    # navigate to profile -> click edit -> modify form fields -> click save.
    async with browser.page_pool.acquire() as page:
        try:
            await page_goto(page, f"{NAUKRI_BASE}/mnjuser/profile")
            await asyncio.sleep(3)

            if "/nlogin" in page.url:
                return {"status": "error", "message": "Not logged in. Call naukri_login first.", "error_code": "AUTH_ERROR"}

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
                        return {"status": "error", "message": "Could not find edit button for resume headline", "error_code": "BROWSER_ERROR"}

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
                        return {"status": "error", "message": "Edit modal opened but textarea not found", "error_code": "NOT_FOUND"}

                    # Click save
                    save_result = await _click_save_modal(page)
                    if not save_result:
                        return {"status": "error", "message": "Edit modal opened but Save button not found", "error_code": "NOT_FOUND"}

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
                        return {"status": "error", "message": "Could not find edit button for key skills", "error_code": "BROWSER_ERROR"}

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
                            "error_code": "VALIDATION_ERROR",
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
                        return {"status": "error", "message": f"Skills updated in DOM but Save failed: {save_result}", "error_code": "BROWSER_ERROR"}

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
                        return {"status": "error", "message": f"Skills save validation failed: {post_save}", "error_code": "BROWSER_ERROR"}

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
                            "error_code": "BROWSER_ERROR",
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
                                    "error_code": "VALIDATION_ERROR",
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
                                        "error_code": "BROWSER_ERROR",
                                    }
                            elif notice_set == "select_set":
                                ui_updated.append("noticePeriod")
                            else:
                                return {
                                    "status": "error",
                                    "message": "Could not find notice period dropdown in Career Profile modal",
                                    "error_code": "BROWSER_ERROR",
                                }

                            await asyncio.sleep(0.5)
                        except Exception as e:
                            return {
                                "status": "error",
                                "message": f"Failed to update noticePeriod: {type(e).__name__}: {e}",
                                "error_code": "BROWSER_ERROR",
                            }

                    # --- Current CTC ---
                    if "currentCtc" in career_fields:
                        try:
                            filled = await _fill_ctc_input(page, "currentCtc", str(career_fields["currentCtc"]), "current ctc")
                            if filled:
                                ui_updated.append("currentCtc")
                            else:
                                return {"status": "error", "message": "Could not find Current CTC input in Career Profile modal", "error_code": "BROWSER_ERROR"}
                        except Exception as e:
                            return {"status": "error", "message": f"Failed to update currentCtc: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

                    # --- Expected CTC ---
                    if "expectedCtc" in career_fields:
                        try:
                            filled = await _fill_ctc_input(page, "expectedCtc", str(career_fields["expectedCtc"]), "expected ctc")
                            if filled:
                                ui_updated.append("expectedCtc")
                            else:
                                return {"status": "error", "message": "Could not find Expected CTC input in Career Profile modal", "error_code": "BROWSER_ERROR"}
                        except Exception as e:
                            return {"status": "error", "message": f"Failed to update expectedCtc: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

                    # Click save on the Career Profile modal
                    save_result = await _click_save_modal(page)
                    if not save_result:
                        return {"status": "error", "message": "Career Profile modal opened but Save button not found", "error_code": "NOT_FOUND"}

                    await asyncio.sleep(3)

                # Check for any remaining unsupported fields
                all_browser_handled = {"resumeHeadline", "keySkills", "noticePeriod", "expectedCtc", "currentCtc"}
                unsupported = [k for k in fields.keys() if k not in all_browser_handled]
                if unsupported and not ui_updated:
                    return {
                        "status": "error",
                        "message": f"Browser UI update not yet supported for: {', '.join(unsupported)}. "
                                   f"Currently supported: {', '.join(sorted(all_browser_handled))}",
                        "error_code": "VALIDATION_ERROR",
                    }

            finally:
                page.remove_listener("response", on_response)

            _profile_cache.clear()
            return {
                "status": "updated",
                "method": "browser_ui",
                "updated_fields": ui_updated,
                "api_confirmed": api_confirmed.get("status") == 200,
                "message": f"Profile updated via browser UI. Fields: {', '.join(ui_updated)}",
            }
        except Exception as e:
            return {"status": "error", "message": f"Profile update failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


async def _audit_profile() -> dict:
    """Audit profile and return actionable improvement suggestions (internal helper)."""
    try:
        # Get profile data
        profile_result = await _get_profile()
        if profile_result.get("status") != "success":
            return profile_result

        # Get completeness percentage
        from naukri_server.tools.assessments import _get_profile_completeness
        completeness_result = await _get_profile_completeness()
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
        tips.append("Use naukri_profile(action='boost') daily to stay in 'recently active' searches")
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
        return {"status": "error", "message": f"Profile audit failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_profile(
    action: str = "get",
    fields: Optional[dict] = None,
    notice_period: Optional[str] = None,
    expected_ctc: Optional[float] = None,
    current_ctc: Optional[float] = None,
    randomize: bool = False,
) -> dict:
    """Unified profile management — get, update, audit, or boost visibility.

    Actions:
      - "get": Fetch full profile (skills, employment, education, CTC, etc.)
      - "update": Update profile fields via browser UI (requires fields dict)
      - "audit": Audit profile completeness and get improvement suggestions
      - "boost": Re-save headline to appear as 'recently active' in recruiter searches

    Args:
        action: "get" | "update" | "audit" | "boost"
        fields: Required for update — dict of fields to change. Supported keys:
            resumeHeadline, keySkills, noticePeriod, expectedCtc, currentCtc
        notice_period: Shorthand for update — "Serving Notice Period", "15 Days or less",
            "1 Month", "2 Months", "3 Months", "More than 3 Months"
        expected_ctc: Shorthand for update — expected CTC in lakhs (e.g., 15)
        current_ctc: Shorthand for update — current CTC in lakhs (e.g., 12)
        randomize: For boost only — if True, wait random 0-300s before refreshing

    Returns:
        - get: {status, name, current_ctc, expected_ctc, skills_with_experience, employment, education, ...}
        - update: {status: "updated", updated_fields, method, api_confirmed, message}
        - audit: {status, completeness_pct, grade, strengths, gaps, tips}
        - boost: {status: "refreshed", method, message}
        - {status: "error", message} on failure
    """
    # ── get ─────────────────────────────────────────────────────────────
    if action == "get":
        return await _get_profile()

    # ── update ──────────────────────────────────────────────────────────
    elif action == "update":
        return await _update_profile(
            fields=fields or {},
            notice_period=notice_period,
            expected_ctc=expected_ctc,
            current_ctc=current_ctc,
        )

    # ── audit ───────────────────────────────────────────────────────────
    elif action == "audit":
        return await _audit_profile()

    # ── boost ───────────────────────────────────────────────────────────
    elif action == "boost":
        return await _boost_visibility(randomize=randomize)

    # ── unknown action ──────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: get, update, audit, boost", "error_code": "VALIDATION_ERROR"}


# ---------------------------------------------------------------------------
# Separate tool — different API, different purpose
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_get_dashboard() -> dict:
    """Get your Naukri dashboard summary via API.

    Returns profile views, recruiter activity, CTC, experience,
    recruiter invites, unread mail counts, application stats, profile
    completeness, resume details, saved jobs count, and more — a
    comprehensive health check of your Naukri presence.

    Returns:
        - {status: "success", profile_views, recruiter_activity_date, ctc_lpa, experience_years,
           unread_invites, total_invites, unread_relevant_mail, has_inbox, total_matches,
           applied_count, profile_completion, profile_score, saved_jobs_count,
           resume_name, resume_last_updated, profile_title, profile_image_url,
           designation, company, location, notice_period, preferred_locations,
           total_unread_mail, search_impressions, recruiter_actions_count,
           is_profile_verified, is_email_verified, is_phone_verified,
           subscription_type, subscription_expiry, job_alerts_count,
           pending_invites, accepted_invites, ...}
        - {status: "error", message}
    """
    try:
        data = await api_get(DASHBOARD_API)
        db = data.get("dashBoard", {})

        # --- Core fields (existing) ---
        result = {
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

        # --- Application stats ---
        result["applied_count"] = db.get("appliedCount") or db.get("totalApplied")
        result["applied_today"] = db.get("appliedToday")
        result["applied_this_week"] = db.get("appliedThisWeek")

        # --- Profile completeness and score ---
        result["profile_completion"] = db.get("profileCompletion") or db.get("completeness") or db.get("profileCompleteness")
        result["profile_score"] = db.get("profileScore") or db.get("profileRank")
        result["profile_quality"] = db.get("profileQuality")
        result["profile_strength"] = db.get("profileStrength")

        # --- Saved jobs ---
        result["saved_jobs_count"] = db.get("savedJobsCount") or db.get("totalSavedJobs")

        # --- Resume details ---
        resume = db.get("resumeDetails") or db.get("resume") or {}
        if isinstance(resume, dict):
            result["resume_name"] = resume.get("name") or resume.get("resumeName") or db.get("resumeName")
            result["resume_last_updated"] = resume.get("lastUpdated") or resume.get("modifiedDate") or db.get("resumeLastUpdated")
            result["resume_headline"] = resume.get("resumeHeadline") or db.get("resumeHeadline")
            result["resume_id"] = resume.get("resumeId") or resume.get("id")
        else:
            result["resume_name"] = db.get("resumeName")
            result["resume_last_updated"] = db.get("resumeLastUpdated")
            result["resume_headline"] = db.get("resumeHeadline")
            result["resume_id"] = None

        # --- User identity ---
        result["profile_title"] = db.get("profileTitle") or db.get("title")
        result["profile_image_url"] = db.get("profileImageUrl") or db.get("imageUrl") or db.get("photoUrl")
        result["name"] = db.get("name") or db.get("fullName")
        result["email"] = db.get("email") or db.get("emailId")
        result["phone"] = db.get("phone") or db.get("mobileNo")
        result["user_id"] = db.get("userId") or db.get("resId")

        # --- Current employment context ---
        result["designation"] = db.get("designation") or db.get("currentDesignation")
        result["company"] = db.get("company") or db.get("currentCompany")
        result["location"] = db.get("location") or db.get("city")
        result["notice_period"] = db.get("noticePeriod")
        result["preferred_locations"] = db.get("preferredLocations") or db.get("locationPref")

        # --- Mail / messaging ---
        result["total_unread_mail"] = db.get("totalUnreadMail") or db.get("unreadMailCount")
        result["unread_direct_mail"] = db.get("unreadDirectMail")
        result["unread_forwarded_mail"] = db.get("unreadForwardedMail")

        # --- Search and recruiter analytics ---
        result["search_impressions"] = db.get("searchImpressions") or db.get("searchAppearances") or db.get("searchCount")
        result["recruiter_actions_count"] = db.get("recruiterActionsCount") or db.get("totalRecruiterActions")
        result["profile_views_trend"] = db.get("profileViewsTrend") or db.get("viewsTrend")
        result["profile_views_last_week"] = db.get("profileViewsLastWeek")
        result["profile_views_last_month"] = db.get("profileViewsLastMonth")

        # --- Verification flags ---
        result["is_profile_verified"] = db.get("isProfileVerified") or db.get("profileVerified")
        result["is_email_verified"] = db.get("isEmailVerified") or db.get("emailVerified")
        result["is_phone_verified"] = db.get("isPhoneVerified") or db.get("mobileVerified")

        # --- Subscription ---
        result["subscription_type"] = db.get("subscriptionType") or db.get("productName")
        result["subscription_expiry"] = db.get("subscriptionExpiry") or db.get("subscriptionEndDate")
        result["is_premium"] = db.get("isPremium") or db.get("premiumUser")

        # --- Job alerts ---
        result["job_alerts_count"] = db.get("jobAlertsCount") or db.get("totalJobAlerts")

        # --- Invite breakdown ---
        result["pending_invites"] = db.get("pendingNvite") or db.get("pendingInvites")
        result["accepted_invites"] = db.get("acceptedNvite") or db.get("acceptedInvites")
        result["declined_invites"] = db.get("declinedNvite") or db.get("declinedInvites")

        # --- Resdex / visibility ---
        result["resdex_visible"] = db.get("resdexVisible") or db.get("showProfile")
        result["active_status"] = db.get("activeStatus") or db.get("activityStatus")
        result["last_active"] = db.get("lastActive") or db.get("lastLoginDate")

        # --- Recommendations ---
        result["recommended_jobs_count"] = db.get("recommendedJobsCount") or db.get("totalRecommendedJobs")

        # --- Strip None values to keep response clean ---
        result = {k: v for k, v in result.items() if v is not None}
        # Always include status
        result["status"] = "success"

        return result
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e), "error_code": "API_ERROR"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get dashboard: {type(e).__name__}: {e}", "error_code": "API_ERROR"}
