# Naukri MCP Server

117-tool atomic MCP server for automating [Naukri.com](https://www.naukri.com) (India's largest job portal). Search jobs, apply in bulk, manage your profile, track applications, research companies, and monitor recruiter activity -- all from your MCP client. Designed for Claude Code's progressive Tool Search loading (default since Jan 2026), so each tool is single-purpose and discoverable on demand.

**Tech stack:** Python 3.10+, [FastMCP](https://github.com/jlowin/fastmcp), Playwright (persistent Chromium), aiohttp

**Key capabilities:**

- **Search & Apply** -- keyword search, personalized recommendations, single or batch apply with auto-answered screening questions
- **Application Tracking** -- local JSON persistence + 3-tier sync from Naukri's backend (REST, browser intercept, HTML scrape)
- **Profile Management** -- view/edit profile (`naukri_get_profile`, `naukri_update_profile`), boost visibility (`naukri_boost_profile`)
- **Company Research** -- `naukri_research_company` plus AmbitionBox bridge for salary data and employee reviews
- **Performance Analytics** -- `naukri_search_impressions`, `naukri_recruiter_activity`, `naukri_activity_level`
- **Smart Automation** -- `naukri_auto_hunt` (one-call job hunting with fit scoring), `naukri_daily_brief` (morning dashboard), `naukri_tailor_resume`, `naukri_apply_top_fits` (auto-apply to best matches)

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
| **Direct REST API** | `naukri_apply()`, `naukri_get_profile()`, `naukri_get_recommendations`, `naukri_sync`, most reads | Fast, no browser tab needed. Uses JWT token extracted from browser cookies. |
| **Browser intercept** | `naukri_search_jobs`, `naukri_company_jobs()`, `naukri_jobs` (fallback) | Search API returns 406 on direct REST. Browser navigates to the page and intercepts the XHR response. |
| **Browser UI automation** | `naukri_login(method="google")`, `naukri_boost_profile()`, `naukri_update_profile()`, `naukri_update_alert()`, `naukri_delete_alert()` | Requires clicking buttons, filling forms, handling SSO popups. Akamai blocks PUT/DELETE via REST. |
| **AmbitionBox scraping** | `naukri_company_intel` (salary, reviews, interviews) | Extracts `__NEXT_DATA__` from server-rendered Next.js pages. |

### PagePool

The server maintains a pool of 3 browser tabs (configurable via `NAUKRI_MAX_TABS`). Tabs are checked out with a semaphore, auto-recovered if crashed, and returned after use. This allows concurrent operations like batch apply to run in parallel without opening excessive tabs.

### TokenManager

JWT authentication token (`nauk_at` cookie) is extracted from the Playwright browser context and cached in memory. On 401 errors, a single-writer refresh lock prevents parallel refresh storms -- one request refreshes, others wait and reuse the result.

### 3-Tier Sync Fallback

`naukri_sync_applications()` tries three strategies in order:

1. **REST API** -- paginated GET to the history endpoint (fastest, most reliable)
2. **Browser intercept** -- navigate to the applied-jobs page and capture the XHR response
3. **HTML scrape** -- extract job cards from the server-rendered DOM using adaptive CSS selectors

---

## Quick Start for AI Consumers

```
1.  naukri_auth_status()             # Check session
    naukri_login(method="google")              # Authenticate (Google SSO or email)
2.  naukri_daily_brief()                     # Morning dashboard: recommendations + analytics
3.  naukri_auto_hunt(keywords="...", location="...")  # One-call job hunt with fit scoring
4.  naukri_assess_fit(job_id=...)           # Pre-flight check before applying
    naukri_apply(job_id=...)   # Submit application
5.  naukri_compare_jobs(job_ids=[id1, id2, id3])  # Side-by-side with fit scores
6.  naukri_accept_nvite(nvite_job_id="...")  # Respond to recruiter NVites
7.  naukri_sync_applications()       # Pull latest from Naukri backend
    naukri_list_applications()       # Query local tracking
8.  naukri_research_company(keyword="...")  # Unified: Naukri + AmbitionBox data
    naukri_company_intel(company="slug", intel_type="interviews")  # Interview tips
9.  naukri_tailor_resume(job_id=...)  # Get tailoring suggestions
    naukri_update_profile(...)     # Apply them
10. naukri_download_resume(save_path="...")  # Download resume
```

**Apply flow detail:** If a job has screening questions, the first `naukri_apply()` call returns them. Pass answers back in the second call. Answer keys are fuzzy-matched -- `"current ctc"` matches `"What is your current CTC?"`. Answers are cached in `questions.json` so you only answer each question type once.

---

## Tools (117 atomic)

Almost every tool follows the **single-purpose atomic pattern** — one MCP tool per operation.
Only `naukri_company_intel` and `naukri_debug` keep an `action`/`intel_type` parameter (see the
"Dispatcher tools" subsection below for why). This catalog is designed for Claude Code's
progressive Tool Search loading (default since Jan 2026), so a large number of focused tools
costs no more than a few multi-purpose ones.

### Auth
- `naukri_login(method=...)` — Google SSO or email/password
- `naukri_verify_otp(otp)` — Submit OTP after login
- `naukri_auth_status()` — Check session validity

### Job Search & Discovery
- `naukri_search_jobs` — Keyword search with browser intercept
- `naukri_get_recommendations` — Personalized job recommendations
- `naukri_get_job(job_id)` — Full job details
- `naukri_similar_jobs(job_id)` — Find similar jobs
- `naukri_compare_jobs(job_ids)` — Side-by-side with fit scores
- `naukri_bulk_fetch_jobs(job_ids)` — Up to 20 jobs in one call
- `naukri_job_detail_v1(job_id)` — Walk-in info, contact details
- `naukri_report_fraud(job_id, reason)` — Report fraudulent listing
- `naukri_auto_hunt` — One-call automated job hunting with fit scoring

### Apply & Track
- `naukri_apply(job_id, set_reminder_days=...)` — Single apply with auto-reminder
- `naukri_batch_apply(keywords=...)` — Bulk apply from search
- `naukri_assess_fit(job_id, apply_if_fit=False)` — Fit assessment (auto-apply optional)
- `naukri_score_saved_jobs(min_fit_score=60)` — Score all saved jobs
- `naukri_apply_top_fits(min_fit_score=70, limit=10)` — Score + auto-apply top matches
- `naukri_list_applications(...)` — Query local tracking
- `naukri_get_application(job_id)` — Detailed application status
- `naukri_purge_applications(before_date)` — Delete old records
- `naukri_stale_applications(...)` — Detect stale applications
- `naukri_follow_up_priority(...)` — Cross-reference inbox + reminders
- `naukri_draft_follow_up(job_id)` — Generate follow-up message
- `naukri_recruiter_history()` — Per-company communication history

### Sync & Export
- `naukri_sync_applications(force_browser=False, days_back=365)` — 3-tier sync
- `naukri_sync_saved(force_browser=False)` — Sync saved jobs
- `naukri_export_data(data_type, export_format="json")` — Export to JSON/CSV

### Saved Jobs
- `naukri_list_saved_jobs(limit=50, page=1)` — List saved/bookmarked jobs
- `naukri_save_job(job_id, ...)` — Save a job
- `naukri_unsave_job(job_id)` — Remove a saved job
- `naukri_sync_saved_jobs()` — Pull from Naukri server

### Inbox (recruiter messages)
- `naukri_list_inbox(limit=20, unread_only=False)` — List messages
- `naukri_read_message(message_id, vcard_id, unique_id)` — Read full message
- `naukri_mark_interested(mail_id, conversation_id, interested=True)` — Signal interest
- `naukri_accept_nvite(nvite_job_id, ...)` — Apply via NVite

### Notifications
- `naukri_list_notifications(limit=20, page=1, notif_type=None)` — Filtered list
- `naukri_notification_count()` — Unread count
- `naukri_mark_notification_read(notification_id, date)` — Mark single
- `naukri_mark_all_notifications_read()` — Mark all
- `naukri_notification_summary()` — Unified dashboard

### Profile
- `naukri_get_profile()` — Full profile
- `naukri_update_profile(fields, ...)` — Update profile fields
- `naukri_audit_profile()` — Completeness + tips
- `naukri_boost_profile(randomize=False)` — Re-save headline for visibility
- `naukri_dashboard()` — Profile dashboard data
- `naukri_profile_targeting()` — DFP targeting view

### Resume & Photo
- `naukri_resume_info()` — Resume metadata
- `naukri_upload_resume(file_path)` — Upload PDF/DOC/DOCX
- `naukri_download_resume(save_path)` — Download to local file
- `naukri_photo_info()` — Photo metadata
- `naukri_upload_photo(file_path)` — Upload PNG/JPG/JPEG/GIF
- `naukri_delete_photo()` — Remove profile photo

### Insights & Analytics
- `naukri_application_insights(days=30)` — Status breakdown + velocity
- `naukri_salary_position(designation=...)` — Salary positioning
- `naukri_cached_answers(action="list|update|delete", key=..., new_answer=...)` — Manage cached answers
- `naukri_match_analytics(days=30)` — Match-score per-field breakdowns
- `naukri_match_quality(days=30)` — Aggregate match quality
- `naukri_skill_gap(...)` — Skill gap vs market demand
- `naukri_salary_benchmark(keywords, ...)` — Market salary benchmark
- `naukri_taxonomy()` — Naukri's role taxonomy (37 dept × 167 categories × 1461 roles)
- `naukri_profile_prompts()` — Pending profile-completion actions
- `naukri_conversion_funnel(days=30)` — Application-to-interview funnel
- `naukri_status_changes(days=30)` — Detect status transitions

### Performance
- `naukri_search_impressions(days=7)` — Search appearance stats
- `naukri_recruiter_activity(page=1, limit=100, filter_by=None)` — Recruiter actions on profile
- `naukri_activity_level()` — Current profile activity level

### Companies
- `naukri_search_companies(keyword, page=1, limit=10)` — Find companies
- `naukri_company_jobs(group_id, ...)` — Jobs at a company
- `naukri_company_slug(group_id)` — AmbitionBox slug (single or batch comma-separated)
- `naukri_research_company(keyword, ...)` — Naukri + AmbitionBox combined
- `naukri_follow_company(group_id|group_ids, action="follow|unfollow")` — Follow/unfollow
- `naukri_follow_status(group_id|group_ids)` — Check follow status
- `naukri_company_intel(company, intel_type="salary|reviews|interviews")` — AmbitionBox intel

### Settings
- `naukri_get_settings()` — All current account settings (job-search status, notifications, consent flags)
- `naukri_update_settings(...)` — Modify settings (pass only fields to change)
- `naukri_blocked_companies()` — List blocked companies
- `naukri_check_email()` — Email/mobile verification status
- `naukri_visibility()` — Resdex visibility toggles
- `naukri_notification_prefs()` — Email/SMS/push/WhatsApp preferences
- `naukri_subscription_status()` — Naukri 360 subscription + features

### Job Alerts
- `naukri_list_alerts()` — All your saved-search job alerts
- `naukri_alert_detail(alert_id)` — Single alert details
- `naukri_create_alert(name, keywords, ...)` — Create new alert
- `naukri_update_alert(alert_id, ...)` — Edit alert fields
- `naukri_delete_alert(alert_id)` — Delete an alert

### Early Access (pre-posted roles)
- `naukri_list_early_access(...)` — Browse pre-posted roles from top companies
- `naukri_share_early_access(job_id)` — Express interest (instant, no screening)

### Resume Builder
- `naukri_resume_templates()` — Available templates (free + pro)
- `naukri_resume_builder_status()` — AI rewrite attempts left, subscription tier
- `naukri_tailor_resume(job_id, ...)` — Tailoring suggestions for a specific job

### Mock Interview (AI)
- `naukri_mock_interview_topics()` — Available topics + completion status
- `naukri_mock_interview_history()` — Past interviews with scores/feedback
- `naukri_start_mock_interview(job_id)` — Start a JD-based mock interview
- `naukri_answer_mock_interview(test_id, topic_id, question_id, answer)` — Submit answer
- `naukri_mock_interview_prep(job_id)` — Interview prep bundle

### Autonomous Agent
- `naukri_agent_status()` — Agent state + last 5 runs + config summary
- `naukri_agent_config()` — Full configuration
- `naukri_agent_update_config(updates)` — Patch config with JSON
- `naukri_agent_run_now(ctx=None)` — Execute one observe→decide→act→learn cycle
- `naukri_agent_approve(cycle_id)` — Apply pending decisions
- `naukri_agent_reject(cycle_id)` — Reject pending decisions
- `naukri_agent_history(limit=10)` — Recent run history
- `naukri_agent_decisions(cycle_id)` — Per-job decisions for one cycle

### Background Scheduler
- `naukri_scheduler_status()` — Scheduler state + per-task last-run info
- `naukri_enable_task(task_name)` — Enable a disabled task
- `naukri_disable_task(task_name)` — Disable a task
- `naukri_run_task_now(task_name)` — Execute a task immediately
- `naukri_task_history(task_name=None, limit=20)` — Recent run history

### Reminders & Interviews
- `naukri_list_reminders(include_past=True, include_app_status=True)` — All reminders with due status
- `naukri_set_reminder(job_id, days=7, ...)` — Create/update reminder
- `naukri_interview_prep(job_id)` — Interview prep package
- `naukri_add_interview_round(job_id, round_type, ...)` — Track interview round
- `naukri_list_interview_rounds(job_id=None)` — List rounds
- `naukri_compare_offers(job_ids)` — Compare multiple job offers

### Dispatcher tools (only 2 left — kept by design)
- `naukri_company_intel(company, intel_type="salary|reviews|interviews")` — Three actions
  share the same `company` resolution + AmbitionBox auth flow; splitting would duplicate
  that orchestration.
- `naukri_debug(action=...)` — 16 dev-only debug actions across browser/API/discovery;
  catalog cost is real here even with progressive loading since most users never invoke them.

### Other
- `naukri_daily_brief` — Morning dashboard: 16 sources + recommended actions
- `naukri_health_check` — Endpoint validation + browser pool + AmbitionBox

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

Then call `naukri_login(method="google")` from your MCP client. It opens a visible Chromium window where you can:

1. **Google SSO (recommended):** Click "Login with Google" -- uses the Chrome profile's saved Google session, no credentials needed.
2. **Email/password:** Pass `method="email"`, `email="..."`, `password="..."`.

The browser session is stored in `chrome-profile/` (auto-created, gitignored). This directory is machine-specific -- it contains cookies, local storage, and cached credentials. Do not copy it between machines.

### Session Lifetime

Sessions persist for approximately 30 days. When expired, the server detects it at startup or on the first API call and returns a `"Not logged in"` error. Re-authenticate with `naukri_login(method="google")`.

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
- **Profile mutations** (`naukri_update_profile()`) -- PUT/DELETE blocked by Akamai; browser automation used instead
- **Job alerts** -- CRUD operations go through browser UI automation for the same reason

This is expected behavior. Tools that require browser interaction are documented as such. If you see 406 errors from tools that should use REST, check login status with `naukri_auth_status()` -- an expired token causes Akamai to classify requests as bot traffic.

### AmbitionBox Scraping

AmbitionBox is a Next.js SSR site. Salary and review tools extract `__NEXT_DATA__` from server-rendered pages. If AmbitionBox changes their page structure, these tools may return errors. `naukri_health_check` includes an AmbitionBox check -- a "warn" status there is expected periodically and not a blocker for core Naukri functionality.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **"Not logged in" errors** | Session expired (~30 days). Call `naukri_login(method="google")` to re-authenticate. |
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

---

## Remote Access

Run the server on your always-on machine and connect from anywhere (web Claude in cowork environments, mobile, etc.). Two auth modes are supported and can run side-by-side on the same server.

### Quick decision

| Client | Auth mode | Why |
|--------|-----------|-----|
| Claude Code CLI | Bearer (`MCP_SHARED_SECRET`) | `claude mcp add --transport http ... --header "Authorization: Bearer ..."` works directly |
| Claude Desktop | Bearer (`MCP_SHARED_SECRET`) | Supports `headers` config in `claude_desktop_config.json` |
| Claude.ai web | OAuth (`MCP_OAUTH_ENABLED=1`) | Web UI only exposes OAuth client_id/secret fields, not bearer |
| Both at once | Bearer + OAuth (set both env vars) | Single server, OAuth provider's `load_access_token` falls back to the shared secret |

### Step 1 — Generate secrets

```powershell
# Bearer secret (for Claude Code / Desktop)
python -c "import secrets; print(secrets.token_urlsafe(48))"

# OAuth client_id + client_secret (for Claude.ai web)
python -c "import secrets; print('client_id=claude-ai-web')"
python -c "import secrets; print('client_secret=' + secrets.token_urlsafe(48))"
```

### Step 2 — Configure `.env`

Copy `.env.example` to `.env` and fill in. The `.env` file is gitignored. Minimal config to enable BOTH auth modes:

```
MCP_REMOTE=1
MCP_PORT=8321
MCP_PUBLIC_URL=https://naukri.<your-domain>

# Bearer (Claude Code + Desktop)
MCP_SHARED_SECRET=<paste output from token_urlsafe(48)>

# OAuth (claude.ai web)
MCP_OAUTH_ENABLED=1
MCP_OAUTH_CLIENT_ID=claude-ai-web
MCP_OAUTH_CLIENT_SECRET=<paste output from token_urlsafe(48)>
MCP_OAUTH_AUTO_APPROVE=1
```

If `MCP_REMOTE=1` but no auth env var is set, the server **refuses to start** — this is the safety check that prevents accidentally exposing an unauthenticated MCP to the internet.

### Step 3 — Public hostname (Cloudflare Tunnel recommended)

Cloudflare Tunnel gives you a stable public HTTPS URL without opening firewall ports. Free tier, unmetered bandwidth.

```powershell
winget install Cloudflare.cloudflared
cloudflared tunnel login
cloudflared tunnel create naukri-mcp
cloudflared tunnel route dns naukri-mcp naukri.<your-domain>
```

Edit `%USERPROFILE%\.cloudflared\config.yml`:

```yaml
tunnel: <UUID-from-create-command>
credentials-file: C:\Users\<you>\.cloudflared\<UUID>.json
ingress:
  - hostname: naukri.<your-domain>
    service: http://localhost:8321
  - service: http_status:404
```

Run the tunnel: `cloudflared tunnel run naukri-mcp` (or `cloudflared service install` for autostart).

Alternatives: Tailscale Funnel (peer-to-peer, lower latency for trusted devices) or ngrok (simpler but free tier has limits).

### Step 4 — Start the server

```powershell
# Load env vars from .env (PowerShell — use a one-liner or a helper script)
Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=.+' } | ForEach-Object {
    $name, $val = $_ -split '=', 2
    [Environment]::SetEnvironmentVariable($name, $val, "Process")
}

python naukri.py --http
```

Logs should show `Auth: OAuth provider enabled (issuer=https://naukri.<your-domain>, bearer-fallback=yes)` and `HTTP mode: 0.0.0.0:8321`.

### Step 5 — Connect clients

**Claude Code CLI (uses bearer):**

```powershell
claude mcp add --transport http naukri https://naukri.<your-domain>/mcp `
  --header "Authorization: Bearer <MCP_SHARED_SECRET>"
```

**Claude Desktop (uses bearer):**

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "naukri": {
      "url": "https://naukri.<your-domain>/mcp",
      "transport": "http",
      "headers": { "Authorization": "Bearer <MCP_SHARED_SECRET>" }
    }
  }
}
```

**Claude.ai web (uses OAuth):**

Settings → Connectors → Add custom connector
- URL: `https://naukri.<your-domain>/mcp`
- OAuth Client ID: `claude-ai-web` (matching `MCP_OAUTH_CLIENT_ID`)
- OAuth Client Secret: paste `MCP_OAUTH_CLIENT_SECRET`

Claude.ai will discover OAuth metadata automatically (FastMCP serves `.well-known/oauth-authorization-server` and the `/authorize` + `/token` endpoints).

### Smoke test (curl)

```powershell
# 401 expected — no auth header
curl -i https://naukri.<your-domain>/mcp

# Bearer flow — should return MCP JSON-RPC instead of 401
curl -i -H "Authorization: Bearer <MCP_SHARED_SECRET>" `
  https://naukri.<your-domain>/mcp

# OAuth metadata discovery
curl https://naukri.<your-domain>/.well-known/oauth-authorization-server | jq .
```

### Windows host hardening

The MCP needs a headed Chrome session, so the host machine must stay awake and logged in.

```powershell
# Disable sleep / hibernate while plugged in
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
# Disable screen-off (optional — Chrome stays alive when display sleeps,
# but this avoids GPU pauses)
powercfg /change monitor-timeout-ac 0
```

| Behavior | Result |
|----------|--------|
| Lock screen | Chrome stays alive, MCP works |
| Logout | Chrome dies, MCP fails — keep the user session active |
| RDP disconnect | Process keeps running on host, MCP works |
| System sleep | Chrome resumes but in-flight calls fail — disable sleep |
| Manual Chrome use | Chrome on Windows can't run two instances with different `--user-data-dir`. Don't open the same profile manually while the MCP is running. |

### Monitoring

Cloudflare's "tunnel healthy" status only reflects the edge↔cloudflared link, not the origin. Add an external uptime probe (e.g., UptimeRobot, free) hitting `https://naukri.<your-domain>/.well-known/oauth-authorization-server` (200 expected) so you get notified when the host machine is actually unreachable.

### Auth mode reference

| Env var | Required for | Notes |
|---------|--------------|-------|
| `MCP_REMOTE=1` | Public bind | Without this, server stays on 127.0.0.1 |
| `MCP_PORT` | Custom port | Default 8321 |
| `MCP_PUBLIC_URL` | OAuth issuer / RS metadata | Defaults to `http://localhost:8321` |
| `MCP_SHARED_SECRET` | Bearer auth | >=32 chars; rotate by changing env + restart |
| `MCP_OAUTH_ENABLED=1` | OAuth flow | Enables `/authorize`, `/token`, `/register`, `/revoke` |
| `MCP_OAUTH_CLIENT_ID` | OAuth | Pre-registered client id for claude.ai |
| `MCP_OAUTH_CLIENT_SECRET` | OAuth | >=32 chars |
| `MCP_OAUTH_AUTO_APPROVE` | OAuth UX | `1` skips consent screen (default), `0` shows Approve/Deny page at `/oauth/consent` |
