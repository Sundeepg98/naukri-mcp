#!/usr/bin/env python3
"""
Naukri.com Job Automation MCP Server v2 (8 tools)

Hybrid architecture:
  - Playwright browser for login/cookies/profile-refresh (visual tasks)
  - REST API calls for search/get_job/apply/profile (fast, reliable)
  - Answer cache for screening questions (same question never asked twice)

Browser runs internally for login; all data operations use Naukri's REST APIs
with the nauk_at JWT extracted from browser cookies.
"""

import asyncio
import json
import logging
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import aiohttp
from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright, BrowserContext, Page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("naukri")

# Paths
CHROME_PROFILE = str(Path(__file__).parent / "chrome-profile")
CACHE_FILE = Path(__file__).parent / "questions.json"
NAUKRI_BASE = "https://www.naukri.com"

# Timeouts (ms for Playwright, seconds for aiohttp)
NAV_TIMEOUT = 20_000
ELEMENT_TIMEOUT = 5_000
API_TIMEOUT = 30  # seconds

# API headers (from Naukri-Automation reverse engineering)
API_HEADERS = {
    "accept": "application/json",
    "appid": "121",
    "clientid": "d3skt0p",
    "content-type": "application/json",
    "systemid": "Naukri",
    "gid": "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
    "x-requested-with": "XMLHttpRequest",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

# Apply request trailer fields (required by Naukri's apply endpoint)
APPLY_TRAILER = {
    "flowtype": "show",
    "crossdomain": True,
    "jquery": 1,
    "rdxMsgId": "",
    "chatBotSDK": True,
    "applyTypeId": "107",
    "closebtn": "y",
    "applySrc": "drecomm_profile",
}


# ============================================================================
# Answer Cache — persists across sessions
# ============================================================================


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def _cache_key(question_name: str, answer_option: dict) -> str:
    return f"{question_name}_{json.dumps(answer_option, sort_keys=True)}"


# ============================================================================
# Browser State — for login and browser-based tools only
# ============================================================================


class NaukriBrowser:
    """Playwright browser for login + cookie extraction. Lives entire MCP session."""

    def __init__(self):
        self.pw = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.token: Optional[str] = None  # nauk_at JWT
        self._lock = asyncio.Lock()

    async def start(self):
        self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE,
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self._extract_token()
        logger.info("Browser started, token: %s", "found" if self.token else "none")

    async def stop(self):
        if self.context:
            await self.context.close()
        if self.pw:
            await self.pw.stop()
        logger.info("Browser stopped")

    async def _extract_token(self) -> Optional[str]:
        """Extract nauk_at JWT from browser cookies."""
        try:
            cookies = await self.context.cookies(NAUKRI_BASE)
            for c in cookies:
                if c["name"] == "nauk_at":
                    self.token = c["value"]
                    return self.token
        except Exception:
            pass
        self.token = None
        return None

    async def ensure_token(self) -> str:
        """Get fresh token, raising if not logged in."""
        await self._extract_token()
        if not self.token:
            raise ValueError("Not logged in — call naukri_login first")
        return self.token

    async def goto(self, url: str, wait: str = "domcontentloaded") -> None:
        try:
            await self.page.goto(url, wait_until=wait, timeout=NAV_TIMEOUT)
        except Exception:
            await self.page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT)

    async def text(self, selector: str) -> Optional[str]:
        el = await self.page.query_selector(selector)
        return (await el.text_content()).strip() if el else None

    async def exists(self, selector: str) -> bool:
        return await self.page.query_selector(selector) is not None

    async def safe_fill(self, selector: str, value: str, delay: int = 30):
        el = await self.page.wait_for_selector(selector, timeout=ELEMENT_TIMEOUT)
        await el.click()
        await el.fill("")
        await el.type(value, delay=delay)


browser = NaukriBrowser()


# ============================================================================
# API Helpers — two strategies for different endpoints
# ============================================================================


async def _cookie_header() -> str:
    """Build raw Cookie header string from all browser cookies."""
    try:
        cookies = await browser.context.cookies(NAUKRI_BASE)
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    except Exception:
        return ""


