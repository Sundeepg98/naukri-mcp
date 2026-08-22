"""Search service — pure domain logic for job parsing, scoring, and data transformation.

Extracts business logic from tools/search.py and tools/jobs.py into a service layer.
I/O-bound functions (API calls, browser automation) remain in the tool modules.
"""

import re
from typing import Optional

from naukri_server.config import LAKHS_MULTIPLIER
from naukri_server.domain import safe_get
from naukri_server.domain.job import (
    ParsedSalary, WORK_MODE_CODES, normalize_work_mode, work_mode_from_location,
)

__all__ = [
    "extract_job_id",
    "extract_skills",
    "format_education",
    "extract_placeholder",
    "parse_salary_data",
    "parse_company_data",
    "parse_match_score",
    "parse_job_detail",
    "build_search_result",
    "build_recommendations_result",
    "build_similar_jobs_result",
    "build_rest_search_params",
    "build_search_url",
    "build_search_filters",
    "parse_job_detail_v1",
    "parse_similar_jobs_rest",
    "flatten_match_score_rest",
    "JOB_TYPE_MAP",
    "WORK_MODE_MAP",
    "COMPANY_TYPE_MAP",
    "EDUCATION_MAP",
]


# ---------------------------------------------------------------------------
# Search-param translation tables (kept here as the single source of truth —
# both the REST and browser search paths consume them)
# ---------------------------------------------------------------------------

JOB_TYPE_MAP = {
    "fulltime": "1", "parttime": "2", "contract": "3",
    "internship": "4", "temporary": "5",
}

# wfhType codes, settled against the LIVE API 2026-08-22 rather than guessed.
# Two of the three entries used to be wrong, and the request map disagreed with
# the response map (WORK_MODE_CODES) about what "2" meant:
#
#   work_mode="office" sent wfhType=2 and the first result was a job located
#   "Remote". Asking for office work returned remote work.
#   work_mode="wfh"    sent wfhType=1 and returned 2 results ("Temp. WFH -
#   Ahmedabad") where the real remote code returns a full page.
#   work_mode="hybrid" sent wfhType=3 and returned "Hybrid - Bengaluru" -- the
#   only entry that was already right.
#
# Corroborated from the other direction: a job recommendations labelled
# "Hybrid - Chennai" carries wfhType 3, and one the inbox called "(Remote)"
# carries 2. Request and response now agree.
WORK_MODE_MAP = {"wfh": "2", "remote": "2", "hybrid": "3", "office": "0"}

COMPANY_TYPE_MAP = {
    "startup": "startup", "mnc": "mnc",
    "indian_mnc": "indianMNC", "corporate": "corporate",
}

EDUCATION_MAP = {
    "any_graduate": "0", "b_tech": "35", "mba": "41",
    "m_tech": "36", "diploma": "23", "phd": "46",
}


def build_search_filters(
    experience: int | None = None, salary_min: int | None = None,
    salary_max: int | None = None, sort_by: str | None = None,
    freshness: int | None = None, work_mode: str | None = None,
    job_type: str | None = None, company_type: str | None = None,
    industry: str | None = None, education: str | None = None,
    role_category: str | None = None, posted_within: int | None = None,
) -> dict:
    """Return the ``filters`` dict echoed back in search results.

    Centralised so the REST and browser paths cannot drift in shape.
    """
    return {
        "experience": experience, "salary_min": salary_min,
        "salary_max": salary_max, "sort_by": sort_by,
        "freshness": freshness, "work_mode": work_mode,
        "job_type": job_type, "company_type": company_type,
        "industry": industry, "education": education,
        "role_category": role_category, "posted_within": posted_within,
    }


