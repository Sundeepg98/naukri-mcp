# Naukri MCP Server

86-tool MCP server for automating [Naukri.com](https://www.naukri.com) (India's largest job portal). Search jobs, apply in bulk, manage your profile, track applications, research companies, and monitor recruiter activity -- all from your MCP client.

**Tech stack:** Python 3.10+, [FastMCP](https://github.com/jlowin/fastmcp), Playwright (persistent Chromium), aiohttp

**Key capabilities:**

- **Search & Apply** -- keyword search, personalized recommendations, single or batch apply with auto-answered screening questions
- **Application Tracking** -- local JSON persistence + 3-tier sync from Naukri's backend (REST, browser intercept, HTML scrape)
- **Profile Management** -- view/edit profile, boost visibility to appear "recently active" to recruiters
- **Company Research** -- unified `research_company` tool plus AmbitionBox bridge for salary data and employee reviews
- **Performance Analytics** -- search impressions, recruiter activity (who viewed/downloaded/contacted you), match scores
- **Smart Automation** -- `auto_hunt` (one-call job hunting with fit scoring), `daily_brief` (morning dashboard), `resume_tailor`

---

## Architecture

```
naukri.py                    # Entry point (FastMCP run)
naukri_server/
  __init__.py                # FastMCP setup + lifespan (browser start/stop)
  config.py                  # Constants, API endpoints, timeouts
  browser.py                 # PagePool (3 tabs) + TokenManager (JWT caching)
  api.py                     # Deduplicated _api_request, @api_tool decorator
  cache.py                   # Answer cache for auto-apply screening questions
  scoring.py                 # Alias-aware fit scoring
  validation.py              # Response validators (job lists, profiles, etc.)
  utils.py                   # Shared helpers
  tools/                     # 33 tool modules
    auth.py                  # Login, OTP verification, login status
    search.py                # Job search, recommendations, similar jobs
    jobs.py                  # Job detail (REST + browser dual strategy)
    apply.py                 # Single apply + batch apply with auto-answer
    profile.py               # Profile CRUD, dashboard, boost, audit
    tracking.py              # Application tracking, saved jobs, match analytics
    sync.py                  # Sync applications/saved jobs from Naukri backend
    inbox.py                 # Recruiter messages, NVites, mark_interested
    notifications.py         # Notification feed, mark read, count
    settings.py              # Account settings, blocked companies, email verify
    alerts.py                # Job alert CRUD (browser-based)
    companies.py             # Company search, jobs, follow, follow status
    ambitionbox.py           # Salary data, reviews, slug bridge (AmbitionBox)
    performance.py           # Search impressions, recruiter activity
    insights.py              # Application insights, cached answers, salary position
    research.py              # Unified company research
    daily_brief.py           # Morning dashboard summary
    smart_apply.py           # Smart apply with fit scoring
    compare.py               # Side-by-side job comparison
    auto_hunt.py             # One-call automated job hunting
    skill_gap.py             # Skill gap analysis
    resume_tailor.py         # Resume tailoring suggestions
    export.py                # Export application data
    resume_photo.py          # Resume info, photo upload/delete, download
    upload.py                # Resume upload
    assessments.py           # Skill assessments, profile completeness
    subscription.py          # Naukri 360 status
    mock_interview.py        # AI mock interview topics, sessions, history
    resume_builder.py        # Resume templates, builder status
    early_access.py          # Pre-posted roles from top companies
    debug/                   # Multi-action debug tool (16 actions)
    health.py                # Endpoint validation, browser pool, AmbitionBox checks
```

### Hybrid Browser + REST Strategy

Naukri's Akamai CDN blocks direct REST calls for several endpoints. The server uses a hybrid approach:

| Strategy | Used By | Why |
|----------|---------|-----|
| **Direct REST API** | `apply`, `get_profile`, `get_recommendations`, `sync_applications`, most reads | Fast, no browser tab needed. Uses JWT token extracted from browser cookies. |
| **Browser intercept** | `search_jobs`, `get_company_jobs`, `get_job` (fallback) | Search API returns 406 on direct REST. Browser navigates to the page and intercepts the XHR response. |
| **Browser UI automation** | `login`, `boost_visibility`, `update_profile`, `alerts` | Requires clicking buttons, filling forms, handling SSO popups. Akamai blocks PUT/DELETE via REST. |
| **AmbitionBox scraping** | `get_company_salary`, `get_company_reviews` | Extracts `__NEXT_DATA__` from server-rendered Next.js pages. |

### PagePool

The server maintains a pool of 3 browser tabs (configurable via `NAUKRI_MAX_TABS`). Tabs are checked out with a semaphore, auto-recovered if crashed, and returned after use. This allows concurrent operations like batch apply to run in parallel without opening excessive tabs.

### TokenManager

JWT authentication token (`nauk_at` cookie) is extracted from the Playwright browser context and cached in memory. On 401 errors, a single-writer refresh lock prevents parallel refresh storms -- one request refreshes, others wait and reuse the result.

### 3-Tier Sync Fallback

`naukri_sync_applications` tries three strategies in order:

1. **REST API** -- paginated GET to the history endpoint (fastest, most reliable)
2. **Browser intercept** -- navigate to the applied-jobs page and capture the XHR response
3. **HTML scrape** -- extract job cards from the server-rendered DOM using adaptive CSS selectors

---

## Quick Start for AI Consumers

```
1.  naukri_login / naukri_get_login_status      # Authenticate (Google SSO or email)
2.  naukri_daily_brief                          # Morning dashboard: recommendations + analytics
3.  naukri_auto_hunt(keywords="...", location="...")  # One-call job hunt with fit scoring
4.  naukri_smart_apply("job_id")                # Pre-flight check before applying
    naukri_apply("job_id")                      # Submit application
5.  naukri_compare_jobs(["id1", "id2", "id3"])  # Side-by-side with fit scores
6.  naukri_accept_nvite("nvite_id")             # Respond to recruiter NVites
7.  naukri_sync_applications()                  # Pull latest from Naukri backend
    naukri_get_applications()                   # Query local tracking
8.  naukri_research_company("company_name")     # Unified: Naukri + AmbitionBox data
    naukri_get_interview_experiences("slug")    # Interview tips from employees
9.  naukri_resume_tailor("job_id")              # Get tailoring suggestions
    naukri_update_profile(...)                  # Apply them
10. naukri_download_resume()                    # Download current Naukri resume
```

**Apply flow detail:** If a job has screening questions, the first `naukri_apply` call returns them. Pass answers back in the second call. Answer keys are fuzzy-matched -- `"current ctc"` matches `"What is your current CTC?"`. Answers are cached in `questions.json` so you only answer each question type once.

---

## Tools (86)

| Category | Count | Tools |
|----------|-------|-------|
| Auth | 3 | `naukri_login`, `naukri_verify_otp`, `naukri_get_login_status` |
| Search | 3 | `naukri_search_jobs`, `naukri_get_recommendations`, `naukri_get_similar_jobs` |
| Jobs | 2 | `naukri_get_job`, `naukri_report_fraud_job` |
| Apply | 2 | `naukri_apply`, `naukri_batch_apply` |
| Tracking | 7 | `naukri_get_applications`, `naukri_get_application_status`, `naukri_get_match_analytics`, `naukri_save_job`, `naukri_unsave_job`, `naukri_get_saved_jobs`, `naukri_purge_applications` |
| Sync | 2 | `naukri_sync_applications`, `naukri_sync_saved_jobs` |
| Profile | 5 | `naukri_get_profile`, `naukri_update_profile`, `naukri_boost_visibility`, `naukri_get_dashboard`, `naukri_audit_profile` |
| Resume & Photo | 5 | `naukri_get_resume_info`, `naukri_download_resume`, `naukri_get_photo_info`, `naukri_upload_photo`, `naukri_delete_photo` |
| Settings | 4 | `naukri_get_settings`, `naukri_update_settings`, `naukri_get_blocked_companies`, `naukri_check_email_verification` |
| Alerts | 5 | `naukri_create_job_alert`, `naukri_get_job_alerts`, `naukri_get_alert_detail`, `naukri_delete_job_alert`, `naukri_update_job_alert` |
| Companies | 4 | `naukri_search_companies`, `naukri_get_company_jobs`, `naukri_follow_company`, `naukri_get_company_follow_status` |
| Inbox | 4 | `naukri_get_inbox`, `naukri_read_message`, `naukri_accept_nvite`, `naukri_mark_interested` |
| Notifications | 4 | `naukri_get_notifications`, `naukri_mark_notification_read`, `naukri_mark_all_notifications_read`, `naukri_get_notification_count` |
| Performance | 3 | `naukri_get_search_impressions`, `naukri_get_recruiter_activity`, `naukri_get_activity_level` |
| AmbitionBox | 4 | `naukri_get_company_slug`, `naukri_get_company_salary`, `naukri_get_company_reviews`, `naukri_get_interview_experiences` |
| Insights | 5 | `naukri_get_application_insights`, `naukri_review_cached_answers`, `naukri_analyze_salary_position`, `naukri_delete_cached_answer`, `naukri_update_cached_answer` |
| Research | 1 | `naukri_research_company` |
| Smart Tools | 3 | `naukri_daily_brief`, `naukri_smart_apply`, `naukri_compare_jobs` |
| Automation | 4 | `naukri_auto_hunt`, `naukri_skill_gap_analysis`, `naukri_export_data`, `naukri_resume_tailor` |
| Upload | 1 | `naukri_upload_resume` |
| Assessments | 2 | `naukri_get_assessments`, `naukri_get_profile_completeness` |
| Mock Interview | 4 | `naukri_get_mock_interview_topics`, `naukri_start_mock_interview`, `naukri_answer_mock_question`, `naukri_get_mock_interview_history` |
| Resume Builder | 2 | `naukri_get_resume_templates`, `naukri_get_resume_builder_status` |
| Early Access | 2 | `naukri_get_early_access_roles`, (early access apply) |
| Subscription | 1 | `naukri_get_subscription_status` |
| Debug | 3 | `naukri_debug_browser`, `naukri_debug_api`, `naukri_debug_discovery` (16 actions total) |
| Health | 1 | `naukri_health_check` |

---

## Setup

### Prerequisites

- Python 3.10+
- Playwright Chromium (installed via `playwright install chromium`)

### Installation

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### First Login

Start the server:

```bash
python naukri.py
```

Then call `naukri_login` from your MCP client. It opens a visible Chromium window where you can:

1. **Google SSO (recommended):** Click "Login with Google" -- uses the Chrome profile's saved Google session, no credentials needed.
2. **Email/password:** Pass `method="email"`, `email="..."`, `password="..."` to the tool.

The browser session is stored in `chrome-profile/` (auto-created, gitignored). This directory is machine-specific -- it contains cookies, local storage, and cached credentials. Do not copy it between machines.

### Session Lifetime

Sessions persist for approximately 30 days. When expired, the server detects it at startup or on the first API call and returns a `"Not logged in"` error. Re-authenticate with `naukri_login`.

### MCP Client Config

```json
{
  "mcpServers": {
    "naukri": {
      "command": "python",
      "args": ["naukri.py"],
      "cwd": "/path/to/mcp-servers/naukri"
    }
  }
}
```

### Environment Variables

All optional. Set in shell or a `.env` file.

| Variable | Default | Description |
|----------|---------|-------------|
| `NAUKRI_NAV_TIMEOUT` | `20000` | Playwright page navigation timeout (ms) |
| `NAUKRI_ELEMENT_TIMEOUT` | `5000` | Playwright element wait timeout (ms) |
| `NAUKRI_API_TIMEOUT` | `30` | aiohttp REST API timeout (seconds) |
| `NAUKRI_MAX_TABS` | `3` | Max concurrent browser tabs in the PagePool |

### Data File Locations

All data files live in the project root and are gitignored.

| File | Purpose |
|------|---------|
| `chrome-profile/` | Playwright persistent browser profile. Machine-specific, never commit. |
| `applications.json` | Local application tracking. Written by `apply`, `batch_apply`, and `sync_applications`. |
| `saved_jobs.json` | Local saved/bookmarked jobs. Written by `save_job` and `sync_saved_jobs`. |
| `questions.json` | Screening question answer cache. Auto-populated on apply, used by batch apply for auto-answering. |
| `*.backup` | Automatic backup before any JSON file overwrite (atomic write: write to `.tmp`, backup existing, rename). |

---

## Resilience Features

- **Global aiohttp session** -- single shared session for all REST calls, avoids connection overhead
- **Deduplicated API layer** -- `_api_request` with `@api_tool` decorator normalizes all REST interactions
- **Refresh lock** -- single-writer JWT refresh prevents parallel 401 storms
- **Startup validation** -- browser and token state validated before accepting tool calls
- **Batch apply cancellation safety** -- partial progress preserved if batch is interrupted
- **Data backup** -- `.backup` files created before every JSON overwrite
- **Cache TTL auto-purge** -- stale answer cache entries expire automatically
- **Atomic writes** -- sync state written via temp file + rename to avoid corruption
- **Profile TTL cache** -- profile data cached for 30 seconds to reduce redundant API calls

---

## Known Limitations

### Akamai CDN Blocks

Naukri uses Akamai Bot Manager. Several endpoints return `406 Not Acceptable` or `403 Forbidden` when called directly via REST without a browser session:

- **Search** (`search_jobs`) -- always uses browser intercept; direct REST is blocked
- **Profile mutations** (`update_profile`) -- PUT/DELETE blocked by Akamai; browser automation used instead
- **Job alerts** -- CRUD operations go through browser UI automation for the same reason

This is expected behavior. Tools that require browser interaction are documented as such. If you see 406 errors from tools that should use REST, check login status with `naukri_get_login_status` -- an expired token causes Akamai to classify requests as bot traffic.

### AmbitionBox Scraping

AmbitionBox is a Next.js SSR site. Salary and review tools extract `__NEXT_DATA__` from server-rendered pages. If AmbitionBox changes their page structure, these tools may return errors. `naukri_health_check` includes an AmbitionBox check -- a "warn" status there is expected periodically and not a blocker for core Naukri functionality.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **"Not logged in" errors** | Session expired (~30 days). Call `naukri_login` to re-authenticate. |
| **Search returns empty / 406** | Expected for direct REST. `naukri_search_jobs` uses browser intercept and should work. If it fails, run `naukri_health_check`. |
| **Timeouts on slow connections** | Increase `NAUKRI_NAV_TIMEOUT` (e.g., `30000`) and `NAUKRI_API_TIMEOUT` (e.g., `60`). |
| **Rate limits / daily apply cap** | Naukri limits daily applications by account type. The `daily_applied` field in apply responses shows your count. Naukri 360 subscribers get higher limits. |
| **Browser tab crashes** | The PagePool auto-recovers crashed tabs on the next `acquire()`. If persistent, restart the server. |
| **Token refresh loops** | Delete `chrome-profile/` and re-authenticate from scratch. |
| **`naukri_sync_applications` fails all 3 tiers** | Usually means an invalid session. Log in first. If already logged in, pass `force_browser=True` to skip the REST tier. |
| **AmbitionBox salary/reviews broken** | Run `naukri_health_check` to confirm. If AmbitionBox returns "warn", core Naukri tools are unaffected. |

### Health Check

Run `naukri_health_check()` to validate all integrations at once. It tests login session, profile API, search API (406 is normal here), recommendations, dashboard, browser pool liveness, and AmbitionBox scraping.

Returns `{summary: {ok: N, warn: N, fail: N}, checks: [...]}` with per-check timing.
