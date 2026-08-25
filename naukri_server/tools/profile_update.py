"""Profile update tools — browser mutations, boost, and field handlers."""

import asyncio
from typing import Optional

from naukri_server.browser import browser, page_goto
from naukri_server.interfaces import api_client
from naukri_server.config import (
    NAUKRI_BASE, PROFILE_API,
    BROWSER_OPERATION_TIMEOUT, logger,
    BROWSER_DOM_SETTLE, BROWSER_MODAL_APPEAR, BROWSER_FORM_SAVE, BROWSER_PAGE_LOAD,
    BROWSER_PAGE_SETTLE,
)
from naukri_server.tools.profile_sections import (
    SCALAR_ALLOWED_FIELDS, write_section,
)
from naukri_server.tools.profile_write import fullprofiles_write


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
# Updatable field sets -- TWO ROUTES BEHIND ONE FRONT DOOR
#
# What this tool refused, and why the refusal was stale
# -----------------------------------------------------
# Until this change `UPDATABLE_FIELDS` held five names and the tool rejected
# every other field at the front door. A second refusal further down
# ("Browser UI update not yet supported for: ...") could never fire at all:
# it caught fields that were not browser-handled, but the front-door gate had
# already rejected everything outside the browser-handled five, so the set it
# tested was provably empty. The reachable refusal was the FIELD SET, not the
# message.
#
# And the field set was stale against this repo's own capability.
# ``tools/profile_write.py`` owns a working fullprofiles v1 write route and
# ``tools/profile_sections.py`` writes SIXTEEN sections through it. Four of
# those are scalar-field sections, and between them they own 15 writable
# profile fields -- 13 of which this tool refused.
#
# THE SPLIT, and why it is not arbitrary
# --------------------------------------
# BROWSER_SUPPORTED_FIELDS keep the browser modal as their route of record.
# Nothing about their behaviour changes here. Three of them -- noticePeriod,
# currentCtc, expectedCtc -- have no alternative: no SectionSpec in
# profile_sections.py owns them, so the browser modal is the ONLY measured
# route this server has to those three. The other two (resumeHeadline,
# keySkills) exist on both routes, and stay on the browser one because the
# keySkills handler carries chip-merge and 250-char validation the REST body
# does not, and because a working path is not worth re-routing for symmetry.
#
# REST_SCALAR_FIELDS is DERIVED from the section layer's own spec table rather
# than re-typed here, so this set cannot drift away from it: add a field there
# and this tool can write it, delete one and this tool stops offering it.
# ---------------------------------------------------------------------------

#: Fields whose route of record is the browser edit modal. Unchanged.
BROWSER_SUPPORTED_FIELDS = {"resumeHeadline", "keySkills", "noticePeriod", "expectedCtc", "currentCtc"}

#: field -> the scalar section that owns it. Derived, never re-typed.
REST_FIELD_SECTION = {
    field: section
    for section, owned in SCALAR_ALLOWED_FIELDS.items()
    for field in owned
}

#: Every field reachable through the verified REST section write.
REST_SCALAR_FIELDS = frozenset(REST_FIELD_SECTION)

#: Fields whose ONLY measured route is the browser modal.
BROWSER_ONLY_FIELDS = frozenset(BROWSER_SUPPORTED_FIELDS - REST_SCALAR_FIELDS)

UPDATABLE_FIELDS = set(BROWSER_SUPPORTED_FIELDS) | set(REST_SCALAR_FIELDS)

#: STILL REFUSED, on purpose. These nine were carried in this file as a
#: commented-out list annotated "accepted by the REST API but may not
#: persist". They are refused rather than sent, and this is the measurement
#: behind that: no SectionSpec in tools/profile_sections.py owns any of them,
#: and that spec table is traced field by field to Naukri's own shipped editor
#: bundle mnj_v320.min.js with byte offsets. A field the editor never sends has
#: no measured write route, and this module will not invent one. The note they
#: arrived with says the rest out loud -- "accepted ... but may not persist" is
#: exactly a write that would report success without having happened, which is
#: the one outcome every rail in this subsystem exists to prevent.
NO_WRITE_ROUTE_FIELDS = frozenset({
    "locationPrefId", "experience", "absoluteCtc", "name", "gender",
    "maritalStatus", "dateOfBirth", "homeTown", "pinCode",
})

