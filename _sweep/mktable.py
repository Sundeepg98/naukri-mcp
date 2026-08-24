# -*- coding: utf-8 -*-
import json, os, re, sys
HERE = r"D:\workspace\projects\job-hunting\mcp-servers\naukri\_sweep"
sys.path.insert(0, HERE)
import spec
FV = json.load(open(os.path.join(HERE, "final_verdicts.json")))
AUTO = json.load(open(os.path.join(HERE, "verdicts.json")))
RES = json.load(open(os.path.join(HERE, "results.json")))

# Evidence written by hand where the auto string would carry personal data.
HAND = {
 "naukri_check_email": "is_email_verified=true, is_mobile_verified=true; email/mobile returned as values (redacted here)",
 "naukri_dashboard": "profile_views=343, experience_years and ctc_lpa populated (CTC redacted here)",
 "naukri_resume_info": "resume_headline, resume filename, last-updated all populated (text redacted here)",
 "naukri_photo_info": "has_photo=true, photo_url populated (redacted here)",
 "naukri_get_profile": "full profile object returned, all major sections populated (redacted here)",
 "naukri_draft_follow_up": "company/title/days_since_applied all match the DB row for the probe job (redacted here)",
 "naukri_match_analytics": "total_applies=3, field_breakdown over 6 fields, plus a user_details block (redacted here)",
 "naukri_match_quality": "total_applies=3; payload identical to match_analytics minus user_details",
 "naukri_get_application": "returns the probe job's stored application row (redacted here)",
 "naukri_audit_profile": "returns a populated per-section profile audit (redacted here)",
 "naukri_profile_targeting": "returns the targeting block (redacted here)",
 "naukri_tailor_resume": "returned a tailored resume payload for the probe job (redacted here)",
 "naukri_interview_prep": None,
 "naukri_recruiter_activity": "returns recruiter action rows incl. recruiter names (redacted here)",
 "naukri_search_impressions": "total_appearances_all_time=6120, recruiter_actions=343, windowed timeline present",
 "naukri_skill_gap": "returns present/missing skill sets over a 5-job sample (skill list redacted here)",
 "naukri_assess_fit": "returned a fit verdict with explanation for the probe job; applied=false",
 "naukri_compare_offers": "offers=2 for the two probe job ids",
 "naukri_compare_jobs": "compared the two probe job ids",
 "naukri_bulk_fetch_jobs": "fetched both probe job ids",
 "naukri_get_job": "returned the probe job's full detail",
 "naukri_job_detail_v1": "returned the v1 detail shape for the probe job",
 "naukri_similar_jobs": "returned similar-job rows for the probe job",
 "naukri_mock_interview_prep": "returned a prep payload for the probe job",
 "naukri_download_resume": "wrote a 64628-byte PDF to the scratchpad path",
 "naukri_list_early_access": "total=5, count=5",
 "naukri_research_company": "returned reviews for the probe company",
 "naukri_company_intel": "returned reviews for the probe company",
 "naukri_search_companies": "returned company rows for the probe keyword",
 "naukri_company_jobs": "returned job rows for the probe group_id",
 "naukri_follow_status": "returned follow state for the probe group_id",
 "naukri_list_alerts": "count=3 alerts returned with ids and filters (ids redacted here)",
 "naukri_list_notifications": "total=5, count=5, all is_read=false",
 "naukri_notification_summary": "new_count=4; categories appStatus=11, rmj=11 (total_count 62)",
 "naukri_triage_inbox": "total_in_inbox=62, unread_in_inbox=11, returned=10, scored=true",
 "naukri_list_inbox": None,
 "naukri_blocked_companies": "returned the blocked-company list",
 "naukri_taxonomy": "returned the role taxonomy",
 "naukri_search_jobs": "total=19412, count=20 (Naukri caps a page at 20)",
 "naukri_get_recommendations": "count=5 of total=68",
 "naukri_cached_answers": "returned the cached screening answers (text redacted here)",
 "naukri_export_data": None,
 "naukri_application_insights": "top_companies aggregated over the 162 stored applications",
}

SENSITIVE = [spec.JOB_ID, spec.JOB_ID2]

def redact(s):
    if not s:
        return ""
    for j in SENSITIVE:
        s = s.replace(j, "<probe-job-id>")
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", s)
    s = re.sub(r"\b\d{10}\b", "<phone>", s)
    return s

rows = []
for name in spec.CALL:
    fv = FV[name]
    ev = HAND.get(name, "SENTINEL")
    if ev == "SENTINEL":
        ev = fv.get("evidence") or AUTO.get(name, {}).get("evidence", "")
    elif ev is None:
        ev = fv.get("evidence") or ""
    el = RES.get(name, {}).get("elapsed")
    rows.append((name, fv["verdict"], redact(ev).replace("|", "/"), el))

order = {"wrong-fields": 0, "empty-unclear": 1, "errored": 2, "correct": 3}
rows.sort(key=lambda r: (order.get(r[1], 9), r[0]))
out = ["| # | Tool | Verdict | s | Evidence |", "|---|------|---------|---|----------|"]
for i, (n, v, e, el) in enumerate(rows, 1):
    out.append("| %d | `%s` | %s | %s | %s |" % (i, n, v, el, e[:300]))
open(os.path.join(HERE, "_table_called.md"), "w", encoding="ascii", errors="replace").write("\n".join(out))

sk = ["| # | Tool | Reason it was not called |", "|---|------|--------------------------|"]
for i, (n, r) in enumerate(sorted(spec.SKIP.items()), 1):
    sk.append("| %d | `%s` | %s |" % (i, n, redact(r).replace("|", "/")))
open(os.path.join(HERE, "_table_skipped.md"), "w", encoding="ascii", errors="replace").write("\n".join(sk))
print("rows:", len(rows), "skipped:", len(spec.SKIP))
