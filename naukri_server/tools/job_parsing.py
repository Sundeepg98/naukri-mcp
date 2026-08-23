"""Shared job-list parsing helpers used by search and company tools."""

import logging

from naukri_server.config import NAUKRI_BASE
from naukri_server.domain.job import (
    ParsedSalary, normalize_work_mode, work_mode_from_location,
)

logger = logging.getLogger(__name__)


def _absolute_url(value):
    """Render a Naukri path/slug as a full URL. None stays None.

    A value that is already absolute is returned untouched, so a payload that
    one day carries a real off-site link is not mangled into a naukri.com URL.
    """
    if not value:
        return None
    text = str(value)
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return "%s/%s" % (NAUKRI_BASE, text.lstrip("/"))


def _parse_job_list(job_details: list, limit: int) -> list:
    """Parse Naukri's jobDetails array into a clean list of job dicts."""
    logger.info("Parsing %d job details (limit %d)", len(job_details), limit)
    jobs = []
    for job in job_details[:limit]:
        salary_detail = job.get("salaryDetail", {})
        parsed_salary = ParsedSalary.from_api(salary_detail)

        placeholders = job.get("placeholders", [])
        loc_label = None
        for ph in placeholders:
            if ph.get("type") == "location":
                loc_label = ph.get("label")
                break
        if not loc_label and placeholders:
            loc_label = placeholders[0].get("label")

        jobs.append({
            "job_id": job.get("jobId"),
            "title": job.get("title"),
            "company": job.get("companyName"),
            "salary": parsed_salary.label,
            "salary_min_lakhs": parsed_salary.min_lakhs,
            "salary_max_lakhs": parsed_salary.max_lakhs,
            "location": loc_label,
            "experience": f"{job.get('minimumExperience', '?')}-{job.get('maximumExperience', '?')} Yrs",
            "experience_min": job.get("minimumExperience"),
            "experience_max": job.get("maximumExperience"),
            "is_applied": job.get("isApplied", False),
            "posted_date": job.get("createdDate") or job.get("footerPlaceholderLabel"),
            "tags": [t.strip() for t in job.get("tagsAndSkills", "").split(",") if t.strip()] if isinstance(job.get("tagsAndSkills"), str) else job.get("tagsAndSkills", []),
            "url": (
                f"{NAUKRI_BASE}{job['jdURL']}" if job.get("jdURL")
                else f"{NAUKRI_BASE}/job-listings-{job.get('jobId', '')}"
            ),
            "vacancies": job.get("vacancy") or job.get("vacany"),
            "apply_count": job.get("applyCount"),          # populated in detail only
            "function_area": job.get("functionArea"),       # populated in detail only
            "group_id": job.get("groupId"),
            # LIST payloads carry neither workMode nor wfhType (measured:
            # 42 keys, neither present), so fall back to the mode Naukri
            # prints at the head of the location label. None when absent.
            "work_mode": normalize_work_mode(
                job.get("workMode") or job.get("wfhType")
            ) or work_mode_from_location(loc_label),
            "is_saved": job.get("isSaved"),
            "company_rating": (job.get("ambitionBoxData") or {}).get("Rating") or (job.get("ambitionBoxData") or {}).get("AggregateRating"),
            "company_reviews_count": (job.get("ambitionBoxData") or {}).get("ReviewsCount"),
            "candidates_count": job.get("candidatesCount"), # populated in detail only
            "type_of_business": job.get("typeOfBusiness"),  # populated in detail only
            "description_snippet": job.get("jobDescription"),
            "jd_url": job.get("jdURL"),
            "is_consultant": job.get("consultant", False),
            "hiring_for": job.get("hiringFor") or None,
            "client_company": job.get("clientTitleString"),
            "client_group_id": job.get("clientGroupId"),
            "diversity_tag": job.get("diversityTagText") or None,
            "experience_text": job.get("experienceText") or None,
            "logo_url": job.get("logoPathV3") or job.get("logoPath"),
            "company_id": job.get("companyId"),
            "is_exclusive": job.get("exclusive", False),
            "freshness_color": job.get("footerPlaceholderColor"),
            "is_agent_eligible": job.get("agentEligible", False) or job.get("isAgentEligible", False),
            # --- how this job is applied to -----------------------------
            # Three keys this whitelist used to drop. Census 2026-08-23,
            # N = 124 distinct jobs over five surfaces that answered 200:
            #   mode     jp 54 | resdexSearch 50 | airex 5 | crawled 4 | absent 11
            #   companyApplyJob   false 58 | TRUE 5 | absent 61
            #   clientCareersUrl  9 non-null | absent 115
            # All 4 crawled jobs carry companyApplyJob true, plus one jp job.
            #
            # NO `, False` DEFAULT on is_company_apply: 61 of 124 payloads
            # carry no such key (every resdexSearch row among them), and
            # defaulting would report "does not use company apply" about a
            # payload that says nothing either way.
            #
            # `listing_mode` is the SOURCING channel and is a different
            # field from `work_mode` (remote/hybrid/office) above.
            "listing_mode": job.get("mode"),
            "is_company_apply": job.get("companyApplyJob"),
            # Named "careers url" but it is a NAUKRI page, not the client's
            # own site: the slug seedling-labs-jobs-careers-25659 resolves on
            # naukri.com and redirects to the POSTING consultancy's page
            # (25659 is Pylon's companyId, not Seedling Labs'). Measured, not
            # assumed. Rendered absolute the way `url` is, so a caller can
            # open it; an already-absolute value is passed through.
            "client_careers_url": _absolute_url(job.get("clientCareersUrl")),
        })
        # Structured salary detail from bulk fetch API — add raw fields if available
        if isinstance(salary_detail, dict):
            jobs[-1]["salary_min_raw"] = salary_detail.get("minimumSalary")
            jobs[-1]["salary_max_raw"] = salary_detail.get("maximumSalary")
            jobs[-1]["salary_hidden"] = salary_detail.get("hideSalary", False)
    return jobs
