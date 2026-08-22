# -*- coding: utf-8 -*-
"""Final verdicts, with manual overrides where the auto-classifier is blunt.

The account was LOGGED OUT for the whole sweep (token expired 8.5h before it
started), so a sixth class is needed and is reported honestly rather than
folded into `correct`:

    unverified-auth-blocked -- the tool refused honestly with a clear
    not-logged-in error. That is the RIGHT behaviour, and it is all we learned:
    whether its fields are right cannot be known until there is a session.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RES = json.load(open(os.path.join(HERE, "results.json")))
V = json.load(open(os.path.join(HERE, "verdicts.json")))
import spec  # noqa: E402

# name -> (verdict, one-line evidence)
OVERRIDE = {
    # --- classifier was blunt: these two REPORT the auth failure as their job
    "naukri_auth_status": ("correct",
        "logged_in false + reason no_token; matches the expired JWT on disk (exp 8.5h ago)"),
    "naukri_health_check": ("correct",
        "5/5 checks fail with NotLoggedInError + real watchdog counters; honest"),
    # --- daily_brief: honest at the top, zero-filled underneath
    "naukri_daily_brief": ("empty-unclear",
        "partial_success + names all 14 failures, but still emits dashboard.profile_views=0, "
        "recommendations.count=0, unread_messages.count=0 -- a hard 0 for every fetch that DIED"),
    "naukri_interview_prep": ("wrong-fields",
        "status=success but silently OMITS matched_skills/missing_skills/sample_questions/"
        "company_rating when the 3 sub-fetches die (application_service.py:251-261); "
        "docstring promises all four"),
    "naukri_recruiter_history": ("wrong-fields",
        "total_companies=20 on an account with 115 distinct companies -- SQL LIMIT 20 became "
        "the total; responsive/unresponsive described the page. FIXED + test"),
    "naukri_research_company": ("empty-unclear",
        "status=success with zero data and errors=[2x AttributeError]; a caller checking "
        "status sees success"),
    # --- the page_pool family: raw AttributeError -> INTERNAL_ERROR
    "naukri_company_intel": ("errored", "AttributeError 'NoneType'.acquire leaked as a raw tool error. FIXED"),
    "naukri_company_jobs": ("errored", "INTERNAL_ERROR: AttributeError 'NoneType'.acquire. FIXED"),
    "naukri_company_slug": ("errored", "INTERNAL_ERROR: AttributeError 'NoneType'.acquire. FIXED"),
    "naukri_debug": ("errored", "AttributeError 'NoneType'.acquire leaked as a raw tool error. FIXED"),
    "naukri_profile_prompts": ("errored", "INTERNAL_ERROR: AttributeError 'NoneType'.acquire. FIXED"),
    "naukri_status_changes": ("errored",
        "INTERNAL_ERROR: AttributeError 'NoneType'.acquire. FIXED. Separately: this "
        "analytics-named read runs a FULL sync incl. delete_applications_before"),
    "naukri_sync_applications": ("errored", "API_ERROR wrapping AttributeError 'NoneType'.acquire. FIXED"),
    "naukri_sync_saved": ("errored", "API_ERROR wrapping AttributeError 'NoneType'.acquire. FIXED"),
    # --- honest non-auth errors that are CORRECT behaviour
    "naukri_read_message": ("correct",
        "VALIDATION_ERROR naming all three required ids. NOTE: with real ids it mints a "
        "notification row per call (known, unfixed)"),
    "naukri_salary_position": ("correct",
        "NOT_FOUND: 'No salary data found in 162 applications' -- 162 matches the DB exactly"),
    "naukri_sync_saved_jobs": ("unverified-auth-blocked", "honest API_ERROR naming the login need"),
}

CROSS_CHECKED = {
    "naukri_list_applications": "total=162 == DB count(*) exactly; page/has_more present",
    "naukri_list_reminders": "reminders=50 == DB reminders table exactly",
    "naukri_list_saved_jobs": "total=2 == DB saved_jobs exactly (both rows are TEST DATA: J600/J700)",
    "naukri_cached_answers": "answers=21 == len(questions.json) exactly",
    "naukri_conversion_funnel": "total_applied=11 == DB applications applied_at>=30d exactly",
    "naukri_export_data": "record_count=162 == DB count(*); path validation correctly rejected a path outside exports/",
    "naukri_stale_applications": "total=151, count/page/has_more all present and consistent",
    "naukri_list_interview_rounds": "total_rounds=0 == DB interview_rounds (empty) -- corroborated empty",
    "naukri_agent_history": "total=0 == DB agent_runs (empty) -- corroborated empty",
    "naukri_task_history": "runs=5 of 493 scheduled_runs rows",
    "naukri_scheduler_status": "running=true, 11 tasks -- matches scheduler_tasks.py",
    "naukri_agent_status": "enabled=false, mode=dry_run -- matches agent_config.json",
    "naukri_agent_config": "matches agent_config.json on disk",
    "naukri_compare_offers": "offers=2 for the 2 real job_ids given",
    "naukri_draft_follow_up": "company/title/days_since_applied all match the DB row",
    "naukri_follow_up_priority": "5 items, consistent with stale_applications",
    "naukri_application_insights": "top_companies=10 over the real 162",
    "naukri_config": "returns the jobcore config sections",
}


def main():
    out = {}
    for name in spec.CALL:
        if name in OVERRIDE:
            v, ev = OVERRIDE[name]
        else:
            auto = V.get(name, {})
            av = auto.get("verdict")
            if av == "auth-blocked-honest":
                v, ev = "unverified-auth-blocked", auto.get("evidence", "")
            elif av in ("success-data", "success-scalar"):
                v = "correct"
                ev = CROSS_CHECKED.get(name, auto.get("evidence", ""))
            elif av == "errored":
                v, ev = "errored", auto.get("evidence", "")
            else:
                v, ev = "empty-unclear", auto.get("evidence", "")
        out[name] = {"verdict": v, "evidence": ev}
    for name, reason in spec.SKIP.items():
        out[name] = {"verdict": "skipped", "evidence": reason}

    counts = {}
    for r in out.values():
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("TOTAL TOOLS:", len(out))
    for k in ("correct", "wrong-fields", "empty-unclear", "errored",
              "unverified-auth-blocked", "skipped"):
        print("  %-26s %3d" % (k, counts.get(k, 0)))
    assert len(out) == 120, len(out)
    json.dump(out, open(os.path.join(HERE, "final_verdicts.json"), "w"),
              indent=1, default=str)

    for want in ("wrong-fields", "empty-unclear", "errored"):
        print("\n== %s ==" % want)
        for n, r in sorted(out.items()):
            if r["verdict"] == want:
                print("  %-32s %s" % (n, r["evidence"][:150]))


if __name__ == "__main__":
    main()