def build_rest_search_params(
    keywords: str, location: str | None, page_no: int, limit: int,
    *, experience: int | None = None, salary_min: int | None = None,
    salary_max: int | None = None, sort_by: str | None = None,
    freshness: int | None = None, work_mode: str | None = None,
    job_type: str | None = None, company_type: str | None = None,
    industry: str | None = None, education: str | None = None,
    role_category: str | None = None, posted_within: int | None = None,
) -> dict:
    """Build the REST query-string dict for /jobapi/v3/search.

    Pure, easily unit-testable. Equivalent params should land at the same key
    regardless of caller (tool, agent, batch).
    """
    params: dict = {"keyword": keywords, "noOfResults": str(limit), "pageNo": str(page_no)}
    if location:
        params["location"] = location
    if experience is not None:
        params["experience"] = str(experience)
    if salary_min is not None:
        params["salary"] = f"{salary_min}-{salary_max or 999}"
    if sort_by:
        params["sortBy"] = sort_by
    if freshness is not None:
        params["jobAge"] = str(freshness)
    if industry:
        params["industry"] = industry
    if education:
        params["education"] = education
    if role_category:
        params["roleCategory"] = role_category
    if job_type:
        jt = JOB_TYPE_MAP.get(job_type.lower())
        if jt:
            params["jobType"] = jt
    if company_type:
        params["companyType"] = company_type
    if work_mode:
        # REST passes the string directly (browser path translates); kept identical
        # to existing wire format so behaviour is unchanged.
        params["wfhType"] = work_mode
    if posted_within is not None:
        params["jobAge"] = str(posted_within)
    return params


def build_search_url(
    base: str, keywords: str, location: str | None, page_no: int,
    *, experience: int | None = None, salary_min: int | None = None,
    salary_max: int | None = None, sort_by: str | None = None,
    freshness: int | None = None, work_mode: str | None = None,
    job_type: str | None = None, company_type: str | None = None,
    industry: str | None = None, education: str | None = None,
    role_category: str | None = None, posted_within: int | None = None,
) -> str:
    """Build the SEO-friendly Naukri search URL with query-string filters.

    Mirrors the browser-path URL builder previously inlined in
    naukri_search_jobs. Pure helper, no I/O.
    """
    slug = keywords.lower().replace(" ", "-").replace(".", "-")
    if location:
        loc_slug = location.lower().replace(" ", "-")
        url = f"{base}/{slug}-jobs-in-{loc_slug}"
    else:
        url = f"{base}/{slug}-jobs"
    qs: list[str] = []
    if experience is not None:
        qs.append(f"experience={experience}")
    if salary_min is not None:
        qs.append(f"nignbekg={salary_min}")
    if salary_max is not None:
        qs.append(f"naagnbekg={salary_max}")
    if sort_by:
        qs.append(f"sortBy={sort_by}")
    if freshness is not None:
        qs.append(f"jobAge={freshness}")
    if work_mode:
        wm_val = WORK_MODE_MAP.get(work_mode.lower())
        if wm_val:
            qs.append(f"wfhType={wm_val}")
    if job_type:
        jt = JOB_TYPE_MAP.get(job_type.lower())
        if jt:
            qs.append(f"jobType={jt}")
    if company_type:
        ct = COMPANY_TYPE_MAP.get(company_type.lower())
        if ct:
            qs.append(f"companyType={ct}")
    if industry:
        qs.append(f"industry={industry}")
    if education:
        edu = EDUCATION_MAP.get(education.lower(), education)
        qs.append(f"education={edu}")
    if role_category:
        qs.append(f"roleCategory={role_category}")
    if posted_within is not None:
        qs.append(f"jobAge={posted_within}")
    if page_no > 1:
        qs.append(f"pageNo={page_no}")
    if qs:
        url += "?" + "&".join(qs)
    return url


# ---------------------------------------------------------------------------
# Job ID extraction
# ---------------------------------------------------------------------------

def extract_job_id(job_url_or_id: str) -> str:
    """Extract numeric job ID from URL or pass through if already an ID.

    Raises ValueError if no valid numeric job ID can be extracted.
    """
    if job_url_or_id.isdigit():
        return job_url_or_id
    # URL pattern: ...-<jobId> at the end
    match = re.search(r'(\d{6,})', job_url_or_id)
    if match:
        return match.group(1)
    raise ValueError(f"Invalid job ID or URL: {job_url_or_id}")


# ---------------------------------------------------------------------------
# Skill / education / placeholder parsing
# ---------------------------------------------------------------------------