NO_WRITE_ROUTE_REASON = (
    "no SectionSpec in tools/profile_sections.py owns it. That spec table is "
    "traced field by field to Naukri's shipped editor bundle mnj_v320.min.js, "
    "so a field the editor never sends has no measured write route and this "
    "server will not guess one. These were previously recorded here as "
    "'accepted by the REST API but may not persist' -- accepted-but-not-"
    "persisted is a write that reports success without happening."
)


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


# ---------------------------------------------------------------------------
# The REST leg: scalar sections written through the verified section layer
# ---------------------------------------------------------------------------

def _reject_unknown(unknown: set) -> dict:
    """A VALIDATION_ERROR that says WHY, per field, not just "unknown".

    Two different things get rejected here and they deserve different answers:
    a field this server measured and DECLINED to write (see
    ``NO_WRITE_ROUTE_FIELDS``), and a name that is simply not a profile field.
    Collapsing both into "Unknown fields:" is what made the previous refusal
    unreadable -- a caller could not tell a typo from a policy.
    """
    declined = sorted(f for f in unknown if f in NO_WRITE_ROUTE_FIELDS)
    unrecognised = sorted(f for f in unknown if f not in NO_WRITE_ROUTE_FIELDS)

    parts = []
    if declined:
        parts.append(
            "Refused on purpose: %s -- %s"
            % (", ".join(declined), NO_WRITE_ROUTE_REASON))
    if unrecognised:
        parts.append(
            "Not a writable profile field: %s. Writable here: %s. Row "
            "collections (employments, educations, schools, itskills, "
            "projects, certifications, languages, onlineProfiles, "
            "workSamples, presentations, publications, patents) are written "
            "one row at a time with naukri_update_profile_section, because a "
            "row needs its id and a flat field dict has nowhere to put one."
            % (", ".join(unrecognised), ", ".join(sorted(UPDATABLE_FIELDS))))

    return {"status": "error", "error_code": "VALIDATION_ERROR",
            "message": " | ".join(parts)}


def _plan_rest_writes(rest_fields: dict) -> list:
    """Group the REST-routed fields into one write per OWNING section.

    Sorted, so the order a caller sees in a preview is the order the writes
    actually run in. Two fields of the same section travel in one write; two
    fields of different sections cannot, because the section name is the only
    thing that scopes a scalar write (all four scalar sections share the
    ``profile`` envelope, and ``write_section`` refuses a stray field).
    """
    by_section = {}
    for name in sorted(rest_fields):
        by_section.setdefault(REST_FIELD_SECTION[name], {})[name] = rest_fields[name]
    return [(section, by_section[section]) for section in sorted(by_section)]


def classify_section_write(result: dict) -> str:
    """Turn one ``write_section`` reply into an honest per-unit outcome.

    FOUR VALUES, because three would have to lie about one of these cases:

    ``landed``      ``write_section`` re-read the profile and SAW the change.
    ``landed_with_collateral``  the intended change landed AND something else
                    in that section moved. The snapshot in the unit is the way
                    back; this is never reported as a plain success.
    ``not_sent``    refused before the transport (VALIDATION_ERROR). This is
                    the only outcome that certifies nothing reached Naukri.
    ``unknown``     dispatched, or possibly dispatched, and NOT confirmed --
                    a transport exception (which cannot prove the server did
                    not apply it) or NOT_PERSISTED (accepted, then invisible
                    in the re-read). It must never be reported as a success
                    and must never be reported as "nothing happened" either.
    """
    status = result.get("status")
    if status == "updated":
        return "landed"
    if status == "preview":
        return "preview"
    code = result.get("error_code")
    if code == "COLLATERAL_CHANGE":
        return "landed_with_collateral"
    if code == "VALIDATION_ERROR":
        return "not_sent"
    return "unknown"


