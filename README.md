# Naukri MCP Server

MCP server for automating Naukri.com (India's largest job portal). 57 tools across search, apply, profile management, tracking, and analytics -- hybrid Playwright browser + REST API architecture.

## Setup

**Prerequisites:** Python 3.10+

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

**First run:**

```bash
python naukri.py
```

Then call `naukri_login` to authenticate via Google SSO or email/password. The browser session persists in `chrome-profile/` (auto-created, gitignored).

## Environment Variables

All optional. Set in shell or `.env`.

| Variable | Default | Description |
|----------|---------|-------------|
| `NAUKRI_NAV_TIMEOUT` | `20000` | Page navigation timeout (ms) |
| `NAUKRI_ELEMENT_TIMEOUT` | `5000` | Element wait timeout (ms) |
| `NAUKRI_API_TIMEOUT` | `30` | REST API timeout (seconds) |
| `NAUKRI_MAX_TABS` | `3` | Max concurrent browser tabs |

## Architecture

```
naukri.py                  # Entry point (FastMCP run)
naukri_server/
  __init__.py              # FastMCP setup + lifespan (browser start/stop)
  config.py                # Constants, API endpoints, timeouts
  browser.py               # PagePool + TokenManager (Playwright)
  api.py                   # REST helpers (GET/POST/PUT/DELETE with auth)
  cache.py                 # Answer cache for auto-apply screening questions
  tools/                   # 23 tool modules
    auth.py                # Login, OTP verification
    search.py              # Job search, recommendations, similar jobs
    jobs.py                # Job detail retrieval
    apply.py               # Single apply, batch apply with auto-answer
    profile.py             # Profile CRUD, dashboard, refresh
    tracking.py            # Application tracking, saved jobs, match analytics
    sync.py                # Sync applications/saved jobs from Naukri backend
    upload.py              # Resume upload
    resume_photo.py        # Resume info, photo upload/delete
    resume_builder.py      # Resume templates, builder status
    alerts.py              # Job alert CRUD
    companies.py           # Company search, jobs, follow
    ambitionbox.py         # Salary data, employee reviews (AmbitionBox)
    settings.py            # Account settings, blocked companies
    inbox.py               # Recruiter messages, NVites
    notifications.py       # Notification feed, mark read
    performance.py         # Search impressions, recruiter activity
    assessments.py         # Skill assessments, profile completeness
    subscription.py        # Naukri 360 status, login check
    mock_interview.py      # AI mock interview topics, history
    extras.py              # Notification count, fraud report, email verify
    early_access.py        # Pre-posted roles from top companies
    debug.py               # Multi-action debug tool
```

## Tools (57)

| Category | Count | Tools |
|----------|-------|-------|
| Auth | 2 | `naukri_login`, `naukri_verify_otp` |
| Search | 3 | `naukri_search_jobs`, `naukri_get_recommendations`, `naukri_get_similar_jobs` |
| Jobs | 1 | `naukri_get_job` |
| Apply | 2 | `naukri_apply`, `naukri_batch_apply` |
| Profile | 4 | `naukri_get_profile`, `naukri_update_profile`, `naukri_refresh_profile`, `naukri_get_dashboard` |
| Tracking | 6 | `naukri_get_applications`, `naukri_get_application_status`, `naukri_get_match_analytics`, `naukri_get_saved_jobs`, `naukri_save_job`, `naukri_unsave_job` |
| Sync | 2 | `naukri_sync_applications`, `naukri_sync_saved_jobs` |
| Upload | 1 | `naukri_upload_resume` |
| Resume & Photo | 4 | `naukri_get_resume_info`, `naukri_get_photo_info`, `naukri_upload_photo`, `naukri_delete_photo` |
| Resume Builder | 2 | `naukri_get_resume_templates`, `naukri_get_resume_builder_status` |
| Alerts | 4 | `naukri_get_job_alerts`, `naukri_create_job_alert`, `naukri_get_alert_detail`, `naukri_delete_job_alert` |
| Companies | 3 | `naukri_search_companies`, `naukri_get_company_jobs`, `naukri_follow_company` |
| AmbitionBox | 2 | `naukri_get_company_salary`, `naukri_get_company_reviews` |
| Settings | 3 | `naukri_get_settings`, `naukri_update_settings`, `naukri_get_blocked_companies` |
| Inbox | 2 | `naukri_get_inbox`, `naukri_read_message` |
| Notifications | 2 | `naukri_get_notifications`, `naukri_mark_notification_read` |
| Performance | 3 | `naukri_get_search_impressions`, `naukri_get_recruiter_activity`, `naukri_get_activity_level` |
| Assessments | 2 | `naukri_get_assessments`, `naukri_get_profile_completeness` |
| Subscription | 2 | `naukri_get_subscription_status`, `naukri_get_login_status` |
| Mock Interview | 2 | `naukri_get_mock_interview_topics`, `naukri_get_mock_interview_history` |
| Extras | 3 | `naukri_get_notification_count`, `naukri_report_fraud_job`, `naukri_check_email_verification` |
| Early Access | 1 | `naukri_get_early_access_roles` |
| Debug | 1 | `naukri_debug` |

## MCP Client Config

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
