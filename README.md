# Naukri MCP Server

26-tool MCP server for automating [Naukri.com](https://www.naukri.com) (India's largest job portal). Search jobs, apply in bulk, manage your profile, track applications, research companies, and monitor recruiter activity -- all from your MCP client.

**Tech stack:** Python 3.10+, [FastMCP](https://github.com/jlowin/fastmcp), Playwright (persistent Chromium), aiohttp

**Key capabilities:**

- **Search & Apply** -- keyword search, personalized recommendations, single or batch apply with auto-answered screening questions
- **Application Tracking** -- local JSON persistence + 3-tier sync from Naukri's backend (REST, browser intercept, HTML scrape)
- **Profile Management** -- view/edit profile, boost visibility to appear "recently active" to recruiters
- **Company Research** -- unified `naukri_company(action="research")` plus AmbitionBox bridge for salary data and employee reviews
- **Performance Analytics** -- search impressions, recruiter activity (who viewed/downloaded/contacted you), match scores
- **Smart Automation** -- `naukri_auto_hunt` (one-call job hunting with fit scoring), `naukri_daily_brief` (morning dashboard), `naukri_resume_builder(action="tailor")`

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
  tools/                     # 24 tool modules (26 tools)
    auth.py                  # Login, OTP verification, login status
    search.py                # Job search, recommendations
    jobs.py                  # Job detail, similar, compare, bulk, report fraud
    apply.py                 # Applications: list, detail, apply, batch, purge, stale, follow-up
    tracking.py              # Saved jobs: list, save, unsave, sync
    smart_apply.py           # Smart apply with fit scoring
    auto_hunt.py             # One-call automated job hunting
    profile.py               # Profile CRUD, dashboard, boost, audit
    resume_photo.py          # Resume/photo info, upload, download, delete
    resume_builder.py        # Resume templates, builder status, tailor
    sync.py                  # Sync applications/saved jobs, export
    insights.py              # Application insights, salary, match analytics, skill gap, taxonomy
    performance.py           # Search impressions, recruiter activity
    companies.py             # Company search, jobs, slug, research, follow/unfollow
    ambitionbox.py           # Salary data, reviews, interviews (AmbitionBox)
    inbox.py                 # Recruiter messages, NVites, mark_interested
    notifications.py         # Notification feed, mark read, count, summary
    settings.py              # Account settings, blocked companies, email, visibility, subscription
    alerts.py                # Job alert CRUD
    early_access.py          # Pre-posted roles from top companies
    mock_interview.py        # AI mock interview topics, sessions, history
    reminders.py             # Follow-up reminders
    daily_brief.py           # Morning dashboard summary
    health.py                # Endpoint validation, browser pool, AmbitionBox checks
    debug/                   # Multi-action debug tool (16 actions)
```

### Hybrid Browser + REST Strategy

Naukri's Akamai CDN blocks direct REST calls for several endpoints. The server uses a hybrid approach:

| Strategy | Used By | Why |
|----------|---------|-----|
| **Direct REST API** | `naukri_applications(action="apply")`, `naukri_profile(action="get")`, `naukri_get_recommendations`, `naukri_sync`, most reads | Fast, no browser tab needed. Uses JWT token extracted from browser cookies. |
| **Browser intercept** | `naukri_search_jobs`, `naukri_company(action="jobs")`, `naukri_jobs` (fallback) | Search API returns 406 on direct REST. Browser navigates to the page and intercepts the XHR response. |
| **Browser UI automation** | `naukri_auth(action="login")`, `naukri_profile(action="boost")`, `naukri_profile(action="update")`, `naukri_job_alerts` | Requires clicking buttons, filling forms, handling SSO popups. Akamai blocks PUT/DELETE via REST. |
| **AmbitionBox scraping** | `naukri_company_intel` (salary, reviews, interviews) | Extracts `__NEXT_DATA__` from server-rendered Next.js pages. |

### PagePool

The server maintains a pool of 3 browser tabs (configurable via `NAUKRI_MAX_TABS`). Tabs are checked out with a semaphore, auto-recovered if crashed, and returned after use. This allows concurrent operations like batch apply to run in parallel without opening excessive tabs.

### TokenManager

JWT authentication token (`nauk_at` cookie) is extracted from the Playwright browser context and cached in memory. On 401 errors, a single-writer refresh lock prevents parallel refresh storms -- one request refreshes, others wait and reuse the result.

### 3-Tier Sync Fallback

`naukri_sync(entity="applications")` tries three strategies in order:

1. **REST API** -- paginated GET to the history endpoint (fastest, most reliable)
2. **Browser intercept** -- navigate to the applied-jobs page and capture the XHR response
3. **HTML scrape** -- extract job cards from the server-rendered DOM using adaptive CSS selectors

---

## Quick Start for AI Consumers

```
1.  naukri_auth(action="status")             # Check session
    naukri_auth(action="login")              # Authenticate (Google SSO or email)
2.  naukri_daily_brief()                     # Morning dashboard: recommendations + analytics
3.  naukri_auto_hunt(keywords="...", location="...")  # One-call job hunt with fit scoring
4.  naukri_smart_apply(job_id=...)           # Pre-flight check before applying
    naukri_applications(action="apply", job_id=...)   # Submit application
5.  naukri_jobs(action="compare", job_ids=[id1, id2, id3])  # Side-by-side with fit scores
6.  naukri_inbox(action="accept_nvite", nvite_id="...")  # Respond to recruiter NVites
7.  naukri_sync(entity="applications")       # Pull latest from Naukri backend
    naukri_applications(action="list")       # Query local tracking
8.  naukri_company(action="research", company_name="...")  # Unified: Naukri + AmbitionBox data
    naukri_company_intel(company="slug", intel_type="interviews")  # Interview tips
9.  naukri_resume_builder(action="tailor", job_id=...)  # Get tailoring suggestions
    naukri_profile(action="update", ...)     # Apply them
10. naukri_profile_media(media_type="resume", action="download")  # Download resume
```

**Apply flow detail:** If a job has screening questions, the first `naukri_applications(action="apply")` call returns them. Pass answers back in the second call. Answer keys are fuzzy-matched -- `"current ctc"` matches `"What is your current CTC?"`. Answers are cached in `questions.json` so you only answer each question type once.

---

## Tools (26)

All tools use the **action-parameter pattern** -- a single MCP tool with an `action` (or `intel_type` / `media_type`) parameter that dispatches to sub-operations. This keeps the tool count at 26 while exposing full functionality.

| # | Tool | Dispatch Parameter | Actions / Notes |
|---|------|--------------------|-----------------|
| 1 | `naukri_auth` | `action` | `login` \| `verify_otp` \| `status` |
| 2 | `naukri_search_jobs` | -- | Keyword search with browser intercept (single purpose) |
| 3 | `naukri_get_recommendations` | -- | Personalized job recommendations (single purpose) |
| 4 | `naukri_jobs` | `action` | `get` \| `similar` \| `compare` \| `report_fraud` \| `detail_v1` \| `bulk` |
| 5 | `naukri_applications` | `action` | `list` \| `detail` \| `purge` \| `stale` \| `follow_up` \| `apply` \| `batch_apply` |
| 6 | `naukri_saved_jobs` | `action` | `list` \| `save` \| `unsave` \| `sync` |
| 7 | `naukri_smart_apply` | `action` | *(default)* \| `bulk_saved` \| `apply_top_fits` |
| 8 | `naukri_auto_hunt` | -- | One-call automated job hunting with fit scoring (single purpose) |
| 9 | `naukri_company` | `action` | `search` \| `jobs` \| `slug` \| `batch_slugs` \| `research` \| `follow_status` \| `follow` \| `unfollow` |
| 10 | `naukri_company_intel` | `intel_type` | `salary` \| `reviews` \| `interviews` |
| 11 | `naukri_sync` | `entity` | `applications` \| `saved_jobs` \| `export` |
| 12 | `naukri_insights` | `insight_type` | `applications` \| `salary` \| `cached_answers` \| `match_analytics` \| `match_quality` \| `skill_gap` \| `salary_benchmark` \| `taxonomy` |
| 13 | `naukri_performance` | `metric` | `impressions` \| `recruiter_activity` \| `activity_level` |
| 14 | `naukri_inbox` | `action` | `list` \| `read` \| `mark_interested` \| `accept_nvite` |
| 15 | `naukri_notifications` | `action` | `list` \| `count` \| `mark_read` \| `mark_all_read` \| `summary` |
| 16 | `naukri_job_alerts` | `action` | `list` \| `detail` \| `create` \| `update` \| `delete` |
| 17 | `naukri_reminders` | `action` | `list` \| `set` |
| 18 | `naukri_settings` | `action` | `get` \| `update` \| `blocked_companies` \| `check_email` \| `visibility` \| `notification_prefs` \| `subscription` |
| 19 | `naukri_early_access` | `action` | `list` \| `share` |
| 20 | `naukri_mock_interview` | `action` | `topics` \| `history` \| `start` \| `answer` \| `prep` |
| 21 | `naukri_resume_builder` | `action` | `templates` \| `status` \| `tailor` |
| 22 | `naukri_profile` | `action` | `get` \| `update` \| `audit` \| `boost` \| `dashboard` |
| 23 | `naukri_profile_media` | `media_type` + `action` | `resume` / `photo` x `info` / `upload` / `download` / `delete` |
| 24 | `naukri_daily_brief` | -- | Morning dashboard: inbox, notifications, recs, activity (single purpose) |
| 25 | `naukri_health_check` | -- | Endpoint validation + browser pool + AmbitionBox checks (single purpose) |
| 26 | `naukri_debug` | `action` | 16 actions across browser (`snapshot` \| `screenshot` \| `scan` \| `deepscan` \| `explore` \| `notif_explore`), API (`fetch_api` \| `post_api` \| `put_api` \| `delete_api` \| `fetch_widget` \| `settings_api`), discovery (`discover` \| `fetch_all_statuses` \| `intercept_requests` \| `click_discover`) |

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

Then call `naukri_auth(action="login")` from your MCP client. It opens a visible Chromium window where you can:

1. **Google SSO (recommended):** Click "Login with Google" -- uses the Chrome profile's saved Google session, no credentials needed.
2. **Email/password:** Pass `method="email"`, `email="..."`, `password="..."`.

The browser session is stored in `chrome-profile/` (auto-created, gitignored). This directory is machine-specific -- it contains cookies, local storage, and cached credentials. Do not copy it between machines.

### Session Lifetime

Sessions persist for approximately 30 days. When expired, the server detects it at startup or on the first API call and returns a `"Not logged in"` error. Re-authenticate with `naukri_auth(action="login")`.

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
| `applications.json` | Local application tracking. Written by `apply`, `batch_apply`, and `sync`. |
| `saved_jobs.json` | Local saved/bookmarked jobs. Written by `saved_jobs` and `sync`. |
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

- **Search** (`naukri_search_jobs`) -- always uses browser intercept; direct REST is blocked
- **Profile mutations** (`naukri_profile(action="update")`) -- PUT/DELETE blocked by Akamai; browser automation used instead
- **Job alerts** -- CRUD operations go through browser UI automation for the same reason

This is expected behavior. Tools that require browser interaction are documented as such. If you see 406 errors from tools that should use REST, check login status with `naukri_auth(action="status")` -- an expired token causes Akamai to classify requests as bot traffic.

### AmbitionBox Scraping

AmbitionBox is a Next.js SSR site. Salary and review tools extract `__NEXT_DATA__` from server-rendered pages. If AmbitionBox changes their page structure, these tools may return errors. `naukri_health_check` includes an AmbitionBox check -- a "warn" status there is expected periodically and not a blocker for core Naukri functionality.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **"Not logged in" errors** | Session expired (~30 days). Call `naukri_auth(action="login")` to re-authenticate. |
| **Search returns empty / 406** | Expected for direct REST. `naukri_search_jobs` uses browser intercept and should work. If it fails, run `naukri_health_check`. |
| **Timeouts on slow connections** | Increase `NAUKRI_NAV_TIMEOUT` (e.g., `30000`) and `NAUKRI_API_TIMEOUT` (e.g., `60`). |
| **Rate limits / daily apply cap** | Naukri limits daily applications by account type. The `daily_applied` field in apply responses shows your count. Naukri 360 subscribers get higher limits. |
| **Browser tab crashes** | The PagePool auto-recovers crashed tabs on the next `acquire()`. If persistent, restart the server. |
| **Token refresh loops** | Delete `chrome-profile/` and re-authenticate from scratch. |
| **`naukri_sync` fails all 3 tiers** | Usually means an invalid session. Log in first. If already logged in, pass `force_browser=True` to skip the REST tier. |
| **AmbitionBox salary/reviews broken** | Run `naukri_health_check` to confirm. If AmbitionBox returns "warn", core Naukri tools are unaffected. |

### Health Check

Run `naukri_health_check()` to validate all integrations at once. It tests login session, profile API, search API (406 is normal here), recommendations, dashboard, browser pool liveness, and AmbitionBox scraping.

Returns `{summary: {ok: N, warn: N, fail: N}, checks: [...]}` with per-check timing.