def _unit(section: str, fields: dict, result: dict) -> dict:
    """One row of the per-unit report. Carries what a caller must act on."""
    return {
        "route": "rest_section_write",
        "section": section,
        "fields": sorted(fields),
        "outcome": classify_section_write(result),
        "status": result.get("status"),
        "error_code": result.get("error_code"),
        "verified": result.get("verified"),
        "snapshot": result.get("snapshot"),
        "would_send": result.get("would_send"),
        "collateral": result.get("collateral"),
        "message": result.get("message"),
    }


async def _run_rest_writes(plan: list, confirm: bool) -> dict:
    """Run the planned section writes SEQUENTIALLY. NOT ATOMIC.

    There is no transaction across two fullprofiles writes and no rollback, so
    this does the only honest thing available: it runs them in a fixed order,
    STOPS at the first unit that does not land, and reports every unit by name
    with what happened to it. Units after the stop are reported
    ``not_attempted`` rather than silently omitted -- a caller that cannot see
    the difference between "did not run" and "ran and failed" will eventually
    trust a half-written profile.

    Returns ``{units, landed, incomplete}``. ``incomplete`` is True the moment
    any unit is anything but ``landed``, and the caller must not report success
    when it is set.
    """
    units, landed = [], []
    stopped = False

    for section, section_fields in plan:
        if stopped:
            units.append({
                "route": "rest_section_write", "section": section,
                "fields": sorted(section_fields), "outcome": "not_attempted",
                "status": None,
                "message": "Not attempted: an earlier write in this call did "
                           "not land, so this one was never sent.",
            })
            continue
        try:
            result = await write_section(
                section=section, fields=section_fields, confirm=confirm,
                label="pre-update-%s" % section,
            )
        except Exception as e:  # noqa: BLE001 - one bad unit must not hide the rest
            logger.warning("Section write %r raised: %s: %s",
                           section, type(e).__name__, e)
            result = {
                "status": "error", "error_code": "API_ERROR",
                "message": "Section write raised before it could be verified, "
                           "so whether Naukri applied it is UNKNOWN: %s: %s"
                           % (type(e).__name__, e),
            }
        unit = _unit(section, section_fields, result)
        units.append(unit)
        if unit["outcome"] in ("landed", "preview"):
            landed.append(section)
        else:
            stopped = True

    incomplete = any(u["outcome"] not in ("landed", "preview") for u in units)
    return {"units": units, "landed": landed, "incomplete": incomplete}


