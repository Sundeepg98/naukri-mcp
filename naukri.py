#!/usr/bin/env python3
"""
Naukri.com Job Automation MCP Server (6 tools)

Self-contained MCP with embedded Playwright browser — Unipile-style architecture.
Browser runs internally; each tool call does all automation server-side.
One tool call = one complete action. Zero context burn for Claude.

Tools: login, verify_otp, search_jobs, get_job, apply, refresh_profile
"""

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright, BrowserContext, Page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("naukri")

# Persistent chrome profile directory (cookies survive restarts)
CHROME_PROFILE = str(Path(__file__).parent / "chrome-profile")
NAUKRI_BASE = "https://www.naukri.com"

# Default timeouts (ms)
NAV_TIMEOUT = 20_000
ELEMENT_TIMEOUT = 5_000
SHORT_WAIT = 2_000


# ============================================================================
# Browser State — persists across all tool calls within one MCP session
# ============================================================================


class NaukriBrowser:
    """Manages a single Playwright browser that lives for the entire MCP session."""

    def __init__(self):
        self.pw = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._lock = asyncio.Lock()

    async def start(self):
        self.pw = await async_playwright().start()
        # NOTE: Do NOT use channel="chrome" — system Chrome uses
        # --remote-debugging-pipe which conflicts with MCP's stdio transport.
        # Playwright's bundled Chromium uses websocket protocol instead.
        self.context = await self.pw.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE,
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        logger.info("Browser started with profile: %s", CHROME_PROFILE)

    async def stop(self):
        if self.context:
            await self.context.close()
        if self.pw:
            await self.pw.stop()
        logger.info("Browser stopped")

    async def goto(self, url: str, wait: str = "domcontentloaded") -> None:
        """Navigate with retry on timeout."""
        try:
            await self.page.goto(url, wait_until=wait, timeout=NAV_TIMEOUT)
        except Exception:
            # Retry with looser wait
            await self.page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT)

    async def text(self, selector: str) -> Optional[str]:
        """Get text content of an element, or None if not found."""
        el = await self.page.query_selector(selector)
        return (await el.text_content()).strip() if el else None

    async def exists(self, selector: str) -> bool:
        return await self.page.query_selector(selector) is not None

    async def click_if_exists(self, selector: str) -> bool:
        el = await self.page.query_selector(selector)
        if el:
            await el.click()
            return True
        return False

    async def safe_fill(self, selector: str, value: str, delay: int = 30):
        """Fill a field with human-like delay."""
        el = await self.page.wait_for_selector(selector, timeout=ELEMENT_TIMEOUT)
        await el.click()
        await el.fill("")
        await el.type(value, delay=delay)


browser = NaukriBrowser()


# ============================================================================
# MCP Lifespan — browser starts once, lives until MCP disconnects
# ============================================================================


@asynccontextmanager
async def lifespan(server):
    await browser.start()
    try:
        yield
    finally:
        await browser.stop()


mcp = FastMCP("naukri", lifespan=lifespan)


# ============================================================================
# Tool 1: Login
# ============================================================================


