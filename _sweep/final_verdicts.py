# -*- coding: utf-8 -*-
"""Final verdicts, with manual overrides where the auto-classifier is blunt.

    unverified-auth-blocked -- the tool refused honestly with a clear
    not-logged-in error. That is the RIGHT behaviour, and it is all we learned:
    whether its fields are right cannot be known until there is a session.

RUN SELECTION (added 2026-08-24). The override tables below are OBSERVATIONS OF
ONE RUN, not standing truths, and applying one run's observations to another
run's data manufactures verdicts. The 2026-08-22 sweep ran LOGGED OUT; the
2026-08-24 sweep ran against a live, api_confirmed session. Every conclusion
that named an auth failure or a `NoneType.acquire` page_pool crash therefore
describes 08-22 ONLY -- e.g. the 08-22 table forces naukri_auth_status to
"logged_in false + reason no_token", which is FALSE of the live run.

`RUN` picks which table is applied. It is derived from the data (the observed
auth state in results.json), never hardcoded, so a re-run cannot silently
inherit the wrong table.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RES = json.load(open(os.path.join(HERE, "results.json")))
V = json.load(open(os.path.join(HERE, "verdicts.json")))
import spec  # noqa: E402

# Which run is results.json? Derived from the data, never hardcoded: the
# 08-22 sweep observed logged_in false, the live sweep observes true.
_auth = (RES.get("naukri_auth_status") or {}).get("parsed") or {}
RUN = "live" if _auth.get("logged_in") is True else "loggedout"

# name -> (verdict, one-line evidence)
# ---- 2026-08-22, LOGGED OUT. Retained as the record of that run. NOT applied
#      to a live run: every entry naming an auth failure or a NoneType.acquire
#      page_pool crash is an artefact of the logged-out session.
OVERRIDE_20260822 = {
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

CROSS_CHECKED_20260822 = {
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


# ---- 2026-08-24, LIVE session (api_confirmed). Populated from THIS run's
#      responses cross-checked against naukri.db. Empty entries are not
#      guesses: a tool absent here is classified by the auto-classifier alone.
OVERRIDE_LIVE = {
    # --- the headline defect: a hard 0 the live session PROVES wrong
    "naukri_daily_brief": ("wrong-fields",
        "unread_messages.count=0 while list_inbox and triage_inbox both report unread=11 in "
        "the same sweep. Sourced from _fetch_inbox(unread_only=True), whose page returns "
        "count=0/messages=[] beside its own unread=11; the fetch SUCCEEDS so the 08-22 "
        "None-guard never engages, and errors[] names only the AmbitionBox 403. Everything "
        "else in the brief is real data and honestly partial_success"),
    "naukri_list_inbox": ("wrong-fields",
        "unread_only=True returns count=0/messages=[] while the SAME response says unread=11 "
        "and total=62; unread_only=False returns count=5. Measured twice, post-sweep"),
    # --- a second hard 0, contradicted by its own siblings
    "naukri_notification_count": ("wrong-fields",
        "count=0 while notification_summary reports new_count=4 (appStatus 11, rmj 11) and "
        "list_notifications returns 5 rows all is_read=false. safe_get(data,'count',default=0) "
        "is called without warn/field_name, so a missing or drifted key becomes a confident 0"),
    # --- a permanently broken sub-fetch, but honestly surfaced
    "naukri_interview_prep": ("wrong-fields",
        "partial_success; errors[] carries ImportError: cannot import name "
        "'naukri_mock_interview'. application_service.py:261 still calls the consolidated "
        "dispatcher that the de-consolidation removed, so the mock-topics section can never "
        "populate for any job. Surfaced, not silent -- and the label is doubled "
        "('Mock topics: Mock topics: ...')"),
    # --- classifier was blunt: these errors ARE the correct behaviour
    "naukri_read_message": ("correct",
        "VALIDATION_ERROR naming all three required ids. NOTE: with real ids it mints a "
        "notification row per call (known, unfixed, not exercised here)"),
    "naukri_salary_position": ("correct",
        "NOT_FOUND: 'No salary data found in 162 applications' -- 162 == DB count(*) exactly"),
    "naukri_company_slug": ("correct",
        "NOT_FOUND naming the true cause: that group_id has no AmbitionBox integration"),
    "naukri_mock_interview_topics": ("correct",
        "passes Naukri's own HTTP 400 'No Role Activated for this user' through with "
        "http_status intact -- an account-state refusal, not a server defect"),
    # --- honest partials
    "naukri_follow_up_priority": ("correct",
        "partial_success and names the inbox sub-fetch in errors[]; stale_applications=5 is "
        "real. The inbox worked 15 calls later, so that sub-fetch was transient"),
    "naukri_salary_benchmark": ("correct",
        "partial_success with jobs_sampled=5 / jobs_with_salary=0 -- Naukri hides salary on "
        "most posts and the tool says which half it got"),
    # --- a zero with no explanation attached
    "naukri_score_saved_jobs": ("empty-unclear",
        "total_saved=2, scored_count=0, scored_jobs=[] and no channel saying why neither "
        "scored. Both saved rows are TEST DATA (J600/J700), so this is likely correct "
        "behaviour reported without its reason"),
    "naukri_session_info": ("correct",
        "authenticated=true measured against the activityLevel endpoint; nauk_rt/nauk_sid/"
        "nauk_cs correctly report present=null with the locked-cookie-jar reason rather than "
        "false. NOTE: it is the only tool with no top-level `status` key"),
}

CROSS_CHECKED_LIVE = {
    "naukri_list_applications": "total=162 == DB applications count(*) exactly",
    "naukri_export_data": "record_count=162 == DB count(*) exactly",
    "naukri_list_reminders": "total=50 == DB reminders exactly",
    "naukri_list_saved_jobs": "total=2 == DB saved_jobs exactly (both rows are TEST DATA)",
    "naukri_recruiter_history": "total_companies=115 == DB distinct companies exactly -- the "
                                "08-22 'LIMIT 20 became the total' bug is FIXED and this run "
                                "is the live confirmation",
    "naukri_conversion_funnel": "total_applied=11 == DB applied_at>=30d exactly",
    "naukri_agent_history": "total=0 == DB agent_runs (empty) -- corroborated empty",
    "naukri_list_interview_rounds": "total_rounds=0 == DB interview_rounds (empty)",
    "naukri_stale_applications": "total=151 of 162, count/page/has_more consistent",
    "naukri_auth_status": "logged_in true + verified true + api_confirmed; matches the "
                          "unexpired nauk_at and session_info's live endpoint check",
    "naukri_server_info": "commit 1bc55528dec1 / pid 43228 / surface.tools=125 -- matches the "
                          "process and the live tools/list exactly",
    "naukri_triage_inbox": "total_in_inbox=62 and unread_in_inbox=11 agree with list_inbox; "
                           "scored=true, scoring_error=null over 10 returned rows",
    "naukri_health_check": "5 checks, 21 probes, 3 degraded, api_metrics.errors=27 -- real "
                           "counters, not placeholders",
    "naukri_debug": "browser_snapshot returned a live DOM with 5 [data-job-id] nodes",
}

OVERRIDE = OVERRIDE_LIVE if RUN == "live" else OVERRIDE_20260822
CROSS_CHECKED = CROSS_CHECKED_LIVE if RUN == "live" else CROSS_CHECKED_20260822


def main():
    print("RUN: %s (naukri_auth_status.logged_in=%r)\n" % (RUN, _auth.get("logged_in")))
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
    assert len(out) == len(spec.CALL) + len(spec.SKIP), len(out)
    json.dump(out, open(os.path.join(HERE, "final_verdicts.json"), "w"),
              indent=1, default=str)

    for want in ("wrong-fields", "empty-unclear", "errored"):
        print("\n== %s ==" % want)
        for n, r in sorted(out.items()):
            if r["verdict"] == want:
                print("  %-32s %s" % (n, r["evidence"][:150]))


if __name__ == "__main__":
    main()