def extract_skills(raw_skills) -> list:
    """Extract skill labels from keySkills — handles both list and dict formats."""
    if isinstance(raw_skills, dict):
        # v3 format: {"preferred": [{label: "Python"}, ...], "other": [...]}
        skills = []
        for category_skills in raw_skills.values():
            if isinstance(category_skills, list):
                for s in category_skills:
                    label = s.get("label", s) if isinstance(s, dict) else s
                    if label and isinstance(label, str):
                        skills.append(label)
        return skills
    elif isinstance(raw_skills, list):
        return [s.get("label", s) if isinstance(s, dict) else s for s in raw_skills]
    return []


def format_education(edu) -> str | None:
    """Format the v4 education object into a readable string."""
    if not edu or not isinstance(edu, dict):
        return None
    parts = []
    ug = edu.get("ugQualification") or edu.get("ug")
    pg = edu.get("pgQualification") or edu.get("pg")
    if ug:
        parts.append(f"UG: {ug}")
    if pg:
        parts.append(f"PG: {pg}")
    return ", ".join(parts) if parts else None


def extract_placeholder(placeholders: list, ptype: str) -> str | None:
    """Extract a label from Naukri's placeholders array by type."""
    for p in placeholders:
        if isinstance(p, dict) and p.get("type") == ptype:
            return p.get("label")
    return None


# ---------------------------------------------------------------------------
# Salary / company / match-score parsing
# ---------------------------------------------------------------------------

def parse_salary_data(job: dict) -> dict:
    """Extract salary info from job data.

    Thin wrapper around ParsedSalary.from_api() — keeps the same return shape
    so callers (parse_job_detail, tests) don't need changes.
    """
    salary_detail = safe_get(job, "salaryDetail", field_name="salary", warn=True, default={})
    parsed = ParsedSalary.from_api(salary_detail)
    # Pass through raw values from the API dict for backward compatibility
    sal_min = salary_detail.get("minimumSalary", 0) if isinstance(salary_detail, dict) else 0
    sal_max = salary_detail.get("maximumSalary", 0) if isinstance(salary_detail, dict) else 0
    return {
        "salary": parsed.label,
        "salary_min": sal_min,
        "salary_max": sal_max,
    }


def parse_company_data(job: dict, details_data: dict) -> dict:
    """Extract company details + ambitionbox rating."""
    company = safe_get(job, "companyDetail", field_name="companyDetail", warn=True, default={})
    ambition = job.get("ambitionBoxData") or {}
    ab_details = details_data.get("ambitionBoxDetails", {})
    if not ambition and ab_details:
        ci = ab_details.get("companyInfo") or {}
        ambition = {
            "AggregateRating": ci.get("rating"),
            "ReviewsCount": ci.get("reviewsCount"),
        }
    return {
        "company_name": safe_get(company, "name", field_name="company_name", warn=True),
        "company_rating": safe_get(ambition, "AggregateRating", "Rating", field_name="company_rating", warn=False),
        "company_reviews_count": ambition.get("ReviewsCount"),
        "company_website": company.get("websiteUrl") if company else None,
        "group_id": company.get("groupId") if company else None,
    }


def parse_match_score(score_data: dict) -> dict:
    """Build normalized match_score dict from job scoring data (browser or REST)."""
    if not score_data:
        return {"match_score": None, "match_details": None}

    skill_mismatch_str = score_data.get("skillMismatch") or ""
    # Normalize to consistent dict format (same shape as _fetch_match_score REST path)
    match_score = {
        "education": (score_data.get("education", {}).get("userMatching", False)
                      if isinstance(score_data.get("education"), dict)
                      else score_data.get("education")),
        "functional_area": (score_data.get("functionalArea", {}).get("userMatching", False)
                            if isinstance(score_data.get("functionalArea"), dict)
                            else score_data.get("functionalArea")),
        "key_skills": score_data.get("Keyskills"),
        "work_experience": (score_data.get("Experience", {}).get("userMatching", False)
                            if isinstance(score_data.get("Experience"), dict)
                            else score_data.get("workExperience")),
        "industry": (score_data.get("Industry", {}).get("userMatching", False)
                     if isinstance(score_data.get("Industry"), dict)
                     else score_data.get("industry")),
        "location": (score_data.get("Location", {}).get("userMatching", False)
                     if isinstance(score_data.get("Location"), dict)
                     else score_data.get("location")),
        "early_applicant": score_data.get("earlyApplicant", False),
    }
    match_details = {
        "skill_mismatch": [s.strip() for s in skill_mismatch_str.split(",") if s.strip()],
    }
    return {"match_score": match_score, "match_details": match_details}


