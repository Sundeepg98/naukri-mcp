"""
Configuration constants and logging setup for Naukri MCP server.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("naukri")

# Paths — go up from naukri_server/ to naukri/ where chrome-profile/ and questions.json live
CHROME_PROFILE = str(Path(__file__).parent.parent / "chrome-profile")
CACHE_FILE = Path(__file__).parent.parent / "questions.json"
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

# Conversion constants
LAKHS_MULTIPLIER = 100_000  # 1 lakh = 100,000 — used for CTC conversion across alerts, jobs, parsing, insights

# Operational limits
DAILY_APPLY_QUOTA = 50  # Naukri's daily application limit
BATCH_APPLY_DEFAULT_DELAY_MS = 500  # Delay between batch apply requests
MAX_BULK_JOBS = 50  # Max jobs to score in bulk operations

# Cache TTLs (seconds)
PROFILE_CACHE_TTL = 30  # Profile + dashboard cache TTL
STALE_THRESHOLD_DAYS = 14  # Days before an application is considered stale

# AmbitionBox timeouts
AMBITIONBOX_WAIT_TIMEOUT = 10000  # Playwright timeout for __NEXT_DATA__ selector (ms)
AMBITIONBOX_FALLBACK_SLEEP = 2  # Seconds to wait if selector times out

# Browser hardcoded timeouts
SESSION_VALIDATE_TIMEOUT = 5  # Seconds for session validation on startup
TOKEN_RENEWAL_TIMEOUT = 15000  # Milliseconds for token renewal navigation

# Cache purge threshold
CACHE_PURGE_DAYS = 30  # Days before cached answers are purged

# Apply timeouts (seconds)
BATCH_APPLY_PER_JOB_TIMEOUT = 30    # Per-job apply timeout
BATCH_APPLY_TOTAL_TIMEOUT = 120     # Overall batch gather timeout

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

# Company API (different appid from main API — discovered via webpack analysis)
COMPANY_API_HEADERS = {
    "appid": "103",
    "systemid": "Naukri",
}
