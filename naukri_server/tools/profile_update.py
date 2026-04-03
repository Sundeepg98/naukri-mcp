"""Profile update tools — browser mutations, boost, and field handlers."""

import asyncio
from typing import Optional

from naukri_server.browser import browser, page_goto
from naukri_server.api import api_get, api_post
from naukri_server.config import (
    NAUKRI_BASE, PROFILE_API, FULLPROFILES_API,
    BROWSER_OPERATION_TIMEOUT, logger,
    BROWSER_DOM_SETTLE, BROWSER_MODAL_APPEAR, BROWSER_FORM_SAVE, BROWSER_PAGE_LOAD,
    BROWSER_PAGE_SETTLE,
)


# ---------------------------------------------------------------------------
# Browser helpers for profile update modals
# ---------------------------------------------------------------------------

_SAVE_MODAL_JS = """() => {
    // Strategy 1: stable structural selectors (preferred)
    var save = document.querySelector('.profileEditDrawer button[type="submit"]')
            || document.querySelector('.keySkillsEdit button[type="submit"]');
    if (save) { save.click(); return 'structural'; }
    // Strategy 2: text matching inside modal (fallback)
    const modal = document.querySelector('[class*="modal"], [class*="dialog"], [class*="overlay"], [role="dialog"]');
    if (modal) {
        const btn = Array.from(modal.querySelectorAll('button')).find(
            b => b.textContent.trim().toLowerCase() === 'save'
        );
        if (btn) { btn.click(); return 'modal_save'; }
    }
    // Strategy 3: visible Save button anywhere (last resort)
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
    await asyncio.sleep(BROWSER_DOM_SETTLE)
    return field_key


async def _click_save_modal(page) -> str | None:
    """Click Save in the current modal. Returns save method or None."""
    return await page.evaluate(_SAVE_MODAL_JS)


# ---------------------------------------------------------------------------
# Updatable field sets
# ---------------------------------------------------------------------------

UPDATABLE_FIELDS = {
    "resumeHeadline", "keySkills", "noticePeriod", "expectedCtc", "currentCtc",
    # These fields are accepted by the REST API but may not persist:
    # "summary", "locationPrefId", "experience", "absoluteCtc", "absoluteExpectedCtc",
    # "name", "gender", "maritalStatus", "dateOfBirth", "homeTown", "pinCode",
}

BROWSER_SUPPORTED_FIELDS = {"resumeHeadline", "keySkills", "noticePeriod", "expectedCtc", "currentCtc"}


# ---------------------------------------------------------------------------
# Field-specific update handlers
# ---------------------------------------------------------------------------

async def _update_headline(page, value) -> dict:
    """Update resume headline via browser UI. Operates on an already-loaded profile page."""
    try:
        edit_clicked = await page.evaluate("""() => {
            // Strategy 1: stable structural selector (preferred)
            var editBtn = document.querySelector('.resumeHeadline .edit');
            if (editBtn) { editBtn.click(); return 'headline_edit'; }
            // Strategy 2: text matching (fallback)
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

        await asyncio.sleep(BROWSER_MODAL_APPEAR)

        # Find textarea in the modal and fill with new headline
        textarea = await page.query_selector(
            '[class*="modal"] textarea, [class*="dialog"] textarea, '
            '[role="dialog"] textarea, textarea[class*="headline"], '
            'textarea'
        )
        if not textarea:
            return {"status": "error", "message": "Edit modal opened but textarea not found", "error_code": "NOT_FOUND"}

        await textarea.click()
        await textarea.fill("")
        await textarea.fill(value)
        await asyncio.sleep(BROWSER_DOM_SETTLE)

        # Click save
        save_result = await _click_save_modal(page)
        if not save_result:
            return {"status": "error", "message": "Edit modal opened but Save button not found", "error_code": "NOT_FOUND"}

        await asyncio.sleep(BROWSER_FORM_SAVE)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to update resumeHeadline: {type(e).__name__}: {e}", "error_code": "BROWSER_ERROR"}


async def _update_key_skills(page, value) -> dict:
    """Update key skills via browser UI. Operates on an already-loaded profile page."""
    try:
        # Navigate to key skills section and click edit
        edit_clicked = await page.evaluate("""() => {
            // Strategy 1: stable structural selector (preferred)
            var editBtn = document.querySelector('.keySkills .edit');
            if (editBtn) { editBtn.click(); return 'skills_edit'; }
            // Strategy 2: text matching (fallback)
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

        await asyncio.sleep(BROWSER_MODAL_APPEAR)

        # Dismiss any chat/notification overlays blocking interaction
        await page.evaluate("""() => {
            for (const sel of ['.ltLayer', '.ltCont', '[class*="chatWidget"]',
                               '[class*="ChatWidget"]', '[class*="livechat"]']) {
                const el = document.querySelector(sel);
                if (el) el.style.display = 'none';
            }
        }""")
        await asyncio.sleep(BROWSER_DOM_SETTLE)

        # Read current skills from the hidden #chipsVal input
        current_skills_str = await page.evaluate("""() => {
            const hidden = document.querySelector('#chipsVal');
            return hidden ? hidden.value : '';
        }""")
        current_skills = [s.strip() for s in (current_skills_str or "").split(",") if s.strip()]
        current_lower = {s.lower(): s for s in current_skills}

        # Parse desired skills (full replacement set)
        desired_skills = [s.strip() for s in value.split(",") if s.strip()]
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
            await asyncio.sleep(BROWSER_DOM_SETTLE)

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

        changed = bool(to_remove or to_add)

        # Click Save inside the .keySkillsEdit lightbox
        save_result = await page.evaluate("""() => {
            const lightbox = document.querySelector('.keySkillsEdit');
            if (!lightbox) return 'no_lightbox';
            // Strategy 1: stable structural selector (preferred)
            var save = lightbox.querySelector('button[type="submit"]');
            if (save) { save.click(); return 'save_clicked'; }
            // Strategy 2: text matching (fallback)
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

        await asyncio.sleep(BROWSER_FORM_SAVE)

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

        return {"status": "success", "changed": changed}
    except Exception as e:
        return {"status": "error", "message": f"Failed to update keySkills: {type(e).__name__}: {e}", "error_code": "BROWSER_ERROR"}


async def _update_notice_period(page, value) -> dict:
    """Update notice period inside an already-open Career Profile modal."""
    try:
        VALID_NOTICE_PERIODS = {
            "Serving Notice Period",
            "15 Days or less",
            "1 Month",
            "2 Months",
            "3 Months",
            "More than 3 Months",
        }
        if value not in VALID_NOTICE_PERIODS:
            return {
                "status": "error",
                "message": f"Invalid noticePeriod: '{value}'. "
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
        }""", value)

        if notice_set == "custom_dropdown_clicked":
            # Wait for dropdown options to appear, then click the matching one
            await asyncio.sleep(BROWSER_PAGE_SETTLE)
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
            }""", value)

            if not option_clicked:
                return {
                    "status": "error",
                    "message": f"Notice period dropdown opened but could not select '{value}'",
                    "error_code": "BROWSER_ERROR",
                }
        elif notice_set == "select_set":
            pass  # success via native select
        else:
            return {
                "status": "error",
                "message": "Could not find notice period dropdown in Career Profile modal",
                "error_code": "BROWSER_ERROR",
            }

        await asyncio.sleep(BROWSER_DOM_SETTLE)
        return {"status": "success"}
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to update noticePeriod: {type(e).__name__}: {e}",
            "error_code": "BROWSER_ERROR",
        }


async def _update_current_ctc(page, value) -> dict:
    """Update current CTC inside an already-open Career Profile modal."""
    try:
        filled = await _fill_ctc_input(page, "currentCtc", str(value), "current ctc")
        if filled:
            return {"status": "success"}
        return {"status": "error", "message": "Could not find Current CTC input in Career Profile modal", "error_code": "BROWSER_ERROR"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to update currentCtc: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


async def _update_expected_ctc(page, value) -> dict:
    """Update expected CTC inside an already-open Career Profile modal."""
    try:
        filled = await _fill_ctc_input(page, "expectedCtc", str(value), "expected ctc")
        if filled:
            return {"status": "success"}
        return {"status": "error", "message": "Could not find Expected CTC input in Career Profile modal", "error_code": "BROWSER_ERROR"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to update expectedCtc: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


# Handler dispatch tables
_STANDALONE_HANDLERS = {
    "resumeHeadline": _update_headline,
    "keySkills": _update_key_skills,
}

_CAREER_FIELD_HANDLERS = {
    "noticePeriod": _update_notice_period,
    "expectedCtc": _update_expected_ctc,
    "currentCtc": _update_current_ctc,
}

_FIELD_HANDLERS = {**_STANDALONE_HANDLERS, **_CAREER_FIELD_HANDLERS}


async def _open_career_profile_modal(page) -> str | None:
    """Open the Career Profile edit modal. Returns edit method or None."""
    edit_clicked = await page.evaluate("""() => {
        // Strategy 1: stable structural selector (preferred)
        var editBtn = document.querySelector('.dashboard-component .icon.edit');
        if (editBtn) { editBtn.click(); return 'career_profile_edit'; }
        // Strategy 2: text matching — find edit icon near "Career profile" text (fallback)
        const editIcons = Array.from(document.querySelectorAll('*')).filter(el =>
            el.children.length === 0 && el.textContent.trim() === 'editOneTheme'
        );
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
        // Strategy 3: look for section with class containing "careerProfile"
        for (const icon of editIcons) {
            const section = icon.closest('[class*="careerProfile"], [class*="career-profile"], [class*="CareerProfile"]');
            if (section) { icon.click(); return 'career_section_edit'; }
        }
        return null;
    }""")
    return edit_clicked


async def _update_profile(
    fields: dict,
    notice_period: Optional[str] = None,
    expected_ctc: Optional[float] = None,
    current_ctc: Optional[float] = None,
) -> dict:
    """Update Naukri profile fields via browser UI automation (internal helper).

    Dispatches to field-specific handlers:
      - _update_headline / _update_key_skills: standalone modal per field
      - _update_notice_period / _update_current_ctc / _update_expected_ctc:
        share the Career Profile modal (opened once, saved once)
    """
    # Import caches here to avoid circular imports
    from naukri_server.tools.profile import _profile_ttl_cache, _dashboard_ttl_cache

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
            await asyncio.sleep(BROWSER_PAGE_LOAD)

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
                # --- Standalone fields (each opens and saves its own modal) ---
                for field_name in ("resumeHeadline", "keySkills"):
                    if field_name not in fields:
                        continue
                    handler = _STANDALONE_HANDLERS[field_name]
                    result = await handler(page, fields[field_name])
                    if result.get("status") == "error":
                        return result
                    if result.get("status") == "success":
                        # keySkills handler tracks whether anything actually changed
                        if field_name == "keySkills" and not result.get("changed", True):
                            pass  # no-op update, don't append
                        else:
                            ui_updated.append(field_name)

                # --- Career profile fields (share one modal) ---
                career_fields = {
                    k: fields[k] for k in ("noticePeriod", "expectedCtc", "currentCtc")
                    if k in fields
                }
                if career_fields:
                    edit_clicked = await _open_career_profile_modal(page)
                    if not edit_clicked:
                        return {
                            "status": "error",
                            "message": "Could not find edit button for Career Profile section "
                                       "(needed for noticePeriod/expectedCtc/currentCtc)",
                            "error_code": "BROWSER_ERROR",
                        }
                    await asyncio.sleep(BROWSER_MODAL_APPEAR)

                    for field_name in ("noticePeriod", "currentCtc", "expectedCtc"):
                        if field_name not in career_fields:
                            continue
                        handler = _CAREER_FIELD_HANDLERS[field_name]
                        result = await handler(page, career_fields[field_name])
                        if result.get("status") == "error":
                            return result
                        if result.get("status") == "success":
                            ui_updated.append(field_name)

                    # Click save on the Career Profile modal (shared save)
                    save_result = await _click_save_modal(page)
                    if not save_result:
                        return {"status": "error", "message": "Career Profile modal opened but Save button not found", "error_code": "NOT_FOUND"}
                    await asyncio.sleep(BROWSER_FORM_SAVE)

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

            _profile_ttl_cache.invalidate()
            _dashboard_ttl_cache.invalidate()
            return {
                "status": "updated",
                "method": "browser_ui",
                "updated_fields": ui_updated,
                "api_confirmed": api_confirmed.get("status") == 200,
                "message": f"Profile updated via browser UI. Fields: {', '.join(ui_updated)}",
            }
        except Exception as e:
            return {"status": "error", "message": f"Profile update failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


async def _boost_visibility(randomize: bool = False) -> dict:
    """Boost profile visibility by re-saving headline (internal helper)."""
    # Import caches here to avoid circular imports
    from naukri_server.tools.profile import _profile_ttl_cache, _dashboard_ttl_cache

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
                        _profile_ttl_cache.invalidate()
                        _dashboard_ttl_cache.invalidate()
                        return {
                            "status": "success", "action": "refreshed",
                            "method": f"rest_api_{version}",
                            "headline_length": len(headline),
                            "message": f"Profile refreshed via REST API ({version}). You appear as 'recently active'.",
                        }
                    except Exception as e:
                        logger.debug("Profile REST refresh %s: %s", version, e)
                        continue
    except Exception as e:
        logger.warning("REST refresh failed, falling back to browser: %s", e)

    # Strategy 2: Browser fallback (original approach)
    try:
        async with browser.page_pool.acquire() as page:
            await page_goto(page, f"{NAUKRI_BASE}/mnjuser/profile")
            await asyncio.sleep(BROWSER_PAGE_LOAD)

            if "/nlogin" in page.url:
                return {"status": "error", "message": "Not logged in. Call naukri_login first.", "error_code": "AUTH_ERROR"}

            edit_clicked = await page.evaluate("""() => {
                // Strategy 1: stable structural selector (preferred)
                var editBtn = document.querySelector('.resumeHeadline .edit');
                if (editBtn) { editBtn.click(); return 'headline_edit'; }
                // Strategy 2: text matching (fallback)
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

            await asyncio.sleep(BROWSER_MODAL_APPEAR)

            api_confirmed = {}

            async def on_response(response):
                if "fullprofiles" in response.url and response.request.method == "POST":
                    api_confirmed["status"] = response.status

            page.on("response", on_response)

            try:
                save_result = await page.evaluate(_SAVE_MODAL_JS)

                if not save_result:
                    return {"status": "partial", "method": edit_clicked, "message": "Edit opened but Save not found."}

                await asyncio.sleep(BROWSER_FORM_SAVE)
            finally:
                page.remove_listener("response", on_response)

            _profile_ttl_cache.invalidate()
            _dashboard_ttl_cache.invalidate()
            return {
                "status": "success", "action": "refreshed",
                "method": f"browser_{edit_clicked}",
                "save": save_result,
                "api_confirmed": api_confirmed.get("status") == 200,
                "message": "Profile refreshed via browser. You appear as 'recently active'.",
            }
    except Exception as e:
        return {"status": "error", "message": f"Browser fallback failed: {type(e).__name__}: {e}", "error_code": "BROWSER_ERROR"}