# ---------------------------------------------------------------------------
# Job detail assembly
# ---------------------------------------------------------------------------

def parse_job_detail(details_data: dict, job_id: str, page_url: str,
                     score_data: Optional[dict] = None) -> dict:
    """Parse job detail API response (v3 or v4 format) into a structured result dict."""
    from naukri_server.validation import validate_job_detail

    job = safe_get(details_data, "jobDetails", field_name="jobDetails", warn=True, default=details_data)

    salary_data = parse_salary_data(job)
    company_data = parse_company_data(job, details_data)
    match_data = parse_match_score(score_data)

    ctx = f"job_id={job_id}"
    is_applied = job.get("isApplied", False)
    external = bool(job.get("applyRedirectUrl"))

    exp_min = safe_get(job, "minimumExperience", field_name="experience_min", warn=True, context=ctx)
    exp_max = safe_get(job, "maximumExperience", field_name="experience_max", warn=True, context=ctx)

    result = {
        "status": "success",
        "job_id": job_id,
        "title": safe_get(job, "title", field_name="title", warn=True, context=ctx),
        "company": company_data["company_name"],
        "company_rating": company_data["company_rating"],
        "company_reviews_count": company_data["company_reviews_count"],
        "salary": salary_data["salary"],
        "experience": f"{exp_min or '?'}-{exp_max or '?'} years",
        "experience_min": exp_min,
        "experience_max": exp_max,
        "candidates_count": job.get("candidatesCount"),
        "location": safe_get(
            job, "cityName", "citySuburb", "location",
            field_name="location", warn=True, context=ctx,
        ) or extract_placeholder(job.get("placeholders", []), "location"),
        "description": safe_get(job, "description", field_name="description", warn=False, default=""),
        "skills": extract_skills(job.get("keySkills", [])),
        "match_score": match_data["match_score"],
        "match_details": match_data["match_details"],
        "is_applied": is_applied,
        "external_apply": external,
        "can_apply": not is_applied and not external,
        "vacancies": job.get("vacany"),
        "apply_count": job.get("applyCount"),
        "hr_name": safe_get(job, "contactPerson", "createdBy", field_name="hr_name", warn=False),
        "hr_email": job.get("contactEmail"),
        "external_apply_url": job.get("applyRedirectUrl"),
        "company_website": company_data["company_website"],
        "notice_period": job.get("noticePeriod"),
        "benefits": job.get("benefits"),
        "group_id": company_data["group_id"],
        "work_mode": normalize_work_mode(
            safe_get(job, "workMode", "wfhType", field_name="work_mode", warn=False)
        ) or work_mode_from_location(
            extract_placeholder(job.get("placeholders", []), "location")),
        "posted_date": safe_get(job, "createdDate", field_name="posted_date", warn=False),
        "industry": safe_get(
            job, "industryType",
            field_name="industry", warn=False,
        ) or (job.get("industryTypeGid", {}).get("label")
              if isinstance(job.get("industryTypeGid"), dict) else None),
        "role_category": job.get("roleCategory"),
        "education": format_education(job.get("education")) or job.get("qualification"),
        "job_role": job.get("jobRole"),
        "employment_type": job.get("employmentType"),
        "hybrid_detail": job.get("hybridWfhDetail"),
        "saved": bool(job.get("savedJobFlag")),
        "valid_through": safe_get(job, "validThrough", "expiryDate", field_name="valid_through", warn=False),
        "department": safe_get(job, "department", "functionalArea", field_name="department", warn=False),
        "url": page_url,
    }
    # AmbitionBox enrichment from top-level v4 response
    ab = details_data.get("ambitionBoxDetails", {})
    if ab:
        salaries = ab.get("salaries", {})
        if salaries:
            result["salary_benchmarks"] = {
                "average": salaries.get("averageSalary"),
                "min": salaries.get("minSalary"),
                "max": salaries.get("maxSalary"),
            }
        benefits_data = ab.get("benefits", {})
        if isinstance(benefits_data, dict) and benefits_data.get("benefitsList"):
            result["benefits_list"] = benefits_data["benefitsList"]
        reviews_list = ab.get("reviews", [])
        if reviews_list:
            result["top_reviews"] = [
                {"rating": r.get("rating"), "role": r.get("role"), "title": r.get("title")}
                for r in reviews_list[:3] if isinstance(r, dict)
            ]

    warnings = validate_job_detail(result)
    if warnings:
        result["warnings"] = warnings
    return result


