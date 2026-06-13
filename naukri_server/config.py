"""
Configuration constants and logging setup for Naukri MCP server.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional


class StructuredFormatter(logging.Formatter):
    """JSON-formatted log lines for structured observability.

    Emits one JSON object per log line with timestamp, level, module, message,
    and optional request_id for trace correlation.
    """

    def format(self, record):
        log_data = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }
        if hasattr(record, "request_id"):
            log_data["request_id"] = record.request_id
        if hasattr(record, "cycle_id"):
            log_data["cycle_id"] = record.cycle_id
        if hasattr(record, "step"):
            log_data["step"] = record.step
        if hasattr(record, "extras") and isinstance(record.extras, dict):
            log_data.update(record.extras)
        if record.exc_info and record.exc_info[1]:
            log_data["exception"] = str(record.exc_info[1])
        return json.dumps(log_data)


_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(StructuredFormatter())

logging.basicConfig(
    level=logging.INFO,
    handlers=[_handler],
)
logger = logging.getLogger("naukri")

# Paths — go up from naukri_server/ to naukri/ where chrome-profile/ and data files live
_PACKAGE_ROOT = Path(__file__).parent.parent
CHROME_PROFILE = str(_PACKAGE_ROOT / "chrome-profile")

# Configurable data directory — override via NAUKRI_DATA_DIR env var
DATA_DIR = Path(os.environ.get("NAUKRI_DATA_DIR", str(_PACKAGE_ROOT)))
APPLICATIONS_FILE = DATA_DIR / "applications.json"
SAVED_JOBS_FILE = DATA_DIR / "saved_jobs.json"
REMINDERS_FILE = DATA_DIR / "reminders.json"
QUESTIONS_FILE = DATA_DIR / "questions.json"
SYNC_STATE_FILE = DATA_DIR / "sync_state.json"
EARLY_ACCESS_TRACKING_FILE = DATA_DIR / "early_access_tracking.json"
INTERVIEW_ROUNDS_FILE = DATA_DIR / "interview_rounds.json"
EXPORTS_DIR = DATA_DIR / "exports"

# Backward-compatible alias
CACHE_FILE = QUESTIONS_FILE

# Auto-purge threshold
AUTO_PURGE_DAYS = 180  # Auto-purge applications older than this many days during sync
NAUKRI_BASE = "https://www.naukri.com"

# Timeouts (ms for Playwright, seconds for aiohttp)
NAV_TIMEOUT = int(os.environ.get("NAUKRI_NAV_TIMEOUT", "20000"))
ELEMENT_TIMEOUT = int(os.environ.get("NAUKRI_ELEMENT_TIMEOUT", "5000"))
API_TIMEOUT = int(os.environ.get("NAUKRI_API_TIMEOUT", "30"))
MAX_TABS = int(os.environ.get("NAUKRI_MAX_TABS", "3"))
CDP_PORT = int(os.environ.get("NAUKRI_CDP_PORT", "9223"))

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

# Sync pages & API endpoints (discovered via naukri_debug action="discover")
APPLIED_JOBS_PAGE = "https://www.naukri.com/myapply/historypage"
SAVED_JOBS_PAGE = "https://www.naukri.com/mnjuser/savedjobs"
APPLIED_JOBS_API = "/cloudgateway-apply/whtma-services/v0/applyapi/v5/history"  # GET → {applyDetails, matchingRowsCount}
SAVED_JOBS_API = "/jobapi/v3/user/savedJobs/detail"  # GET → {totaljobs, list}

# Application status detail
APPLICATION_STATUS_API = "/cloudgateway-apply/whtma-services/v0/applyapi/v3/history-description"

# Inbox & messaging
INBOX_API = "/cloudgateway-nc-js/nc-services/v0/template/ni-inboxusermails-svc-tmpl_v0"
MESSAGE_API = "/cloudgateway-mynaukri/resman-aggregator-services/v1/inbox/users/self/mail"
INBOX_MARK_INTERESTED_API = "/cloudgateway-mynaukri/resman-aggregator-services/v0/inbox/users/self/markInterested"

# Recommended jobs
RECOMMENDED_JOBS_API = "/jobapi/v2/search/recom-jobs"

# Notification center (discovered via naukri_debug action="fetch_api" + "click_discover")
NOTIFICATION_FEED_API = "/cloudgateway-mynaukri/notification-center-services/v0/naukrinotificationcentre/user/self/feed"  # GET ?page=1&limit=20 → [{id, type, message, displayTitle, createdAt, readStatus, url, metadata, ...}]
NOTIFICATION_COUNT_API = "/cloudgateway-mynaukri/notification-center-services/v0/naukrinotificationcentre/user/self/count"  # GET → {count: N}
NOTIFICATION_READ_API = "/cloudgateway-mynaukri/notification-center-services/v0/naukrinotificationcentre/user/self/read"  # PUT/POST (405 on GET) — mark notification(s) as read

# Dashboard & analytics
DASHBOARD_API = "/cloudgateway-mynaukri/resman-aggregator-services/v1/users/self/dashboard"
DASHBOARD_PROPERTIES = "userDetails,profilePerformance,incompleteSection,isPaidUser,profileSegment,lookupData,res360NotifType,photoInfo,campusData,aiInterviewEligibility"
MATCH_ANALYTICS_API = "/cloudgateway-apply/whtma-services/v0/users/self/apply-match-score"
APPLY_MATCH_SCORE_API = "/cloudgateway-apply/whtma-services/v0/users/self/apply-match-score"  # GET ?days=7

# Job Alerts (discovered via webpack chunk analysis of SRP page JS + live API research)
# SSA = "Save Search Alert" — create-only endpoint (POST)
JOB_ALERT_API = "/alertapi/v2/ssa"  # POST → create alert {name, keyword, location, functionAreaId, roleId, experience, minCTC, industryTypeId, email}
# CJA = "Custom Job Alerts" — the unified list endpoint (GET) that returns both SSA and CJA alerts
JOB_ALERTS_LIST_API = "/alertapi/v2/user/cjas"  # GET → {list: [{alertId, name, keywords, location, functionAreaId, roleId, experience, minCTC, maxCTC, industryTypeId, alertType, email}]}

# Save/Unsave Jobs (discovered via webpack config module 55841)
SAVE_JOB_API = "/jobapi/v3/user/savejob/"  # POST (append job_id)
UNSAVE_JOB_API = "/jobapi/v3/user/unsavejob/"  # POST (append job_id)

# Additional APIs discovered via webpack bundle analysis
SEARCH_API = "/jobapi/v3/search"  # GET → {noOfJobs, clusters, jobDetails, ...}
JOB_DETAIL_API = "/jobapi/v3/job/"  # GET (append job_id)
JOB_MATCH_SCORE_API = "/jobapi/v3/job/"  # GET (append job_id + /matchscore)
SIMILAR_JOBS_API = "/jobapi/v2/search/simjobs/"  # GET (append job_id)
APPLY_WORKFLOW_API = "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply"  # POST
REPORT_FRAUD_API = "/servicegateway-apply/fraud-detection/1.0/jobseeker/report"  # POST
COMPANY_FOLLOW_STATUS_API = "/cloudgateway-mynaukri/jobseeker-follow-services/v0/users/self/companygroups-follow-status"  # GET
BATCH_FOLLOW_STATUS_API = "/cloudgateway-mynaukri/jobseeker-follow-services/v0/users/self/companygroups-follow-status"  # POST {groups: [id1, id2, ...]} → {followedGroups, unfollowedGroups}

# Profile editing & settings
PROFILE_API = "/cloudgateway-mynaukri/resman-aggregator-services/v2/users/self"
FULLPROFILES_API = "/cloudgateway-mynaukri/resman-aggregator-services/v0/users/self/fullprofiles"
FORMATTED_SETTINGS_API = "/servicegateway-mynaukri/settings-services/v0/user/self/formattedsettings"
SETTINGS_API = "/servicegateway-mynaukri/settings-services/v0/user/self/settings"
BLOCKED_COMPANIES_API = "/servicegateway-mynaukri/settings-services/v0/user/self/blockedCompanies"
COMPANY_SEARCH_API = "/companyapi/v1/search"

# Profile Performance & Recruiter Analytics
SEARCH_IMPRESSIONS_API = "/cloudgateway-apply/profile-performance/v0/jobseeker/self/search-impressions"
RECRUITER_ACTIVITY_API = "/cloudgateway-nc-js/nc-services/v0/template/ni-jobseeker-activity-svc-tmpl_v0"
ACTIVITY_LEVEL_API = "/cloudgateway-mynaukri/resman-aggregator-services/v0/users/self/activityLevel"

# Naukri 360 / Subscription & Services
N360_CONFIG_API = "/n360-services/v1/config-n360-pro"
MOCK_INTERVIEW_TOPICS_API = "/cloudgateway-naukri360/jobseeker-order-management-services/v0/users/self/mock-interview/topics"
MOCK_INTERVIEW_HISTORY_API = "/cloudgateway-naukri360/jobseeker-order-management-services/v0/users/self/mock-interview/previousInterview"
MOCK_INTERVIEW_ROLE_API = "/cloudgateway-naukri360/jobseeker-order-management-services/v0/users/self/mock-interview/role"
MOCK_INTERVIEW_OTHER_TOPICS_API = "/cloudgateway-naukri360/jobseeker-order-management-services/v0/users/self/mock-interview/other-topics"
MOCK_INTERVIEW_QUESTION_API = "/cloudgateway-naukri360/jobseeker-order-management-services/v0/users/self/mock-interview/question"

# Resume & Photo
RESUME_DOWNLOAD_API = "/cloudgateway-mynaukri/resman-aggregator-services/v0/users/self/resume"
PHOTO_API = "/cloudgateway-mynaukri/resman-aggregator-services/v2/users/self/photo"

# Single Alert Detail
ALERT_DETAIL_API = "/alertapi/v2/user/cja"  # GET (append /{alertId})

# Early Access / Pre-posted Roles
EARLY_ACCESS_API = "/jobapi/v1/search/pseudojobs"

# Resume Builder (Naukri 360)
RESUME_BUILDER_CONFIG_API = "/cloudgateway-naukri360/jobseeker-order-management-services/v0/users/self/services/resumeBuilder/configurations"
RESUME_BUILDER_STATUS_API = "/cloudgateway-naukri360/jobseeker-order-management-services/v0/users/self/services/v1/details"

# AmbitionBox (used by ambitionbox.py and health.py)
AMBITIONBOX_BASE = "https://www.ambitionbox.com"
AB_GATEWAY = f"{AMBITIONBOX_BASE}/servicegateway-ambitionbox"
AB_SALARY_API = f"{AB_GATEWAY}/salaries-services/v0/company"
AB_BENEFITS_API = f"{AB_GATEWAY}/benefits-services/v0/company"
AB_REVIEW_DIST_API = f"{AB_GATEWAY}/review-services/v0/review/distribution"
AB_REVIEW_FILTERS_API = f"{AB_GATEWAY}/review-services/v0/review/filters"
AB_INTERVIEW_QS_API = f"{AB_GATEWAY}/interview-services/v0/company/top-questions"
AB_COMPANY_COMPARE_API = f"{AB_GATEWAY}/company-services/v0/compare/company/top-comparsions"
AB_COMPANY_LOCATIONS_API = f"{AB_GATEWAY}/company-services/v0/company"
AB_INSIGHTS_APPLIED_API = f"{AB_GATEWAY}/insights-services/v0/insights/niAppliedJobs"
AB_COOKIE_TTL = 1800  # 30 minutes

# Conversion constants
LAKHS_MULTIPLIER = 100_000  # 1 lakh = 100,000 — used for CTC conversion across alerts, jobs, parsing, insights

# Operational limits
DAILY_APPLY_QUOTA = 50  # Naukri's daily application limit
BATCH_APPLY_DEFAULT_DELAY_MS = 500  # Delay between batch apply requests
MAX_BULK_JOBS = 50  # Max jobs to score in bulk operations

# Apply throttling / anti-detection cadence.
# A constant request cadence is itself a bot tell, so the apply path is both
# rate-limited (token bucket) AND jittered. These are conservative defaults;
# override via env. APPLY_RATE_MAX_CALLS within APPLY_RATE_PERIOD_SECONDS caps
# sustained throughput; APPLY_JITTER_* randomizes the gap between submissions.
APPLY_RATE_MAX_CALLS = int(os.environ.get("NAUKRI_APPLY_RATE_MAX_CALLS", "12"))
APPLY_RATE_PERIOD_SECONDS = float(os.environ.get("NAUKRI_APPLY_RATE_PERIOD_SECONDS", "60"))
# Extra random delay (seconds) added to the configured batch delay between
# submissions so the cadence is non-constant. min must be <= max.
APPLY_JITTER_MIN_SECONDS = float(os.environ.get("NAUKRI_APPLY_JITTER_MIN_SECONDS", "0.4"))
APPLY_JITTER_MAX_SECONDS = float(os.environ.get("NAUKRI_APPLY_JITTER_MAX_SECONDS", "1.5"))

# Cache TTLs (seconds)
PROFILE_CACHE_TTL = 30  # Profile + dashboard cache TTL
STALE_THRESHOLD_DAYS = 14  # Days before an application is considered stale

# AmbitionBox timeouts
AMBITIONBOX_WAIT_TIMEOUT = 10000  # Playwright timeout for __NEXT_DATA__ selector (ms)
AMBITIONBOX_FALLBACK_SLEEP = 2  # Seconds to wait if selector times out

# Browser timing constants (seconds) — used by tools to wait for browser state transitions
BROWSER_DOM_SETTLE = 0.5           # Wait for DOM to update after input/chip removal
BROWSER_MODAL_APPEAR = 2.0        # Wait for modal/dialog to appear after clicking edit
BROWSER_FORM_SAVE = 3.0           # Wait for form save to complete (POST round-trip)
BROWSER_PAGE_SETTLE = 1.0         # Wait for page to finish rendering after navigation
BROWSER_UPLOAD_COMPLETE = 5.0     # Wait for file upload to complete
BROWSER_PAGE_LOAD = 3.0           # Wait for full page load after goto (profile, settings)
BROWSER_FORM_LOAD = 4.0           # Wait for form to load after navigation (alert modify)

# Browser hardcoded timeouts
SESSION_VALIDATE_TIMEOUT = 5  # Seconds for session validation on startup
TOKEN_RENEWAL_TIMEOUT = 15000  # Milliseconds for token renewal navigation
POOL_CHECKOUT_TIMEOUT = 30  # Max seconds to wait for a browser tab

# Cache purge threshold
CACHE_PURGE_DAYS = 30  # Days before cached answers are purged

# Apply timeouts (seconds)
BATCH_APPLY_PER_JOB_TIMEOUT = 30    # Per-job apply timeout
BATCH_APPLY_TOTAL_TIMEOUT = 120     # Overall batch gather timeout

# Overall browser operation timeout (seconds) — caps total wall-clock time for any
# single browser-based mutation (profile update, boost, alert edit/delete, file upload).
# Individual element timeouts are shorter, but this prevents indefinite hangs if the
# site itself is unresponsive.
BROWSER_OPERATION_TIMEOUT = 60

# Browser intercept wait (seconds)
INTERCEPT_WAIT_TIMEOUT = 10         # Wait for API response intercept

# API retry behavior
API_MAX_RETRIES = 2                 # Max retries for retriable HTTP status codes
API_BACKOFF_BASE = 1.0              # Base delay (seconds) for exponential backoff
API_MAX_BACKOFF_SECONDS = 8         # Cap on backoff delay

# Additional operational limits
MAX_MARK_ALL_ITERATIONS = 10        # Cap on mark-all-read loop iterations
RESUME_MAX_SIZE_MB = 5              # Max resume upload size in MB
BULK_FETCH_CONCURRENCY = 3          # Concurrency limit for bulk job detail fetches
BATCH_APPLY_DEFAULT_CONCURRENCY = 3 # Default max_concurrent for batch operations

# Tier 21: Validated endpoints from deep probing (2026-03-02)
BULK_JOBS_API = "/jobapi/v2/jobs"                          # POST {jobIds: [...]}
JOB_DETAIL_V1_API = "/jobapi/v1/job/"                      # GET + job_id (97 keys, walk-in/contact/metrics)
INBOX_REST_API = "/cloudgateway-mynaukri/resman-aggregator-services/v1/inbox/users/self/mails"  # GET with pageSize/pageNo/mailType

# Widget API headers (appid:109) — required for performance, settings, NC template endpoints
WIDGET_HEADERS = {
    "appid": "109",
    "systemid": "109",
}

# Company API (different appid from main API — discovered via webpack analysis)
COMPANY_API_HEADERS = {
    "appid": "103",
    "systemid": "Naukri",
}

# Tier 22: Team probing discoveries (2026-03-02)
RECOMMEND_NOTIFY_API = "/recommendapi/v1/notify"
ENTITY_TAXONOMY_API = "/cloudgateway-central-services/central-entity-services/v0/entity/entity_depart/role_categ/role"

# DFP (DoubleClick for Publishers) profile targeting
DFP_PROFILE_API = "/jobapi/v1/ads/new/dfp"

# CCS (Content/Campaign Serving) — profile completion widgets (requires browser cookies, not JWT)
CCS_PAGE_API = "/cloudgateway-ccs/inventory-management-services/v2/page/pagename"
CCS_DASHBOARD_PAGE = "ni-desktop-dashboard-v2"