async def _update_profile(
    fields: dict,
    notice_period: Optional[str] = None,
    expected_ctc: Optional[float] = None,
    current_ctc: Optional[float] = None,
    confirm: bool = False,
) -> dict:
    """Update Naukri profile fields. Two routes, one front door.

    THE BROWSER ROUTE (resumeHeadline, keySkills, noticePeriod, expectedCtc,
    currentCtc) is unchanged, gate included: a call naming ONLY these five
    behaves exactly as it always has and does not need ``confirm``. Three of
    them have no other route -- no SectionSpec owns noticePeriod, currentCtc
    or expectedCtc, so the modal is the only measured way to write them.

    THE REST ROUTE reaches every scalar field ``tools/profile_sections.py``
    can write (summary, and the twelve careerPreferences fields), through
    ``write_section`` rather than around it. That means each one inherits, not
    re-implements, the four rails: its own pre-write snapshot, the post-write
    RE-READ that decides ``verified``, the collateral detector, and the
    confirm gate.

    ``confirm`` GATES EVERY REST FIELD. A call naming even one of them writes
    NOTHING unless ``confirm=True`` -- not the REST part and not the browser
    part -- and returns the exact body each write would send. The browser
    fields cannot be shown as a body (they are DOM form edits, not a payload),
    so the preview names them and their values instead and says so.

    NOT ATOMIC, and it says so in the result. Two REST sections are two
    writes; a REST leg plus a browser leg is two legs. There is no rollback on
    naukri.com. So the reply carries a per-unit report and the status is
    ``partial`` -- never ``updated`` -- whenever any unit did not land. A unit
    that was dispatched and could not be confirmed is reported ``unknown``,
    which is neither a success nor a "nothing happened".

    Args:
        fields: field -> value. See ``UPDATABLE_FIELDS``.
        notice_period, expected_ctc, current_ctc: shorthands merged into
            ``fields``.
        confirm: required (True) to write anything when a REST field is named.
            Ignored for a browser-only call, which keeps its original
            behaviour.

    Returns:
        Browser-only call: the original shape -- ``{status: "updated",
        method: "browser_ui", updated_fields, api_confirmed, message}``.
        Any call naming a REST field: ``{status: preview|updated|partial|
        error, atomic: False, units: [...], updated_fields, ...}``.
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
        return _reject_unknown(unknown)

    browser_fields = {k: v for k, v in fields.items()
                      if k in BROWSER_SUPPORTED_FIELDS}
    rest_fields = {k: v for k, v in fields.items()
                   if k not in BROWSER_SUPPORTED_FIELDS}

    plan = _plan_rest_writes(rest_fields)

    if rest_fields and not confirm:
        preview = await _run_rest_writes(plan, confirm=False)
        # A preview unit that did not come back `preview` was REFUSED by the
        # section layer -- a controlled-vocabulary field passed as a bare
        # string, say. Saying "2 writes would run" when one of them will be
        # rejected makes the preview a worse description of the confirmed run
        # than it has to be, and the preview is the thing being approved.
        blocked = [u["section"] for u in preview["units"]
                   if u["outcome"] not in ("preview", "not_attempted")]
        return {
            "status": "preview",
            "written": False,
            "atomic": False,
            "confirm_required_because": sorted(rest_fields),
            "blocked_by": blocked,
            "would_write_everything_asked": not blocked,
            "units": preview["units"],
            "browser_fields_that_would_be_edited": sorted(browser_fields),
            "browser_field_values": {k: browser_fields[k]
                                     for k in sorted(browser_fields)},
            "message": (
                "NOTHING WAS WRITTEN. %d REST section write(s) would run in "
                "the order shown, each body exactly as given in `would_send`, "
                "and they are NOT atomic -- a later one can fail after an "
                "earlier one has landed.%s %s Re-run with confirm=True to send "
                "them."
                % (len(plan),
                   ("" if not blocked else
                    " WARNING: %s would be REFUSED by the section layer before "
                    "reaching Naukri (see that unit's message), and the run "
                    "stops there, so the writes after it would not happen."
                    % ", ".join(blocked)),
                   ("Then %d browser field(s) would be edited through the "
                    "profile edit modals: %s. A modal edit has no request "
                    "body to show, so those are named here with the values "
                    "that would be typed in, not previewed as a payload."
                    % (len(browser_fields), ", ".join(sorted(browser_fields))))
                   if browser_fields else
                   "No browser fields are involved in this call.")),
        }

    rest = await _run_rest_writes(plan, confirm=True) if plan else None
    updated_fields = ([f for section, sf in plan
                       if section in rest["landed"] for f in sorted(sf)]
                      if rest else [])

    if rest and rest["incomplete"]:
        # Do not open a browser on top of a REST leg that did not complete.
        return {
            "status": "partial" if rest["landed"] else "error",
            "error_code": None if rest["landed"] else "PARTIAL_WRITE",
            "written": bool(rest["landed"]),
            "atomic": False,
            "units": rest["units"],
            "updated_fields": updated_fields,
            "browser_fields_not_attempted": sorted(browser_fields),
            "message": (
                "%d of %d section write(s) landed and the run STOPPED there; "
                "the browser leg (%s) was not attempted. Read `units`: each "
                "one carries its own outcome and, where a write was taken, "
                "the `snapshot` to restore from. An `unknown` outcome means "
                "the write was dispatched and could NOT be confirmed -- treat "
                "it as neither done nor undone and re-read the profile."
                % (len(rest["landed"]), len(plan),
                   ", ".join(sorted(browser_fields)) or "none")),
        }

    # --- the browser leg, called from exactly ONE place --------------------
    # It is one call site and the emit below is one emit site ON PURPOSE:
    # tests/test_read_path_purity.py freezes the census of every event emitter
    # as (file, function, event), so ProfileUpdated has to stay lexically
    # inside a function named `_update_profile` in this module. That is why
    # `_browser_update` returns its result and does not emit.
    browser_result = (await _browser_update(browser_fields)
                      if browser_fields else None)
    if browser_result is not None and browser_result.get("status") == "updated":
        # Invalidate on any completed browser run, exactly as the original did
        # -- including a keySkills no-op, where updated_fields is empty.
        _profile_ttl_cache.invalidate()
        _dashboard_ttl_cache.invalidate()
    if browser_result and browser_result.get("updated_fields"):
        try:
            from naukri_server.events import event_bus, ProfileUpdated
            await event_bus.emit(ProfileUpdated(
                fields=", ".join(browser_result["updated_fields"]),
                method="browser_ui",
            ))
        except Exception:
            pass

    if rest is None:
        # Browser-only call: the original shape, unchanged.
        return browser_result

    if browser_result is None:
        _profile_ttl_cache.invalidate()
        _dashboard_ttl_cache.invalidate()
        return {
            "status": "updated",
            "method": "rest_section_write",
            "atomic": False,
            "units": rest["units"],
            "updated_fields": updated_fields,
            "message": "Wrote %d section(s) through the verified REST route "
                       "and confirmed each one in a fresh read: %s."
                       % (len(plan), ", ".join(updated_fields)),
        }

    browser_unit = {
        "route": "browser_ui",
        "fields": sorted(browser_fields),
        "outcome": ("landed" if browser_result.get("status") == "updated"
                    else "unknown"),
        "status": browser_result.get("status"),
        "error_code": browser_result.get("error_code"),
        "updated_fields": list(browser_result.get("updated_fields") or []),
        "message": browser_result.get("message"),
    }
    all_units = rest["units"] + [browser_unit]
    updated_fields = updated_fields + browser_unit["updated_fields"]

    if browser_unit["outcome"] != "landed":
        return {
            "status": "partial",
            "written": True,
            "atomic": False,
            "units": all_units,
            "updated_fields": updated_fields,
            "message": (
                "The REST leg landed (%s) and the browser leg did NOT "
                "complete. The profile is part-way: %s. A browser modal that "
                "fails after an earlier modal saved leaves those earlier "
                "fields written, so `updated_fields` is what is KNOWN to have "
                "landed, not a guess. The REST sections carry a `snapshot` "
                "each."
                % (", ".join(rest["landed"]),
                   browser_result.get("message") or "no detail returned")),
        }

    return {
        "status": "updated",
        "method": "rest_section_write+browser_ui",
        "atomic": False,
        "units": all_units,
        "updated_fields": updated_fields,
        "api_confirmed": browser_result.get("api_confirmed"),
        "message": "Wrote %d section(s) through the verified REST route and "
                   "%d field(s) through the browser modals. Fields: %s."
                   % (len(plan), len(browser_unit["updated_fields"]),
                      ", ".join(updated_fields)),
    }


def _partial_browser_error(error: dict, ui_updated: list) -> dict:
    """A browser-leg error that admits what had ALREADY been saved.

    The modal sequence is a run of independent Saves. When the third one
    fails, the first two are ON THE PROFILE -- there is no rollback and no
    transaction. The path this replaces returned the bare handler error and
    dropped ``ui_updated`` on the floor, so a resumeHeadline that saved
    followed by a keySkills failure came back as a plain error with nothing in
    it saying the headline had changed. A caller reading that error would
    reasonably conclude the profile was untouched.

    ``status`` stays ``error`` so no existing caller's branch changes;
    ``updated_fields`` and ``partial`` are added beside it.
    """
    if not ui_updated:
        return error
    out = dict(error)
    out["partial"] = True
    out["updated_fields"] = list(ui_updated)
    out["message"] = (
        "%s -- BUT %d field(s) had ALREADY been saved before this failed and "
        "are on the profile now: %s. Each browser modal saves on its own; "
        "there is no rollback."
        % (error.get("message", "Browser update failed"),
           len(ui_updated), ", ".join(ui_updated)))
    return out


async def _browser_update(fields: dict) -> dict:
    """The browser-modal leg. Behaviour unchanged from before the REST split.

    Split out of ``_update_profile`` so a REST failure never opens a browser
    tab, and so the modal path keeps exactly one caller. The ONE thing that
    changed: when a handler fails partway, the error now carries
    ``updated_fields`` naming the modals that had ALREADY saved. It used to
    return the bare handler error and drop that list, so a headline that saved
    followed by a keySkills failure reported a plain error and nothing said the
    headline had changed.
    """
    # WHY THE BROWSER HERE -- and what the 405 does NOT say.
    # The note this replaces read: the fullprofiles endpoint rejects all
    # external API calls (405 from CDN), only the React app's own XHR works.
    # That generalised a 405 on GET into a verdict about POST, and it is
    # wrong. Naukri's own shipped editor bundle mnj_v320.min.js (848718
    # bytes) writes to the fullprofiles v1 route at bundle offset 44671, as a
    # POST carrying `X-HTTP-Method-Override: PUT`. tools/profile_write.py owns
    # that transport; tools/profile_sections.py writes SIXTEEN sections
    # through it (its own module docstring still says ten -- counted off
    # SECTION_SPECS: 4 scalar + 6 single-row + 6 whole-list), and
    # _boost_visibility below uses it too.
    # What the 405 actually shows: a live GET to all three fullprofiles
    # versions returns 405 with NO `Allow` header and a byte-identical
    # 92-byte body. It says only that GET is not allowed -- it does not even
    # prove which versions are routed, let alone anything about POST.
    # These five fields stay on the browser UI because they are modal form
    # edits (noticePeriod/currentCtc/expectedCtc share one Career Profile
    # modal) and because THREE OF THEM HAVE NO REST ROUTE AT ALL: no
    # SectionSpec owns noticePeriod, currentCtc or expectedCtc. Not because
    # the REST route is closed -- _update_profile now uses it for every scalar
    # field the section layer can write.
    # navigate to profile -> click edit -> modify form fields -> click save.
    #
    # Track which fields were updated via UI. Declared OUTSIDE the try so the
    # catch-all at the bottom can still report what had already saved: a modal
    # sequence that throws halfway has written the modals it already saved,
    # and an error that drops that list teaches the caller the profile is
    # untouched when it is not.
    ui_updated = []
    async with browser.page_pool.acquire() as page:
        try:
            await page_goto(page, f"{NAUKRI_BASE}/mnjuser/profile")
            await asyncio.sleep(BROWSER_PAGE_LOAD)

            if "/nlogin" in page.url:
                return {"status": "error", "message": "Not logged in. Call naukri_login first.", "error_code": "AUTH_ERROR"}

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
                        return _partial_browser_error(result, ui_updated)
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
                        return _partial_browser_error({
                            "status": "error",
                            "message": "Could not find edit button for Career Profile section "
                                       "(needed for noticePeriod/expectedCtc/currentCtc)",
                            "error_code": "BROWSER_ERROR",
                        }, ui_updated)
                    await asyncio.sleep(BROWSER_MODAL_APPEAR)

                    # The three career fields SHARE one modal and one Save, so
                    # nothing they set is written until that Save lands. They
                    # are therefore staged, not appended -- appending them here
                    # would name a field as updated on a run where the Save
                    # below never happened.
                    staged = []
                    for field_name in ("noticePeriod", "currentCtc", "expectedCtc"):
                        if field_name not in career_fields:
                            continue
                        handler = _CAREER_FIELD_HANDLERS[field_name]
                        result = await handler(page, career_fields[field_name])
                        if result.get("status") == "error":
                            return _partial_browser_error(result, ui_updated)
                        if result.get("status") == "success":
                            staged.append(field_name)

                    # Click save on the Career Profile modal (shared save)
                    save_result = await _click_save_modal(page)
                    if not save_result:
                        return _partial_browser_error(
                            {"status": "error",
                             "message": "Career Profile modal opened but Save button not found",
                             "error_code": "NOT_FOUND"},
                            ui_updated)
                    await asyncio.sleep(BROWSER_FORM_SAVE)
                    ui_updated.extend(staged)

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
            return _partial_browser_error(
                {"status": "error",
                 "message": f"Profile update failed: {type(e).__name__}: {e}",
                 "error_code": "API_ERROR"},
                ui_updated)


async def _boost_visibility(randomize: bool = False) -> dict:
    """Boost profile visibility by re-saving headline (internal helper)."""
    # Import caches here to avoid circular imports
    from naukri_server.tools.profile import _profile_ttl_cache, _dashboard_ttl_cache

    if randomize:
        import random
        delay = random.randint(0, 300)
        logger.info("Randomized delay: %d seconds", delay)
        await asyncio.sleep(delay)

    # Strategy 1: the fullprofiles WRITE route, through the single transport
    # chokepoint. profile_write.fullprofiles_write owns the route (v1), the
    # `X-HTTP-Method-Override: PUT` header and the top-level `profileId`, so
    # none of the three is re-typed here and a boost cannot drift away from
    # the editor contract.
    #
    # What was here before could never have worked: it looped over ("v0",
    # "v2") building the endpoint off FULLPROFILES_API and sent no override
    # header. Neither version is a fullprofiles write route, so every boost
    # fell through to the browser path below.
    try:
        profile_data = await api_client.get(
            PROFILE_API,
            {"expand_level": "4"},
        )
        profiles = profile_data.get("profile", [])
        if profiles and isinstance(profiles[0], dict):
            headline = profiles[0].get("resumeHeadline", "")
            if headline:
                # A boost is a no-op RE-SAVE: the headline just read goes
                # back unchanged. The only thing that moves is Naukri's
                # last-modified stamp, which is what "recently active" reads.
                await fullprofiles_write({"profile": {"resumeHeadline": headline}})
                _profile_ttl_cache.invalidate()
                _dashboard_ttl_cache.invalidate()

                try:
                    from naukri_server.events import event_bus, ProfileBoosted
                    await event_bus.emit(
                        ProfileBoosted(method="rest_fullprofiles_v1"))
                except Exception:
                    pass

                return {
                    "status": "success", "action": "refreshed",
                    "method": "rest_fullprofiles_v1",
                    "headline_length": len(headline),
                    "message": "Profile refreshed via the fullprofiles v1 write "
                               "route. You appear as 'recently active'.",
                }
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

            try:
                from naukri_server.events import event_bus, ProfileBoosted
                await event_bus.emit(ProfileBoosted(method=f"browser_{edit_clicked}"))
            except Exception:
                pass

            return {
                "status": "success", "action": "refreshed",
                "method": f"browser_{edit_clicked}",
                "save": save_result,
                "api_confirmed": api_confirmed.get("status") == 200,
                "message": "Profile refreshed via browser. You appear as 'recently active'.",
            }
    except Exception as e:
        return {"status": "error", "message": f"Browser fallback failed: {type(e).__name__}: {e}", "error_code": "BROWSER_ERROR"}