# ---------------------------------------------------------------------------
# Search result builders
# ---------------------------------------------------------------------------

def build_search_result(
    data: dict, keywords: str, location: str | None, page_no: int, limit: int,
    filters: dict, search_path: str,
) -> dict:
    """Build a standardized search result dict from raw API data."""
    from naukri_server.tools.job_parsing import _parse_job_list
    from naukri_server.validation import validate_job_list

    jobs = _parse_job_list(safe_get(data, "jobDetails", field_name="jobDetails", warn=True, default=[]), limit)
    total = safe_get(data, "noOfJobs", "totalCount", field_name="total_jobs", warn=True, default=len(jobs))
    result = {
        "status": "success",
        "keywords": keywords,
        "location": location,
        "page": page_no,
        "total": total,
        "count": len(jobs),
        "has_more": (page_no * limit) < total if isinstance(total, int) else len(jobs) == limit,
        "filters": {k: v for k, v in filters.items() if v is not None},
        "jobs": jobs,
        "search_path": search_path,
    }
    if search_path == "browser":
        result["clusters"] = data.get("clusters", {})
    warnings = validate_job_list(jobs, safe_get(data, "noOfJobs", field_name="noOfJobs", warn=False), "search")
    if warnings:
        result["warnings"] = warnings
    return result


def build_recommendations_result(
    data: dict, page: int, limit: int,
) -> dict:
    """Build a standardized recommendations result dict from raw API data."""
    from naukri_server.tools.job_parsing import _parse_job_list
    from naukri_server.validation import validate_job_list

    job_details = safe_get(data, "jobDetails", field_name="jobDetails", warn=True, default=[])
    all_jobs = _parse_job_list(job_details, len(job_details))
    total = safe_get(data, "noOfJobs", field_name="total_jobs", warn=True, default=len(all_jobs))
    offset = (page - 1) * limit
    jobs = all_jobs[offset:offset + limit]
    raw_clusters = data.get("clusters") or data.get("recommendedClusters", {})
    cluster_info = {}
    if isinstance(raw_clusters, dict):
        for cluster_type, cluster_data in raw_clusters.items():
            if isinstance(cluster_data, dict):
                cluster_info[cluster_type] = {
                    "count": cluster_data.get("count", 0),
                    "title": cluster_data.get("title") or cluster_type,
                }
            elif isinstance(cluster_data, (int, float)):
                cluster_info[cluster_type] = {"count": int(cluster_data), "title": cluster_type}
    result = {
        "status": "success",
        "source": "recommendations",
        "total": total,
        "count": len(jobs),
        "page": page,
        "has_more": (offset + limit) < total,
        "jobs": jobs,
    }
    result["agent_eligible_exists"] = data.get("agentEligibleJobExists", False)
    result["cluster_split_date"] = data.get("clusterSplitDate")
    result["clusters"] = cluster_info
    warnings = validate_job_list(jobs, total, "recommendations")
    if warnings:
        result["warnings"] = warnings
    return result


def build_similar_jobs_result(
    data: dict, job_id: str, page: int, limit: int,
) -> dict:
    """Build a standardized similar-jobs result dict from raw API data."""
    from naukri_server.tools.job_parsing import _parse_job_list
    from naukri_server.validation import validate_job_list

    sim = safe_get(data, "simJobDetails", field_name="simJobDetails", warn=True, default={})
    job_details = sim.get("content", []) + sim.get("collaborative", [])
    all_jobs = _parse_job_list(job_details, len(job_details))
    total = safe_get(data, "noOfJobs", field_name="total_jobs", warn=True, default=len(all_jobs))
    offset = (page - 1) * limit
    jobs = all_jobs[offset:offset + limit]
    result = {
        "status": "success",
        "job_id": job_id,
        "source": "similar",
        "total": total,
        "count": len(jobs),
        "page": page,
        "has_more": (offset + limit) < total,
        "jobs": jobs,
    }
    warnings = validate_job_list(jobs, total, "similar_jobs")
    if warnings:
        result["warnings"] = warnings
    return result