@mcp.tool()
async def naukri_login(
    method: str = "google",
    email: Optional[str] = None,
    password: Optional[str] = None,
) -> dict:
    """Login to Naukri.com. Supports Google SSO (recommended) or email/password.

    Google SSO is the easiest path — if Google is already signed in via the
    Chrome profile, login is automatic. First time requires one manual click
    to authorize. Cookies persist across sessions (~30 days).

    Args:
        method: "google" (recommended) or "email". Google SSO uses the Chrome
                profile's saved Google session — no credentials needed.
        email: Naukri email (only for method="email")
        password: Naukri password (only for method="email")
    """
    async with browser._lock:
        await browser.goto(f"{NAUKRI_BASE}/nlogin/login")
        await asyncio.sleep(2)

        # Check if already logged in (redirected to homepage)
        if "/nlogin" not in browser.page.url:
            name = await browser.text(".nI-gNb-sb__main-text")
            return {"status": "already_logged_in", "profile_name": name or "unknown"}

        if method == "google":
            # Click "Login with Google" button
            google_clicked = False
            for selector in [
                "button:has-text('Google')",
                "a:has-text('Google')",
                "[class*='google']",
                "button[data-ga-track*='google']",
                "[class*='social'] button:first-child",
                "img[alt*='Google']",
            ]:
                try:
                    el = await browser.page.query_selector(selector)
                    if el:
                        await el.click()
                        google_clicked = True
                        break
                except Exception:
                    continue

            if not google_clicked:
                return {
                    "status": "error",
                    "message": "Google login button not found. Try method='email' instead.",
                }

            # Google OAuth flow — may open popup or redirect
            # Wait for either: Google account picker, or redirect back to Naukri
            await asyncio.sleep(5)

            # If Google session exists in Chrome profile, auto-redirects back
            if "/nlogin" not in browser.page.url and "accounts.google" not in browser.page.url:
                name = await browser.text(".nI-gNb-sb__main-text")
                return {"status": "logged_in", "method": "google", "profile_name": name or "unknown"}

            # If Google account picker is showing, user needs to select account
            if "accounts.google" in browser.page.url:
                return {
                    "status": "waiting_for_user",
                    "message": "Google account picker is open in the browser. "
                               "Please select your account manually. "
                               "Then call naukri_login() again to verify.",
                }

            # Check if we landed on Naukri logged in
            await asyncio.sleep(3)
            if "/nlogin" not in browser.page.url:
                name = await browser.text(".nI-gNb-sb__main-text")
                return {"status": "logged_in", "method": "google", "profile_name": name or "unknown"}

            return {"status": "error", "message": "Google login did not complete. Check the browser."}

        else:
            # Email/password login
            if not email or not password:
                return {"status": "error", "message": "email and password required for method='email'"}

            try:
                await browser.safe_fill("input#usernameField", email)
                await browser.safe_fill("input#passwordField", password)
                await browser.page.click("button[type='submit']")
            except Exception as e:
                return {"status": "error", "message": f"Login form error: {e}"}

            await asyncio.sleep(3)

            # OTP screen
            otp_field = await browser.page.query_selector("input#otp")
            if otp_field:
                return {"status": "otp_required", "needs_otp": True, "message": "Enter OTP sent to your phone"}

            # Successful login
            if "/nlogin" not in browser.page.url:
                name = await browser.text(".nI-gNb-sb__main-text")
                return {"status": "logged_in", "method": "email", "profile_name": name or "unknown"}

            error = await browser.text(".err-message, .error-msg, [class*='error']")
            return {"status": "error", "message": error or "Login failed — check credentials"}


# ============================================================================
# Tool 2: Verify OTP
# ============================================================================