async def api_get(path: str, params: dict = None) -> dict:
    """GET request to Naukri API (aiohttp — for non-reCAPTCHA endpoints)."""
    token = await browser.ensure_token()
    cookie_str = await _cookie_header()
    headers = {**API_HEADERS, "Authorization": f"Bearer {token}", "cookie": cookie_str}
    url = f"{NAUKRI_BASE}{path}" if path.startswith("/") else path
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    logger.info("API GET %s", url)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        trust_env=True,
    ) as session:
        async with session.get(url, headers=headers) as resp:
            logger.info("API GET %s -> %s", path, resp.status)
            if resp.status == 401:
                raise ValueError("Token expired — call naukri_login to re-authenticate")
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {text[:500]}")
            return await resp.json()


async def api_post(path: str, body: dict) -> dict:
    """POST request to Naukri API (aiohttp — for non-reCAPTCHA endpoints)."""
    token = await browser.ensure_token()
    cookie_str = await _cookie_header()
    headers = {**API_HEADERS, "Authorization": f"Bearer {token}", "cookie": cookie_str}
    url = f"{NAUKRI_BASE}{path}" if path.startswith("/") else path
    logger.info("API POST %s", url)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        trust_env=True,
    ) as session:
        async with session.post(url, headers=headers, json=body) as resp:
            logger.info("API POST %s -> %s", path, resp.status)
            if resp.status == 401:
                raise ValueError("Token expired — call naukri_login to re-authenticate")
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {text[:500]}")
            return await resp.json()


# ============================================================================
# MCP Lifespan
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
# Tool 1: Login (Playwright — handles Google SSO visually)
# ============================================================================