# ---------------------------------------------------------------------------
# V1 job-detail parser — separated from I/O so it's purely testable.
# ---------------------------------------------------------------------------

_V1_WORK_MODE_MAP = WORK_MODE_CODES


def parse_job_detail_v1(data: dict) -> dict:
    """Parse the v1 job-detail API response into our flat result shape.

    The v1 endpoint exposes walk-in / contact / metrics fields not present in
    v3/v4. All field reads route through ``safe_get`` so the anti-corruption
    layer logs missing fields. Returns a dict with ``None`` values stripped.
    """
    job = safe_get(data, "job", default=data)
    if not isinstance(job, dict):
        job = {}

    job_id = safe_get(job, "jobId", field_name="jobId", warn=True, context="job_detail_v1")
    ctx = f"job_id={job_id}" if job_id else ""

    result = {
        "status": "success",
        "job_id": job_id,
        "title": safe_get(job, "post", field_name="title", warn=True, context=ctx),
        "company": safe_get(job, "companyName", field_name="company", warn=True, context=ctx),
        "company_id": safe_get(job, "companyId", field_name="company_id", warn=False),
        "group_id": safe_get(job, "groupId", field_name="group_id", warn=False),
        # Walk-in (v1-unique)
        "is_walk_in": safe_get(job, "isWalkIn", field_name="is_walk_in", warn=False, default=False),
        "walkin_time": safe_get(job, "walkinTime", field_name="walkin_time", warn=False),
        "walkin_venue": safe_get(job, "walkinVenue", field_name="walkin_venue", warn=False),
        "walkin_date_from": safe_get(job, "walkingDateFrom", field_name="walkin_date_from", warn=False),
        "walkin_date_to": safe_get(job, "walkingDateTo", field_name="walkin_date_to", warn=False),
        # Contact (v1-unique)
        "contact_name": safe_get(job, "contactName", field_name="contact_name", warn=False),
        "contact_email": safe_get(job, "email", field_name="contact_email", warn=False) or None,
        "contact_phone": safe_get(job, "tel", field_name="contact_phone", warn=False) or None,
        "contact_designation": safe_get(job, "CONTDESIG", field_name="contact_designation", warn=False),
        # Metrics (v1-unique)
        "jd_views": safe_get(job, "jdViews", field_name="jd_views", warn=False),
        "jd_applies": safe_get(job, "jdApplies", field_name="jd_applies", warn=False),
        "vacancy": safe_get(job, "noOfVacancy", field_name="vacancy", warn=False) or None,
        # Dates
        "closing_date": safe_get(job, "closingDate", field_name="closing_date", warn=False),
        "is_expired": safe_get(job, "isExpiredJob", field_name="is_expired", warn=False, default=False),
        "posted_date": safe_get(job, "addDate", field_name="posted_date", warn=False),
        # Salary
        "salary_min": safe_get(job, "minSal", field_name="salary_min", warn=False),
        "salary_max": safe_get(job, "maxSal", field_name="salary_max", warn=False),
        "salary_hidden": safe_get(job, "showSal", field_name="salary_hidden", warn=False) == "n",
        # Experience
        "experience_min": safe_get(job, "minExp", field_name="experience_min", warn=False),
        "experience_max": safe_get(job, "maxExp", field_name="experience_max", warn=False),
        # Location
        "location": safe_get(job, "city", field_name="location", warn=False),
        "work_mode": _V1_WORK_MODE_MAP.get(
            safe_get(job, "wfhType", field_name="wfhType", warn=False, default="0"),
            "unknown",
        ),
        # Content
        "description": safe_get(job, "jobDesc", field_name="description", warn=False),
        "company_profile": safe_get(job, "companyProfile", field_name="company_profile", warn=False) or None,
        "keywords": safe_get(job, "keywords", field_name="keywords", warn=False),
        "employment_type": safe_get(job, "employmentType", field_name="employment_type", warn=False),
        # Education
        "ug_course": safe_get(job, "ugCourse", field_name="ug_course", warn=False),
        "pg_course": safe_get(job, "pgCourse", field_name="pg_course", warn=False),
        # Internship (v1-unique)
        "internship_start": safe_get(job, "internshipStartDate", field_name="internship_start", warn=False),
        "internship_duration": safe_get(job, "internshipDuration", field_name="internship_duration", warn=False),
        "internship_ppo": safe_get(job, "internshipPpo", field_name="internship_ppo", warn=False),
        # Status
        "is_saved": bool(safe_get(job, "isSavedJob", field_name="is_saved", warn=False, default=0)),
        "is_consultant": safe_get(job, "cons", field_name="is_consultant", warn=False) == "y",
        "hiring_for": safe_get(job, "hiringFor", field_name="hiring_for", warn=False) or None,
    }

    # Strip None values (preserve False, 0, "")
    return {k: v for k, v in result.items() if v is not None}


