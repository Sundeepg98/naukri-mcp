# -*- coding: utf-8 -*-
"""Sweep spec: every one of the 125 registered tools is either CALL(args) or SKIP(reason).

2026-08-24: the live surface grew from 120 to 125. The five newcomers
(server_info, session_info, triage_inbox, logout, reauth) were classified
against the same standard as the rest -- see the auth and inbox sections.
"""

JOB_ID = "210826815108"        # InApp / MCP Developer - real, from his applications table
JOB_ID2 = "060426011432"       # IQVIA / Mean Stack Developer
SCRATCH = ("C:/Users/TestUser/AppData/Local/Temp/claude/"
           "D--workspace-projects-mcp-servers/"
           "00000000-0000-0000-0000-000000000000/scratchpad")

R_LEAD = "on the never-call list - irreversible account/quota/reputation write"
R_WRITE = "writes account or local state; not on the never-call list but unsafe to exercise"
R_OPER = "requires the operator at the keyboard (password/2FA/OTP)"

CALL = {}
SKIP = {}


def c(name, **args):
    CALL[name] = args


def s(name, reason):
    SKIP[name] = reason


# ---- auth -----------------------------------------------------------------
c("naukri_auth_status")
c("naukri_server_info")            # 2026-08-24: pure read, no network, no writes
c("naukri_session_info", verify_live=True)   # 2026-08-24: live check is a GET
s("naukri_login", R_OPER + " - opens a real sign-in flow")
s("naukri_verify_otp", R_OPER + " - consumes a one-time code")
s("naukri_logout",
  R_WRITE + " - deletes the cached nauk_at and auth_state.json, destroying "
  "mid-sweep the exact authenticated state this run exists to measure")
s("naukri_reauth",
  R_WRITE + " - mutates the cached credential and its browser_restart stage "
  "relaunches the persistent Chrome profile; browser restarts are out of scope")

# ---- search / jobs --------------------------------------------------------
c("naukri_search_jobs", keywords="node.js developer", location="bangalore")
c("naukri_get_recommendations", limit=5)
c("naukri_get_job", job_id=JOB_ID)
c("naukri_job_detail_v1", job_id=JOB_ID)
c("naukri_similar_jobs", job_id=JOB_ID, limit=5)
c("naukri_bulk_fetch_jobs", job_ids=[JOB_ID, JOB_ID2])
c("naukri_compare_jobs", job_ids=[JOB_ID, JOB_ID2], timeout_seconds=60)
c("naukri_compare_offers", job_ids=JOB_ID + "," + JOB_ID2)
s("naukri_report_fraud", R_LEAD)

# ---- applications / tracking ---------------------------------------------
c("naukri_list_applications", limit=5)
c("naukri_get_application", job_id=JOB_ID)
c("naukri_stale_applications", limit=5)
c("naukri_follow_up_priority", limit=5)
c("naukri_draft_follow_up", job_id=JOB_ID)
c("naukri_recruiter_history")
c("naukri_interview_prep", job_id=JOB_ID)
c("naukri_list_interview_rounds")
s("naukri_purge_applications", R_LEAD + " - DELETEs application history")
s("naukri_add_interview_round", R_WRITE + " - inserts an interview_rounds row")

# ---- apply (all forbidden) ------------------------------------------------
s("naukri_apply", R_LEAD)
s("naukri_batch_apply", R_LEAD)
s("naukri_apply_top_fits", R_LEAD)
s("naukri_auto_hunt", R_LEAD)
s("naukri_accept_nvite", R_LEAD)

# ---- saved jobs -----------------------------------------------------------
c("naukri_list_saved_jobs", limit=10)
s("naukri_save_job", R_WRITE + " - sync_to_naukri can push a bookmark upstream")
s("naukri_unsave_job", R_WRITE + " - removes one of his saved jobs")
c("naukri_sync_saved_jobs")
c("naukri_sync_saved")

# ---- profile --------------------------------------------------------------
c("naukri_get_profile")
c("naukri_audit_profile")
c("naukri_dashboard")
c("naukri_profile_targeting")
c("naukri_profile_prompts")
s("naukri_update_profile", R_LEAD)
s("naukri_boost_profile", R_LEAD + " - rewrites his live headline")

# ---- sync / export --------------------------------------------------------
c("naukri_sync_applications", days_back=7)
c("naukri_export_data", data_type="applications", export_format="json",
  output_path="exports/sweep_export_apps.json")

# ---- inbox ----------------------------------------------------------------
c("naukri_list_inbox", limit=5)
# 2026-08-24: triage walks the same _fetch_inbox pages list_inbox uses and
# scores each row locally -- no read_message, no upstream mark-read.
c("naukri_triage_inbox", limit=10)
c("naukri_read_message")          # no ids -> expect an honest VALIDATION_ERROR
s("naukri_mark_interested", R_LEAD)

# ---- notifications --------------------------------------------------------
c("naukri_list_notifications", limit=5)
c("naukri_notification_count")
c("naukri_notification_summary")
c("naukri_notification_prefs")
s("naukri_mark_notification_read", R_WRITE + " - marks one read upstream")
s("naukri_mark_all_notifications_read", R_LEAD)