@mcp.tool()
async def naukri_verify_otp(otp: str) -> dict:
    """Enter OTP code after login. Call this after naukri_login returns needs_otp.

    Args:
        otp: 6-digit OTP from SMS/email
    """
    async with browser._lock:
        try:
            await browser.safe_fill("input#otp", otp)
            await browser.page.click("button[type='submit']")
            await asyncio.sleep(3)

            if "/nlogin" not in browser.page.url:
                name = await browser.text(".nI-gNb-sb__main-text")
                return {"status": "logged_in", "profile_name": name or "unknown"}

            error = await browser.text(".err-message, .error-msg")
            return {"status": "error", "message": error or "OTP verification failed"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ============================================================================
# Tool 3: Search Jobs
# ============================================================================


@mcp.tool()
async def naukri_search_jobs(
    keywords: str,
    location: Optional[str] = None,
    experience: Optional[int] = None,
    limit: int = 20,
) -> dict:
    """Search for jobs on Naukri.com.

    Args:
        keywords: Job title or skills (e.g., "python developer", "react", "devops")
        location: City name (e.g., "Bangalore", "Mumbai", "Remote")
        experience: Years of experience (e.g., 5)
        limit: Max jobs to return (default 20)

    Returns list of jobs with title, company, salary, location, URL.
    """
    async with browser._lock:
        # Naukri uses SEO-friendly URLs: /keyword-jobs-in-location
        slug = keywords.lower().replace(" ", "-").replace(".", "-")
        if location:
            loc_slug = location.lower().replace(" ", "-")
            url = f"{NAUKRI_BASE}/{slug}-jobs-in-{loc_slug}"
        else:
            url = f"{NAUKRI_BASE}/{slug}-jobs"
        if experience is not None:
            url += f"?experience={experience}"

        await browser.goto(url)
        await asyncio.sleep(4)

        # Extract job cards — confirmed selectors from DOM inspection
        cards = await browser.page.query_selector_all("div.srp-jobtuple-wrapper")

        jobs = []
        for card in cards[:limit]:
            try:
                title_el = await card.query_selector("a.title")
                company_el = await card.query_selector("a.comp-name")
                salary_el = await card.query_selector("span.sal-wrap span.expwdth, span.ni-job-tuple-icon-srp-rupee")
                location_el = await card.query_selector("span.locWdth")
                exp_el = await card.query_selector("span.expwdth")
                job_id = await card.get_attribute("data-job-id")

                title = (await title_el.text_content()).strip() if title_el else None
                href = await title_el.get_attribute("href") if title_el else None

                jobs.append({
                    "job_id": job_id,
                    "title": title,
                    "company": (await company_el.text_content()).strip() if company_el else None,
                    "salary": (await salary_el.text_content()).strip() if salary_el else None,
                    "location": (await location_el.text_content()).strip() if location_el else None,
                    "experience": (await exp_el.text_content()).strip() if exp_el else None,
                    "url": href if href and href.startswith("http") else f"{NAUKRI_BASE}{href}" if href else None,
                })
            except Exception:
                continue

        return {
            "status": "success",
            "keywords": keywords,
            "location": location,
            "count": len(jobs),
            "jobs": jobs,
        }


# ============================================================================
# Tool 4: Get Job Details
# ============================================================================


@mcp.tool()
async def naukri_get_job(job_url: str) -> dict:
    """Get full details for a specific Naukri job.

    Args:
        job_url: Full Naukri job URL (from search results)

    Returns job title, company, full description, requirements, and whether you can apply.
    """
    async with browser._lock:
        await browser.goto(job_url)
        await asyncio.sleep(3)

        # Extract all job details via JS — more reliable than individual queries
        # because Naukri uses CSS-module hashed classes (e.g. styles_jhc__exp__k_giM)
        data = await browser.page.evaluate("""() => {
            const txt = (sel) => {
                const el = document.querySelector(sel);
                return el ? el.textContent.trim() : null;
            };

            // Company name is in a div that also contains rating/reviews children
            // Extract just the first text node to get clean company name
            const compDiv = document.querySelector('[class*="jd-header-comp-name"]');
            let company = null;
            if (compDiv) {
                // Try anchor first, then first text node
                const a = compDiv.querySelector('a');
                if (a) {
                    company = a.textContent.trim();
                } else {
                    // Walk child nodes for first text
                    for (const node of compDiv.childNodes) {
                        if (node.nodeType === 3 && node.textContent.trim()) {
                            company = node.textContent.trim();
                            break;
                        }
                    }
                    // Fallback: full text minus rating pattern
                    if (!company) {
                        company = compDiv.textContent.trim().replace(/\\d+\\.\\d+\\d+ Reviews?$/, '').trim();
                    }
                }
            }

            // Description: get inner text (preserves line breaks)
            const descEl = document.querySelector('section[class*="job-desc-container"], [class*="job-desc"], [class*="JDC"]');
            const description = descEl ? descEl.innerText.trim() : null;

            // Skills
            const skills = Array.from(
                document.querySelectorAll('[class*="chip"], [class*="tag-li"], .key-skill a')
            ).map(s => s.textContent.trim()).filter(Boolean);

            // Apply state
            const alreadyApplied = !!document.querySelector('#already-applied');
            const expired = !!document.querySelector('[class*="alert-message-text"]');
            const external = !!document.querySelector('#company-site-button');
            const applyBtn = !!document.querySelector('button[class*="apply-button"]');

            return {
                title: txt('h1'),
                company,
                salary: txt('[class*="jhc__salary"]'),
                experience: txt('[class*="jhc__exp"]:not([class*="container"])'),
                location: txt('[class*="jhc__location"]'),
                description,
                skills,
                already_applied: alreadyApplied,
                expired,
                external_apply: external,
                can_apply: applyBtn && !alreadyApplied && !expired && !external,
            };
        }""")

        data["status"] = "success"
        data["url"] = job_url
        return data


# ============================================================================
# Tool 5: Apply to Job
# ============================================================================


@mcp.tool()
async def naukri_apply(
    job_url: str,
    resume_path: Optional[str] = None,
    answers: Optional[dict] = None,
) -> dict:
    """Apply to a Naukri job. Handles the full chatbot-style application flow.

    Args:
        job_url: Full Naukri job URL
        resume_path: Path to resume PDF file (optional — uses profile resume if not provided)
        answers: Dict of screening question answers, e.g.:
                 {"notice_period": "30 days", "current_ctc": "25 LPA",
                  "years_of_experience": "5", "willing_to_relocate": "Yes"}
                 If a question isn't in answers, it's returned as pending.

    Returns:
        - {status: "applied"} on success
        - {status: "needs_input", pending_questions: [...]} if unanswered questions remain
        - {status: "already_applied"} if already applied
        - {status: "error", message: "..."} on failure
    """
    if answers is None:
        answers = {}

    async with browser._lock:
        await browser.goto(job_url)
        await asyncio.sleep(2)

        # Pre-flight checks
        if await browser.exists("#already-applied"):
            return {"status": "already_applied", "url": job_url}

        if await browser.exists(".styles_alert-message-text"):
            return {"status": "error", "message": "Job has expired"}

        if await browser.exists("#company-site-button"):
            return {"status": "error", "message": "External application — redirects to company site"}

        # Click Apply — confirmed selector: button.styles_apply-button__*
        apply_clicked = False
        for selector in [
            "button[class*='apply-button']",
            "button:has-text('Apply')",
            "#apply-button",
        ]:
            try:
                el = await browser.page.query_selector(selector)
                if el:
                    await el.click()
                    apply_clicked = True
                    break
            except Exception:
                continue

        if not apply_clicked:
            # Try XPath fallback
            try:
                el = await browser.page.locator("xpath=//*[contains(text(),'Apply')]").first.element_handle()
                if el:
                    await el.click()
                    apply_clicked = True
            except Exception:
                pass

        if not apply_clicked:
            return {"status": "error", "message": "Apply button not found"}

        await asyncio.sleep(2)

        # Check for instant success (some jobs apply with one click)
        success = await _check_success()
        if success:
            return {"status": "applied", "url": job_url, "questions_answered": []}

        # Handle resume upload if prompted
        if resume_path:
            file_input = await browser.page.query_selector("input[type='file']")
            if file_input:
                await file_input.set_input_files(resume_path)
                await asyncio.sleep(1)

        # Handle chatbot screening questions
        questions_answered = []
        pending_questions = []
        max_rounds = 15  # Safety limit

        for _ in range(max_rounds):
            # Check success after each round
            success = await _check_success()
            if success:
                return {
                    "status": "applied",
                    "url": job_url,
                    "questions_answered": questions_answered,
                }

            # Try to find and answer a question
            answered = await _answer_question(answers, questions_answered, pending_questions)

            if not answered:
                # No more questions found
                break

            await asyncio.sleep(1.5)

        # Final success check
        success = await _check_success()
        if success:
            return {
                "status": "applied",
                "url": job_url,
                "questions_answered": questions_answered,
            }

        if pending_questions:
            return {
                "status": "needs_input",
                "url": job_url,
                "pending_questions": pending_questions,
                "questions_answered": questions_answered,
                "message": "Some screening questions need your input. Provide answers dict and call again.",
            }

        return {
            "status": "uncertain",
            "url": job_url,
            "questions_answered": questions_answered,
            "message": "Could not confirm application success. Check Naukri dashboard.",
        }


async def _check_success() -> bool:
    """Check if application was submitted successfully."""
    # Check DOM elements for success indicators
    for selector in [
        ".apply-message",
        ".apply-status-header.green",
        "#already-applied",
        "text=successfully applied",
        "text=Application Submitted",
    ]:
        try:
            el = await browser.page.query_selector(selector)
            if el:
                return True
        except Exception:
            continue

    # Check chatbot messages for "Thank you" / completion signals
    try:
        chat_items = await browser.page.query_selector_all("ul[id^='chatList_'] li")
        if chat_items:
            last = chat_items[-1]
            text = (await last.text_content()).strip().lower()
            if any(phrase in text for phrase in [
                "thank you", "successfully", "application submitted",
                "applied successfully", "all the best",
            ]):
                return True
    except Exception:
        pass

    return False


async def _answer_question(answers: dict, answered: list, pending: list) -> bool:
    """Try to answer one screening question. Returns True if a question was handled."""

    # --- Radio button questions ---
    radios = await browser.page.query_selector_all(".ssrc__radio-btn-container")
    if radios:
        # Find the question text
        question_text = await _get_current_question()
        if not question_text:
            return False

        # Find matching answer
        answer = _match_answer(question_text, answers)

        if answer:
            # Click the matching radio option
            for radio in radios:
                label = await radio.query_selector("label")
                if label:
                    label_text = (await label.text_content()).strip()
                    if label_text.lower() == answer.lower() or answer.lower() in label_text.lower():
                        inp = await radio.query_selector("input")
                        if inp:
                            await browser.page.evaluate("el => el.click()", inp)
                            answered.append({"question": question_text, "answer": label_text})
                            await _click_save()
                            return True

            # No matching label — click first option as fallback? No, add to pending.
            options = []
            for radio in radios:
                label = await radio.query_selector("label")
                if label:
                    options.append((await label.text_content()).strip())
            pending.append({"question": question_text, "type": "radio", "options": options})
            return False
        else:
            options = []
            for radio in radios:
                label = await radio.query_selector("label")
                if label:
                    options.append((await label.text_content()).strip())
            pending.append({"question": question_text, "type": "radio", "options": options})
            return False

    # --- Text input questions ---
    text_area = await browser.page.query_selector(".textArea, textarea[class*='textArea']")
    if text_area:
        question_text = await _get_current_question()
        if not question_text:
            return False

        answer = _match_answer(question_text, answers)
        if answer:
            await text_area.click()
            await text_area.fill(answer)
            answered.append({"question": question_text, "answer": answer})
            await _click_save()
            return True
        else:
            pending.append({"question": question_text, "type": "text"})
            return False

    return False


async def _get_current_question() -> Optional[str]:
    """Extract the current screening question text from the chatbot."""
    for selector in [
        "ul[id^='chatList_'] li.botItem:last-child span",
        "ul[id^='chatList_'] li:last-child div span",
        ".chatbot-question",
        ".bot-msg:last-child",
    ]:
        el = await browser.page.query_selector(selector)
        if el:
            text = (await el.text_content()).strip()
            if text and len(text) > 3:
                # Skip success/completion messages — not actual questions
                lower = text.lower()
                if any(p in lower for p in [
                    "thank you", "successfully", "application submitted",
                    "all the best", "good luck",
                ]):
                    return None
                return text
    return None


def _match_answer(question: str, answers: dict) -> Optional[str]:
    """Fuzzy match a question to the answers dict."""
    q = question.lower()
    for key, value in answers.items():
        k = key.lower().replace("_", " ")
        if k in q or q in k:
            return str(value)
        # Partial word matching
        key_words = k.split()
        if all(w in q for w in key_words):
            return str(value)
    return None


async def _click_save():
    """Click the Save/Next button after answering a question."""
    for selector in [
        "button:has-text('Save')",
        "button:has-text('Next')",
        "button:has-text('Submit')",
        "div.chatbot-action button",
    ]:
        try:
            el = await browser.page.query_selector(selector)
            if el:
                await el.click()
                return
        except Exception:
            continue

    # XPath fallback (from JobSailor)
    try:
        el = await browser.page.locator("xpath=/html/body/div[2]/div/div[1]/div[3]/div/div").element_handle()
        if el:
            await el.click()
    except Exception:
        pass


# ============================================================================
# Tool 6: Refresh Profile
# ============================================================================


@mcp.tool()
async def naukri_refresh_profile() -> dict:
    """Refresh Naukri profile to boost visibility (daily trick).

    Navigates to profile, clicks edit on Resume Headline, saves without changes.
    This triggers Naukri's 'recently active' signal — recruiters see you first.
    """
    async with browser._lock:
        await browser.goto(f"{NAUKRI_BASE}/mnjuser/profile")
        await asyncio.sleep(3)

        if "/nlogin" in browser.page.url:
            return {"status": "error", "message": "Not logged in. Call naukri_login first."}

        # Naukri uses icon-font spans with text "editOneTheme" as edit buttons.
        # Find the one near "Resume headline" section heading, or fall back to any edit icon.
        edit_clicked = await browser.page.evaluate("""() => {
            // Find all edit icons — they're leaf elements with text "editOneTheme"
            const editIcons = Array.from(document.querySelectorAll('*')).filter(el =>
                el.children.length === 0 && el.textContent.trim() === 'editOneTheme'
            );
            if (editIcons.length === 0) return null;

            // Try to find the one in/near "Resume headline" section
            for (const icon of editIcons) {
                const section = icon.closest('[class*="headline"], [class*="resumeHeadline"]');
                if (section) { icon.click(); return 'headline_edit'; }
            }

            // Try to find one near text "Resume headline"
            for (const icon of editIcons) {
                const parent = icon.closest('div, section');
                if (parent && parent.textContent.includes('Resume headline')) {
                    icon.click();
                    return 'headline_parent';
                }
            }

            // Fall back to first edit icon (name/title section is fine too)
            editIcons[0].click();
            return 'first_edit';
        }""")

        if not edit_clicked:
            return {"status": "error", "message": "Could not find edit button on profile"}

        await asyncio.sleep(2)

        # Click Save via JS — target the modal's Save button specifically
        save_result = await browser.page.evaluate("""() => {
            // Priority 1: Save button inside the open modal/dialog overlay
            const modal = document.querySelector('[class*="modal"], [class*="dialog"], [class*="overlay"], [role="dialog"]');
            if (modal) {
                const btn = Array.from(modal.querySelectorAll('button')).find(
                    b => b.textContent.trim().toLowerCase() === 'save'
                );
                if (btn) { btn.click(); return 'modal_save'; }
            }

            // Priority 2: Button with exact text "Save" (not "Save photo" etc.)
            const exactSave = Array.from(document.querySelectorAll('button')).find(
                b => b.textContent.trim() === 'Save' && b.offsetParent !== null
            );
            if (exactSave) { exactSave.click(); return 'exact_save'; }

            // Priority 3: Any visible button with text "Save"
            const anySave = Array.from(document.querySelectorAll('button')).find(
                b => b.textContent.trim().toLowerCase() === 'save'
            );
            if (anySave) { anySave.click(); return 'any_save'; }

            return null;
        }""")

        if not save_result:
            return {"status": "partial", "method": edit_clicked, "message": "Edit opened but Save button not found. Profile may still be refreshed."}

        await asyncio.sleep(2)
        return {
            "status": "refreshed",
            "method": edit_clicked,
            "save": save_result,
            "message": "Profile updated. You now appear as 'recently active' to recruiters.",
        }


# ============================================================================
# Tool 7: Debug — page snapshot for selector calibration
# ============================================================================


@mcp.tool()
async def naukri_debug(action: str = "snapshot") -> dict:
    """Debug tool: capture current page state for selector calibration.

    Args:
        action: "snapshot" — returns page URL, title, and DOM structure of key elements
                "screenshot" — saves screenshot to tools/naukri/debug.png
    """
    async with browser._lock:
        url = browser.page.url
        title = await browser.page.title()

        if action == "screenshot":
            path = str(Path(__file__).parent / "debug.png")
            await browser.page.screenshot(path=path, full_page=False)
            return {"status": "ok", "url": url, "title": title, "screenshot": path}

        # Snapshot: extract DOM structure for selector calibration
        structure = await browser.page.evaluate("""() => {
            // Get all elements with useful class/id patterns
            const selectors = [
                // Job cards
                '[class*="tuple"]', '[class*="jobCard"]', '[class*="job-card"]',
                '[class*="srp-"]', '[data-job-id]', 'article',
                // Job details
                '[class*="title"]', '[class*="comp"]', '[class*="salary"]',
                '[class*="location"]', '[class*="experience"]',
                // Apply
                '[class*="apply"]', 'button',
                // Login
                '[class*="google"]', '[class*="social"]',
            ];

            const results = {};
            for (const sel of selectors) {
                try {
                    const els = document.querySelectorAll(sel);
                    if (els.length > 0 && els.length < 50) {
                        results[sel] = Array.from(els).slice(0, 5).map(el => ({
                            tag: el.tagName.toLowerCase(),
                            id: el.id || null,
                            class: el.className ? el.className.toString().slice(0, 100) : null,
                            text: el.textContent ? el.textContent.trim().slice(0, 80) : null,
                            children: el.children.length,
                        }));
                    }
                } catch(e) {}
            }
            return results;
        }""")

        return {"status": "ok", "url": url, "title": title, "dom_structure": structure}


# ============================================================================
# Server Startup
# ============================================================================

if __name__ == "__main__":
    logger.info("Starting Naukri MCP Server (7 tools, embedded Playwright)")
    mcp.run()