# ---------------------------------------------------------------------------
# Similar-jobs REST parser (lightweight, used by jobs._get_similar_jobs_rest)
# ---------------------------------------------------------------------------

def parse_similar_jobs_rest(data: dict, job_id: str, limit: int) -> dict:
    """Parse the /jobapi/v2/search/simjobs/{id} REST response into our shape.

    Pure function — extracted from tools/jobs._get_similar_jobs_rest so the
    JSON-shape logic is testable in isolation.
    """
    sim = safe_get(data, "simJobDetails", field_name="simJobDetails", warn=False, default={})
    jobs_raw = sim.get("content", []) + sim.get("collaborative", [])
    jobs = []
    for j in jobs_raw:
        if not isinstance(j, dict):
            continue
        placeholders = j.get("placeholders", []) or []
        location = ""
        for ph in placeholders:
            if isinstance(ph, dict) and ph.get("type") == "location":
                location = ph.get("label", "")
                break
        if not location and placeholders and isinstance(placeholders[0], dict):
            location = placeholders[0].get("label", "")

        tags_raw = j.get("tagsAndSkills") or ""
        if isinstance(tags_raw, str):
            tags = [s.strip() for s in tags_raw.split(",") if s.strip()][:5]
        elif isinstance(tags_raw, list):
            tags = tags_raw[:5]
        else:
            tags = []

        jobs.append({
            "job_id": safe_get(j, "jobId", field_name="job_id", warn=False),
            "title": safe_get(j, "title", field_name="title", warn=False),
            "company": safe_get(j, "companyName", field_name="company", warn=False),
            "salary": safe_get(
                j.get("salaryDetail", {}) if isinstance(j.get("salaryDetail"), dict) else {},
                "label", field_name="salary", warn=False, default="Not Disclosed",
            ),
            "location": location,
            "experience": j.get("experienceText", ""),
            "tags": tags,
            "url": j.get("jdURL", ""),
        })
    return {
        "status": "success",
        "job_id": job_id,
        "source": "rest_api",
        "total": safe_get(data, "noOfJobs", field_name="total", warn=False, default=len(jobs)),
        "count": len(jobs[:limit]),
        "jobs": jobs[:limit],
    }


# ---------------------------------------------------------------------------
# Match-score REST flattener (used by tools/jobs._fetch_match_score)
# ---------------------------------------------------------------------------

def flatten_match_score_rest(score_data: dict) -> dict:
    """Flatten the /matchscore REST response into our normalized dict."""
    return {
        "education": safe_get(score_data, "education", field_name="education", warn=False),
        "functional_area": safe_get(score_data, "functionalArea", field_name="functional_area", warn=False),
        "key_skills": safe_get(score_data, "Keyskills", field_name="key_skills", warn=False),
        "work_experience": safe_get(score_data, "workExperience", field_name="work_experience", warn=False),
        "industry": safe_get(score_data, "industry", field_name="industry", warn=False),
        "location": safe_get(score_data, "location", field_name="location", warn=False),
        "early_applicant": safe_get(score_data, "earlyApplicant", field_name="early_applicant", warn=False),
    }