# ---- settings -------------------------------------------------------------
c("naukri_get_settings")
c("naukri_blocked_companies")
c("naukri_check_email")
c("naukri_visibility")
c("naukri_subscription_status")
s("naukri_update_settings", R_LEAD)

# ---- alerts ---------------------------------------------------------------
c("naukri_list_alerts")
s("naukri_alert_detail",
  "needs a real alert_id; list_alerts is auth-blocked so none can be sourced. "
  "Never invent an id.")
s("naukri_create_alert", R_WRITE + " - creates a real job alert")
s("naukri_update_alert", R_WRITE)
s("naukri_delete_alert", R_LEAD)

# ---- companies ------------------------------------------------------------
c("naukri_search_companies", keyword="Infosys", limit=5)
c("naukri_company_jobs", group_id="4058675", limit=5)
c("naukri_company_slug", group_id="4058675")
c("naukri_follow_status", group_id="4058675")
c("naukri_research_company", keyword="Infosys", include_jobs=False,
  include_reviews=True, include_interviews=False, timeout_seconds=90)
c("naukri_company_intel", company="Infosys", intel_type="reviews")
s("naukri_follow_company", R_LEAD)

# ---- recruiter / activity -------------------------------------------------
c("naukri_search_impressions", days=30)
c("naukri_recruiter_activity", limit=5)
c("naukri_activity_level")

# ---- mock interview -------------------------------------------------------
c("naukri_mock_interview_topics")
c("naukri_mock_interview_history")
c("naukri_mock_interview_prep", job_id=JOB_ID)
s("naukri_start_mock_interview", R_WRITE + " - starts a real assessment on his account")
s("naukri_answer_mock_interview", R_WRITE + " - submits a real answer")

# ---- resume / photo -------------------------------------------------------
c("naukri_resume_info")
c("naukri_photo_info")
c("naukri_resume_templates")
c("naukri_resume_builder_status")
c("naukri_download_resume", save_path=SCRATCH + "/sweep_resume.pdf")
c("naukri_tailor_resume", job_id=JOB_ID, timeout_seconds=90)
s("naukri_upload_resume", R_LEAD)
s("naukri_upload_photo", R_LEAD)
s("naukri_delete_photo", R_LEAD)

# ---- early access ---------------------------------------------------------
c("naukri_list_early_access", limit=5)
s("naukri_share_early_access", R_LEAD)

# ---- health / debug -------------------------------------------------------
c("naukri_health_check", include_browser=False)
c("naukri_debug", action="browser_snapshot")

# ---- insights / analytics -------------------------------------------------
c("naukri_application_insights", days=30)
c("naukri_salary_position")
c("naukri_cached_answers", action="list")
c("naukri_match_analytics", days=30)
c("naukri_match_quality", days=30)
c("naukri_skill_gap", keywords="node.js", sample_size=5, timeout_seconds=90)
c("naukri_salary_benchmark", keywords="node.js developer", location="bangalore",
  sample_size=5, timeout_seconds=90)
c("naukri_taxonomy")
c("naukri_conversion_funnel", days=30)
c("naukri_status_changes", days=7)
c("naukri_daily_brief")

# ---- scoring --------------------------------------------------------------
c("naukri_assess_fit", job_id=JOB_ID, apply_if_fit=False, explain=True)
c("naukri_score_saved_jobs", min_fit_score=50, timeout_seconds=90)

# ---- reminders ------------------------------------------------------------
c("naukri_list_reminders", include_past=True)
s("naukri_set_reminder", R_WRITE + " - inserts a reminders row")

# ---- scheduler ------------------------------------------------------------
c("naukri_scheduler_status")
c("naukri_task_history", limit=5)
s("naukri_enable_task", R_WRITE + " - mutates scheduler config")
s("naukri_disable_task", R_WRITE + " - mutates scheduler config")
s("naukri_run_task_now", R_WRITE + " - can run the apply/agent task and spend quota")

# ---- agent ----------------------------------------------------------------
c("naukri_agent_status")
c("naukri_agent_config")
c("naukri_agent_history", limit=5)
s("naukri_agent_decisions",
  "needs a real cycle_id; the agent_runs table is empty so none exists. "
  "Never invent one.")
s("naukri_agent_run_now", R_LEAD)
s("naukri_agent_approve", R_LEAD)
s("naukri_agent_reject", R_LEAD)
s("naukri_agent_update_config", R_LEAD)

# ---- config / kill switch -------------------------------------------------
c("naukri_config")
s("naukri_set_config", R_LEAD)
s("naukri_kill_switch", R_LEAD)

# naukri_debug is CALLed with the read-only browser_snapshot action. Its
# api_post / api_put / api_delete actions write upstream and discover_click
# clicks a caller-supplied selector on the live logged-in page - never exercised.
DEBUG_UNSAFE_ACTIONS = ["api_post", "api_put", "api_delete", "discover_click"]