@mcp.tool()
async def naukri_login(
    method: str = "google",
    email: Optional[str] = None,
    password: Optional[str] = None,
) -> dict:
    """Login to Naukri.com. Supports Google SSO (recommended) or email/password.

    Google SSO uses the Chrome profile's saved Google session — no credentials
    needed. Cookies and API token persist across sessions (~30 days).

    Args:
        method: "google" (recommended) or "email"
        email: Naukri email (only for method="email")
        password: Naukri password (only for method="email")
    """
    async with browser._lock:
        await browser.goto(f"{NAUKRI_BASE}/nlogin/login")
        await asyncio.sleep(2)

        if "/nlogin" not in browser.page.url:
            token = await browser._extract_token()
            name = await browser.text(".nI-gNb-sb__main-text")
            return {"status": "already_logged_in", "profile_name": name or "unknown", "has_token": bool(token)}

        if method == "google":
            google_clicked = False
            for selector in [
                "button:has-text('Google')", "a:has-text('Google')",
                "[class*='google']", "button[data-ga-track*='google']",
                "[class*='social'] button:first-child", "img[alt*='Google']",
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
                return {"status": "error", "message": "Google login button not found. Try method='email'."}

            await asyncio.sleep(5)

            if "/nlogin" not in browser.page.url and "accounts.google" not in browser.page.url:
                token = await browser._extract_token()
                name = await browser.text(".nI-gNb-sb__main-text")
                return {"status": "logged_in", "method": "google", "profile_name": name or "unknown", "has_token": bool(token)}

            if "accounts.google" in browser.page.url:
                return {"status": "waiting_for_user", "message": "Google account picker is open. Select your account, then call naukri_login() again."}

            await asyncio.sleep(3)
            if "/nlogin" not in browser.page.url:
                token = await browser._extract_token()
                name = await browser.text(".nI-gNb-sb__main-text")
                return {"status": "logged_in", "method": "google", "profile_name": name or "unknown", "has_token": bool(token)}

            return {"status": "error", "message": "Google login did not complete. Check the browser."}

        else:
            if not email or not password:
                return {"status": "error", "message": "email and password required for method='email'"}
            try:
                await browser.safe_fill("input#usernameField", email)
                await browser.safe_fill("input#passwordField", password)
                await browser.page.click("button[type='submit']")
            except Exception as e:
                return {"status": "error", "message": f"Login form error: {e}"}

            await asyncio.sleep(3)

            otp_field = await browser.page.query_selector("input#otp")
            if otp_field:
                return {"status": "otp_required", "needs_otp": True, "message": "Enter OTP sent to your phone"}

            if "/nlogin" not in browser.page.url:
                token = await browser._extract_token()
                name = await browser.text(".nI-gNb-sb__main-text")
                return {"status": "logged_in", "method": "email", "profile_name": name or "unknown", "has_token": bool(token)}

            error = await browser.text(".err-message, .error-msg, [class*='error']")
            return {"status": "error", "message": error or "Login failed — check credentials"}


# ============================================================================
# Tool 2: Verify OTP (Playwright)
# ============================================================================


@mcp.tool()
async def naukri_verify_otp(otp: str) -> dict:
    """Enter OTP code after login. Call after naukri_login returns needs_otp.

    Args:
        otp: 6-digit OTP from SMS/email
    """
    async with browser._lock:
        try:
            await browser.safe_fill("input#otp", otp)
            await browser.page.click("button[type='submit']")
            await asyncio.sleep(3)

            if "/nlogin" not in browser.page.url:
                token = await browser._extract_token()
                name = await browser.text(".nI-gNb-sb__main-text")
                return {"status": "logged_in", "profile_name": name or "unknown", "has_token": bool(token)}

            error = await browser.text(".err-message, .error-msg")
            return {"status": "error", "message": error or "OTP verification failed"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ============================================================================
# Tool 3: Search Jobs (Browser intercept — captures API JSON response)
# ============================================================================


@mcp.tool()
async def naukri_search_jobs(
    keywords: str,
    location: Optional[str] = None,
    experience: Optional[int] = None,
    limit: int = 20,
) -> dict:
    """Search for jobs on Naukri.com.

    Navigates to the search page and intercepts the structured JSON response
    that Naukri's frontend receives from its search API.

    Args:
        keywords: Job title or skills (e.g., "python developer", "react")
        location: City name (e.g., "Bangalore", "Mumbai", "Remote")
        experience: Years of experience filter (e.g., 5)
        limit: Max jobs to return (default 20, max 50)

    Returns list of jobs with id, title, company, salary, location, URL.
    """
    async with browser._lock:
        try:
            # Build Naukri search URL (SEO-friendly format)
            slug = keywords.lower().replace(" ", "-").replace(".", "-")
            if location:
                loc_slug = location.lower().replace(" ", "-")
                page_url = f"{NAUKRI_BASE}/{slug}-jobs-in-{loc_slug}"
            else:
                page_url = f"{NAUKRI_BASE}/{slug}-jobs"
            if experience is not None:
                page_url += f"?experience={experience}"

            # Intercept the search API response that Naukri's frontend makes
            captured = {}

            async def on_response(response):
                if "/jobapi/v3/search" in response.url and response.status == 200:
                    try:
                        captured["data"] = await response.json()
                    except Exception:
                        pass

            browser.page.on("response", on_response)
            try:
                await browser.goto(page_url)
                await asyncio.sleep(4)
            finally:
                browser.page.remove_listener("response", on_response)

            data = captured.get("data")
            if not data:
                return {"status": "error", "message": "Search API response not captured. Page may not have loaded correctly."}

            # Parse structured JSON from the intercepted response
            jobs = []
            for job in data.get("jobDetails", [])[:limit]:
                salary = job.get("salaryDetail", {})
                sal_min = salary.get("minimumSalary", 0)
                sal_max = salary.get("maximumSalary", 0)
                sal_label = salary.get("label", "")
                salary_str = sal_label if sal_label else (
                    f"{sal_min/100000:.1f}-{sal_max/100000:.1f} LPA" if sal_max else "Not Disclosed"
                )

                placeholders = job.get("placeholders", [])
                loc_label = None
                for ph in placeholders:
                    if ph.get("type") == "location":
                        loc_label = ph.get("label")
                        break
                if not loc_label and placeholders:
                    loc_label = placeholders[0].get("label")

                jobs.append({
                    "job_id": job.get("jobId"),
                    "title": job.get("title"),
                    "company": job.get("companyName"),
                    "salary": salary_str,
                    "location": loc_label,
                    "experience": f"{job.get('minimumExperience', '?')}-{job.get('maximumExperience', '?')} Yrs",
                    "is_applied": job.get("isApplied", False),
                    "url": f"{NAUKRI_BASE}/job-listings-{job.get('jobId', '')}",
                })

            return {
                "status": "success",
                "keywords": keywords,
                "location": location,
                "total_found": data.get("noOfJobs"),
                "count": len(jobs),
                "jobs": jobs,
            }
        except Exception as e:
            return {"status": "error", "message": f"Search failed: {type(e).__name__}: {e!r}"}


# ============================================================================
# Tool 4: Get Job Details (REST API)
# ============================================================================


def _extract_job_id(job_url_or_id: str) -> str:
    """Extract numeric job ID from URL or pass through if already an ID."""
    if job_url_or_id.isdigit():
        return job_url_or_id
    # URL pattern: ...-<jobId> at the end
    match = re.search(r'(\d{10,})', job_url_or_id)
    return match.group(1) if match else job_url_or_id


@mcp.tool()
async def naukri_get_job(job_url: str) -> dict:
    """Get full details for a specific Naukri job.

    Navigates to the job page and intercepts the structured JSON response
    that Naukri's frontend receives from its job details API.

    Args:
        job_url: Naukri job URL or job ID (numeric)

    Returns title, company, description, skills, salary, match score,
    and whether you can apply.
    """
    async with browser._lock:
        try:
            # Build URL if just an ID was passed
            if job_url.isdigit():
                page_url = f"{NAUKRI_BASE}/job-listings-{job_url}"
            elif not job_url.startswith("http"):
                page_url = f"{NAUKRI_BASE}/job-listings-{job_url}"
            else:
                page_url = job_url

            job_id = _extract_job_id(job_url)

            # Intercept job details + match score API responses
            captured = {}

            async def on_response(response):
                try:
                    if f"/jobapi/v4/job/{job_id}" in response.url and response.status == 200:
                        captured["details"] = await response.json()
                    elif f"/job/{job_id}/matchscore" in response.url and response.status == 200:
                        captured["score"] = await response.json()
                except Exception:
                    pass

            browser.page.on("response", on_response)
            try:
                await browser.goto(page_url)
                await asyncio.sleep(4)
            finally:
                browser.page.remove_listener("response", on_response)

            details_data = captured.get("details")
            if not details_data:
                return {"status": "error", "message": "Job details API response not captured. Page may not have loaded."}

            job = details_data.get("jobDetails", details_data)

            salary = job.get("salaryDetail", {})
            sal_label = salary.get("label", "")
            sal_min = salary.get("minimumSalary", 0)
            sal_max = salary.get("maximumSalary", 0)
            salary_str = sal_label if sal_label else (
                f"{sal_min/100000:.1f}-{sal_max/100000:.1f} LPA" if sal_max else "Not Disclosed"
            )

            company = job.get("companyDetail", {})
            is_applied = job.get("isApplied", False)
            external = bool(job.get("applyRedirectUrl"))

            match_score = None
            score_data = captured.get("score")
            if score_data:
                match_score = score_data.get("Keyskills")

            return {
                "status": "success",
                "job_id": job_id,
                "title": job.get("title"),
                "company": company.get("name"),
                "salary": salary_str,
                "experience": f"{job.get('minimumExperience', '?')}-{job.get('maximumExperience', '?')} years",
                "location": job.get("cityName") or job.get("citySuburb"),
                "description": job.get("description", ""),
                "skills": [s.get("label", s) if isinstance(s, dict) else s for s in job.get("keySkills", [])],
                "match_score": match_score,
                "is_applied": is_applied,
                "external_apply": external,
                "can_apply": not is_applied and not external,
                "vacancies": job.get("vacany"),
                "apply_count": job.get("applyCount"),
                "url": page_url,
            }
        except Exception as e:
            return {"status": "error", "message": f"Get job failed: {type(e).__name__}: {e!r}"}


# ============================================================================
# Tool 5: Apply to Job (REST API — two-phase)
# ============================================================================


@mcp.tool()
async def naukri_apply(
    job_id: str,
    answers: Optional[dict] = None,
) -> dict:
    """Apply to a Naukri job via API. Two-phase flow:

    Phase 1 (no answers): Sends apply request. If the job has screening questions,
    returns them with options. If no questions, applies instantly.

    Phase 2 (with answers): Provide answers dict keyed by question text or question ID.
    Submits answers and completes application.

    Args:
        job_id: Naukri job ID (numeric string, from search results or get_job)
        answers: Dict of answers for screening questions. Keys can be:
                 - Question ID (from phase 1 response): {"12345": "5"}
                 - Question text substring: {"current ctc": "16", "notice period": "30"}
                 For radio/list/checkbox: value is the option text to select.
                 For text box: value is the string to enter.

    Returns:
        - {status: "applied"} — success
        - {status: "needs_input", questions: [...]} — answer the questions and call again
        - {status: "already_applied"} — already applied
        - {status: "error", message: "..."} — failure
    """
    if answers is None:
        answers = {}

    job_id = _extract_job_id(job_id)

    try:
        # Load answer cache
        cache = _load_cache()

        # Build apply body
        body = {
            "strJobsarr": [job_id],
            **APPLY_TRAILER,
        }

        # If we have answers, build applyData
        if answers:
            apply_answers = _build_apply_answers(job_id, answers, cache)
            if apply_answers:
                body["applyData"] = {job_id: {"answers": apply_answers}}

        # POST to apply endpoint
        data = await api_post(
            "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply",
            body,
        )

        # Parse response
        jobs = data.get("jobs", [])
        if not jobs:
            # Might mean already applied
            msg = data.get("message", "")
            if "already" in msg.lower():
                return {"status": "already_applied", "job_id": job_id}
            return {"status": "error", "message": msg or "Unexpected response from apply API", "raw": data}

        job_result = jobs[0]
        status_code = job_result.get("status")
        questionnaire = job_result.get("questionnaire", [])

        if status_code == 200:
            # Applied successfully — cache any answered questions
            if questionnaire and answers:
                _cache_answers(questionnaire, answers, cache)
                _save_cache(cache)
            return {
                "status": "applied",
                "job_id": job_id,
                "message": "Successfully applied!",
                "daily_applied": data.get("quotaDetails", {}).get("dailyApplied"),
            }

        if questionnaire:
            # Questions returned — try to auto-answer from cache
            pending = []
            auto_answers = {}

            for q in questionnaire:
                qid = str(q.get("questionId", ""))
                q_name = q.get("questionName", "")
                q_type = q.get("questionType", "")
                options = q.get("answerOption", {})
                cache_k = _cache_key(q_name, options)

                # Check cache first
                cached = cache.get(cache_k)
                if cached:
                    auto_answers[qid] = cached["answer"]
                    continue

                # Check if answer was provided by user
                user_answer = _find_user_answer(qid, q_name, answers)
                if user_answer is not None:
                    formatted = _format_answer(user_answer, q_type, options)
                    auto_answers[qid] = formatted
                    # Cache it
                    cache[cache_k] = {
                        "questionType": q_type,
                        "questionName": q_name,
                        "answer": formatted,
                    }
                    continue

                # Not in cache or answers — return to user
                pending.append({
                    "question_id": qid,
                    "question": q_name,
                    "type": q_type,
                    "options": options,
                })

            # If we resolved all questions, re-apply with answers
            if not pending and auto_answers:
                _save_cache(cache)
                body["applyData"] = {job_id: {"answers": auto_answers}}
                data2 = await api_post(
                    "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply",
                    body,
                )
                jobs2 = data2.get("jobs", [])
                if jobs2 and jobs2[0].get("status") == 200:
                    return {
                        "status": "applied",
                        "job_id": job_id,
                        "message": "Applied with cached/auto answers!",
                        "questions_answered": len(auto_answers),
                        "daily_applied": data2.get("quotaDetails", {}).get("dailyApplied"),
                    }

            if pending:
                _save_cache(cache)
                return {
                    "status": "needs_input",
                    "job_id": job_id,
                    "questions": pending,
                    "auto_answered": len(auto_answers),
                    "message": f"{len(pending)} question(s) need your input. Provide answers and call again.",
                }

        return {
            "status": "error",
            "job_id": job_id,
            "message": f"Apply returned status {status_code}",
            "raw": job_result,
        }

    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Apply failed: {type(e).__name__}: {e!r}"}


def _find_user_answer(qid: str, q_name: str, answers: dict) -> Optional[str]:
    """Find a user-provided answer by question ID or fuzzy text match."""
    # Exact ID match
    if qid in answers:
        return str(answers[qid])

    # Fuzzy text match on question name
    q_lower = q_name.lower()
    for key, value in answers.items():
        k = key.lower().replace("_", " ")
        if k in q_lower or q_lower in k:
            return str(value)
        if all(w in q_lower for w in k.split()):
            return str(value)
    return None


def _format_answer(answer: str, q_type: str, options: dict) -> any:
    """Format answer for the Naukri API based on question type."""
    if q_type == "Text Box":
        return answer

    # For Radio/List/Check — answer must be wrapped in a list
    # Try to match answer to an option value
    option_values = list(options.values())
    for opt in option_values:
        if answer.lower() == opt.lower() or answer.lower() in opt.lower():
            return [opt] if q_type != "Check Box" else [opt]

    # If answer is a number, try to match option key
    if answer in options:
        return [options[answer]]

    # Fallback: wrap in list as-is
    return [answer] if q_type != "Text Box" else answer


def _build_apply_answers(job_id: str, answers: dict, cache: dict) -> dict:
    """Build answers dict from user-provided answers (for direct submission)."""
    result = {}
    for key, value in answers.items():
        # If key looks like a question ID (numeric), use directly
        if str(key).isdigit():
            result[str(key)] = value if isinstance(value, list) else str(value)
    return result


def _cache_answers(questionnaire: list, answers: dict, cache: dict):
    """Cache successfully answered questions."""
    for q in questionnaire:
        qid = str(q.get("questionId", ""))
        q_name = q.get("questionName", "")
        q_type = q.get("questionType", "")
        options = q.get("answerOption", {})

        answer = _find_user_answer(qid, q_name, answers)
        if answer:
            cache_k = _cache_key(q_name, options)
            if cache_k not in cache:
                cache[cache_k] = {
                    "questionType": q_type,
                    "questionName": q_name,
                    "answer": _format_answer(answer, q_type, options),
                }


# ============================================================================
# Tool 6: Refresh Profile (Playwright — needs browser interaction)
# ============================================================================


@mcp.tool()
async def naukri_refresh_profile() -> dict:
    """Refresh Naukri profile to boost visibility (daily trick).

    Opens Resume Headline editor and saves without changes.
    Triggers Naukri's 'recently active' signal — recruiters see you first.
    """
    async with browser._lock:
        await browser.goto(f"{NAUKRI_BASE}/mnjuser/profile")
        await asyncio.sleep(3)

        if "/nlogin" in browser.page.url:
            return {"status": "error", "message": "Not logged in. Call naukri_login first."}

        edit_clicked = await browser.page.evaluate("""() => {
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

        save_result = await browser.page.evaluate("""() => {
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

        await asyncio.sleep(2)
        return {"status": "refreshed", "method": edit_clicked, "save": save_result, "message": "Profile refreshed. You appear as 'recently active'."}


# ============================================================================
# Tool 7: Debug (Playwright)
# ============================================================================


@mcp.tool()
async def naukri_debug(action: str = "snapshot") -> dict:
    """Debug tool: capture current page state for troubleshooting.

    Args:
        action: "snapshot" — DOM structure | "screenshot" — saves debug.png
    """
    async with browser._lock:
        url = browser.page.url
        title = await browser.page.title()

        if action == "screenshot":
            path = str(Path(__file__).parent / "debug.png")
            await browser.page.screenshot(path=path, full_page=False)
            return {"status": "ok", "url": url, "title": title, "screenshot": path}

        structure = await browser.page.evaluate("""() => {
            const selectors = [
                '[class*="tuple"]', '[class*="jobCard"]', '[data-job-id]',
                '[class*="title"]', '[class*="comp"]', '[class*="salary"]',
                '[class*="location"]', '[class*="apply"]', 'button',
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
                        }));
                    }
                } catch(e) {}
            }
            return results;
        }""")

        return {"status": "ok", "url": url, "title": title, "dom_structure": structure}


# ============================================================================
# Tool 8: Get Profile (REST API)
# ============================================================================


@mcp.tool()
async def naukri_get_profile() -> dict:
    """Get your full Naukri profile via API.

    Returns skills (with experience years), employment history, education,
    current CTC, expected CTC, notice period, location — everything needed
    for Claude to auto-answer screening questions intelligently.
    """
    try:
        data = await api_get(
            "/cloudgateway-mynaukri/resman-aggregator-services/v2/users/self",
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

        return {
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
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Profile API failed: {type(e).__name__}: {e!r}"}


# ============================================================================
# Server Startup
# ============================================================================

if __name__ == "__main__":
    logger.info("Starting Naukri MCP Server v2 (8 tools, hybrid Playwright + REST API)")
    mcp.run()
