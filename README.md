# Naukri MCP Server

61-tool MCP server for automating [Naukri.com](https://www.naukri.com) (India's largest job portal). Search jobs, apply in bulk, manage your profile, track applications, research companies, and monitor recruiter activity -- all from your MCP client.

**Tech stack:** Python 3.10+, [FastMCP](https://github.com/jlowin/fastmcp), Playwright (persistent Chromium), aiohttp

**Key capabilities:**

- **Search & Apply** -- keyword search, personalized recommendations, single or batch apply with auto-answered screening questions
- **Application Tracking** -- local JSON persistence + 3-tier sync from Naukri's backend (REST, browser intercept, HTML scrape)
- **Profile Management** -- view/edit profile, boost visibility to appear "recently active" to recruiters
- **Company Research** -- search companies on Naukri, then bridge to AmbitionBox for salary data and employee reviews
- **Performance Analytics** -- search impressions, recruiter activity (who viewed/downloaded/contacted you), match scores

---

## Setup

### Prerequisites

- Python 3.10+
- Chrome or Chromium (Playwright installs its own Chromium, but the persistent profile needs a Chromium-based browser)

### Installation

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### First Login

```bash
python naukri.py
```

Then call the `naukri_login` tool from your MCP client. It opens a visible Chromium window where you can:

1. **Google SSO (recommended):** Click "Login with Google" -- uses the Chrome profile's saved Google session, no credentials needed.
2. **Email/password:** Pass `method="email"`, `email="..."`, `password="..."` to the tool.

The browser session is stored in `chrome-profile/` (auto-created, gitignored). This directory is **machine-specific** -- it contains cookies, local storage, and cached credentials. Do not copy it between machines.

### Session Lifetime

Sessions persist for approximately **30 days**. When expired, the server detects it at startup or on the first API call and returns a `"Not logged in"` error. Re-authenticate with `naukri_login`.

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

---

## Quick Start Workflows

### Workflow 1: Search and Apply

Search for jobs, inspect details, then apply. The apply flow is two-phase: if the job has screening questions, the first call returns them; the second call submits your answers.

```
1. naukri_search_jobs("python developer", location="Bangalore")
   -> {jobs: [{job_id: "123456", title: "Python Dev", company: "Acme", ...}, ...]}

2. naukri_get_job("123456")
   -> {title, company, salary, description, skills, match_score, can_apply, ...}

3. naukri_apply("123456")
   -> {status: "applied"}                              # No questions -- done!
   -> {status: "needs_input", questions: [...]}         # Has screening questions

4. naukri_apply("123456", answers={"current ctc": "16", "notice period": "30 days"})
   -> {status: "applied", questions_answered: 2}        # Answered and applied
```

**Answer keys are fuzzy-matched** -- `"current ctc"` matches `"What is your current CTC?"`. Answers are cached in `questions.json` so you only answer each question type once.

### Workflow 2: Batch Apply (Fastest)

Search and apply to multiple jobs in a single call. Filters out already-applied jobs, auto-answers screening questions from the cache, and applies in parallel.

```
naukri_batch_apply(
    keywords="react developer",
    location="Mumbai",
    limit=10,
    answers={"current ctc": "16", "notice period": "30 days"}
)
-> {status: "success", searched: 20, filtered: 10, applied: 8,
    already_applied: 1, needs_input: 1, pending_questions: [...]}
```

Jobs with unanswered questions are returned in `pending_questions` with deduplicated question text across jobs.

### Workflow 3: Profile Boost

Re-save your profile headline to trigger Naukri's "recently active" signal. Recruiters see recently active profiles first in search results.

```
1. naukri_boost_visibility()
   -> {status: "refreshed", message: "Profile refreshed. You appear as 'recently active'."}

2. naukri_get_search_impressions(days=7)
   -> {total_appearances: 142, recruiter_actions: 8, top_keywords: {...}}

3. naukri_get_activity_level()
   -> {level: "HIGH", logged_in: true, resume_updated: true}
```

### Workflow 4: Company Research

Bridge from Naukri's company tools (which use `group_id`) to AmbitionBox (which uses `company_slug`) for salary and review data.

```
1. naukri_search_companies("Google")
   -> {companies: [{group_id: "1234", name: "Google India", rating: 4.3, ...}]}

2. naukri_get_company_jobs("1234")
   -> {jobs: [{job_id, title, salary, ...}]}

3. naukri_get_company_slug("1234")
   -> {company_slug: "google", company_name: "Google India"}

4. naukri_get_company_salary("google")
   -> {avg_salary: 2500000, salaries: [{designation: "SWE", ctc: "25L", ...}]}

5. naukri_get_company_reviews("google")
   -> {overall_rating: 4.3, reviews: [{title, rating, pros, cons, ...}]}
```

### Workflow 5: Track Applications

Sync your application history from Naukri's backend into local tracking, then query and filter locally.

```
1. naukri_sync_applications()
   -> {method: "rest_api", total_remote: 87, new_added: 12, updated: 3}

2. naukri_get_applications(status="applied")
   -> {total: 45, summary: {by_status: {applied: 45, viewed_by_recruiter: 20, ...}}}

3. naukri_get_application_status("123456")
   -> {title, company, total_applicants: 230, recruiter_activity: "Active",
       status_timeline: [{status: "Applied", date: "..."}, {status: "Viewed", date: "..."}]}

4. naukri_get_recruiter_activity()
   -> {activities: [{recruiter_name: "Jane", company: "Acme", action: "VIEWED", date: "..."}]}
```

---

## Architecture

```
naukri.py                  # Entry point (FastMCP run)
naukri_server/
  __init__.py              # FastMCP setup + lifespan (browser start/stop)
  config.py                # Constants, API endpoints, timeouts
  browser.py               # PagePool + TokenManager (Playwright)
  api.py                   # REST helpers (GET/POST/PUT/DELETE with auth)
  cache.py                 # Answer cache for auto-apply screening questions
  validation.py            # Response validators (job lists, profiles, etc.)
  tools/                   # 24 tool modules
    auth.py                # Login, OTP verification, login status
    search.py              # Job search, recommendations, similar jobs
    jobs.py                # Job detail retrieval
    apply.py               # Single apply, batch apply with auto-answer
    profile.py             # Profile CRUD, dashboard, boost visibility
    tracking.py            # Application tracking, saved jobs, match analytics
    sync.py                # Sync applications/saved jobs from Naukri backend
    upload.py              # Resume upload
    resume_photo.py        # Resume info, photo upload/delete
    resume_builder.py      # Resume templates, builder status
    alerts.py              # Job alert CRUD
    companies.py           # Company search, jobs, follow, follow status
    ambitionbox.py         # Salary data, reviews, company slug bridge (AmbitionBox)
    settings.py            # Account settings, blocked companies
    inbox.py               # Recruiter messages, NVites
    notifications.py       # Notification feed, mark read, mark all read
    performance.py         # Search impressions, recruiter activity
    assessments.py         # Skill assessments, profile completeness
    subscription.py        # Naukri 360 status
    mock_interview.py      # AI mock interview topics, history
    extras.py              # Notification count, fraud report, email verify
    early_access.py        # Pre-posted roles from top companies
    debug/                 # Multi-action debug tool (16 actions)
    health.py              # Endpoint validation, browser pool, AmbitionBox checks
```

### Hybrid Browser + REST Strategy

Some Naukri endpoints are protected by reCAPTCHA or require browser-level cookies that cannot be replicated via REST. The server uses a hybrid approach:

| Strategy | Used By | Why |
|----------|---------|-----|
| **Browser intercept** | `search_jobs`, `get_company_jobs`, `get_job` (fallback) | Search API returns 406 reCAPTCHA on direct REST calls. Browser navigates to the page and intercepts the XHR response. |
| **Direct REST API** | `apply`, `get_profile`, `get_recommendations`, `sync_applications`, most tools | Fast, no browser tab needed. Uses JWT token extracted from browser cookies. |
| **Browser UI automation** | `login`, `boost_visibility`, `update_profile` | Requires clicking buttons, filling forms, handling SSO popups. |
| **AmbitionBox scraping** | `get_company_salary`, `get_company_reviews` | Extracts `__NEXT_DATA__` from server-rendered Next.js pages. |

### PagePool

The server maintains a pool of browser tabs (default 3, configurable via `NAUKRI_MAX_TABS`). Tabs are checked out with a semaphore, auto-recovered if crashed, and returned after use. This allows concurrent operations like batch apply to run in parallel without opening excessive tabs.

### TokenManager

JWT authentication token (`nauk_at` cookie) is extracted from the Playwright browser context and cached in memory. On 401 errors, a single-writer refresh lock prevents parallel refresh storms -- one request refreshes, others wait and reuse the result.

### 3-Tier Sync Fallback

`naukri_sync_applications` tries three strategies in order:

1. **REST API** -- paginated GET to the history endpoint (fastest, most reliable)
2. **Browser intercept** -- navigate to the applied-jobs page and capture the XHR response
3. **HTML scrape** -- extract job cards from the server-rendered DOM using adaptive CSS selectors

Each tier is a fallback for when the previous one fails (API changes, reCAPTCHA blocks, etc.).

---

## Tools (61)

| Category | Count | Tools |
|----------|-------|-------|
| Auth | 3 | `naukri_login`, `naukri_verify_otp`, `naukri_get_login_status` |
| Search | 3 | `naukri_search_jobs`, `naukri_get_recommendations`, `naukri_get_similar_jobs` |
| Jobs | 1 | `naukri_get_job` |
| Apply | 2 | `naukri_apply`, `naukri_batch_apply` |
| Profile | 4 | `naukri_get_profile`, `naukri_update_profile`, `naukri_boost_visibility`, `naukri_get_dashboard` |
| Tracking | 6 | `naukri_get_applications`, `naukri_get_application_status`, `naukri_get_match_analytics`, `naukri_get_saved_jobs`, `naukri_save_job`, `naukri_unsave_job` |
| Sync | 2 | `naukri_sync_applications`, `naukri_sync_saved_jobs` |
| Upload | 1 | `naukri_upload_resume` |
| Resume & Photo | 4 | `naukri_get_resume_info`, `naukri_get_photo_info`, `naukri_upload_photo`, `naukri_delete_photo` |
| Resume Builder | 2 | `naukri_get_resume_templates`, `naukri_get_resume_builder_status` |
| Alerts | 4 | `naukri_get_job_alerts`, `naukri_create_job_alert`, `naukri_get_alert_detail`, `naukri_delete_job_alert` |
| Companies | 4 | `naukri_search_companies`, `naukri_get_company_jobs`, `naukri_follow_company`, `naukri_get_company_follow_status` |
| AmbitionBox | 3 | `naukri_get_company_salary`, `naukri_get_company_reviews`, `naukri_get_company_slug` |
| Settings | 3 | `naukri_get_settings`, `naukri_update_settings`, `naukri_get_blocked_companies` |
| Inbox | 2 | `naukri_get_inbox`, `naukri_read_message` |
| Notifications | 3 | `naukri_get_notifications`, `naukri_mark_notification_read`, `naukri_mark_all_notifications_read` |
| Performance | 3 | `naukri_get_search_impressions`, `naukri_get_recruiter_activity`, `naukri_get_activity_level` |
| Assessments | 2 | `naukri_get_assessments`, `naukri_get_profile_completeness` |
| Subscription | 1 | `naukri_get_subscription_status` |
| Mock Interview | 2 | `naukri_get_mock_interview_topics`, `naukri_get_mock_interview_history` |
| Extras | 3 | `naukri_get_notification_count`, `naukri_report_fraud_job`, `naukri_check_email_verification` |
| Early Access | 1 | `naukri_get_early_access_roles` |
| Debug | 1 | `naukri_debug` (16 actions: snapshot, screenshot, scan, deepscan, explore, notif_explore, fetch_api, post_api, put_api, delete_api, fetch_widget, settings_api, discover, fetch_all_statuses, intercept_requests, click_discover) |
| Health | 1 | `naukri_health_check` |

---

## Configuration

### Environment Variables

All optional. Set in shell or `.env` file.

| Variable | Default | Description |
|----------|---------|-------------|
| `NAUKRI_NAV_TIMEOUT` | `20000` | Playwright page navigation timeout in milliseconds |
| `NAUKRI_ELEMENT_TIMEOUT` | `5000` | Playwright element wait timeout in milliseconds |
| `NAUKRI_API_TIMEOUT` | `30` | aiohttp REST API timeout in seconds |
| `NAUKRI_MAX_TABS` | `3` | Max concurrent browser tabs in the PagePool |

### Data File Locations

All data files live in the project root (`naukri/`) and are gitignored.

| File | Purpose |
|------|---------|
| `chrome-profile/` | Playwright persistent browser profile (cookies, local storage, session). Machine-specific, never commit. |
| `applications.json` | Local application tracking. Array of `{job_id, title, company, status, applied_at, ...}`. Written by `naukri_apply`, `naukri_batch_apply`, and `naukri_sync_applications`. |
| `saved_jobs.json` | Local saved/bookmarked jobs. Array of `{job_id, title, company, saved_at, notes, ...}`. |
| `questions.json` | Screening question answer cache. Maps question text to answer values. Auto-populated when you answer questions during apply. Used by batch apply for auto-answering. |
| `*.backup` | Automatic backup before any JSON file overwrite. Created by the atomic write helper (write to `.tmp`, backup existing, rename). |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **"Not logged in" errors** | Session expired (~30 days). Call `naukri_login` to re-authenticate. |
| **Search returns empty / 406** | This is expected for direct REST calls. `naukri_search_jobs` uses browser intercept and should work. If it fails, check that the browser is running with `naukri_health_check`. |
| **Timeouts on slow connections** | Increase `NAUKRI_NAV_TIMEOUT` (e.g., `30000`) and `NAUKRI_API_TIMEOUT` (e.g., `60`). |
| **Rate limits / daily apply cap** | Naukri limits daily applications (varies by account type). The `daily_applied` field in apply responses shows your count. Naukri 360 subscribers get higher limits. |
| **Browser tab crashes** | The PagePool auto-recovers crashed tabs on the next `acquire()`. If persistent, restart the server. |
| **AmbitionBox scraping broken** | AmbitionBox is a Next.js SSR site. If they change their `__NEXT_DATA__` structure, salary/review tools may return errors. `naukri_health_check` includes an AmbitionBox check -- a "warn" status here is expected periodically. |
| **Token refresh loops** | If the server keeps returning 401 despite `naukri_login`, delete `chrome-profile/` and re-authenticate from scratch. |
| **`naukri_sync_applications` fails all 3 strategies** | Usually means the session is invalid. Log in first. If logged in, try `force_browser=True` to skip the REST tier. |

### Health Check

Run `naukri_health_check()` to validate all integrations at once. It tests:

- Login session validity
- Profile API
- Search API (expects "warn" for reCAPTCHA -- that is normal)
- Recommendations API
- Dashboard API
- Browser page pool liveness
- AmbitionBox scraping

Returns `{summary: {ok: 5, warn: 2, fail: 0}, checks: [...]}` with per-check timing.
