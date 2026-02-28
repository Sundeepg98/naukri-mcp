"""Response validators for detecting silent API/schema changes.

Each validator returns a list of warning strings (empty = clean).
Validators never raise exceptions or modify data.
"""

import logging

logger = logging.getLogger(__name__)


def validate_job_list(jobs: list, total_found: int, source: str) -> list[str]:
    """Validate a list of parsed jobs from search/recommendations/similar_jobs.

    Args:
        jobs: Parsed job dicts.
        total_found: Total count reported by the API.
        source: Label for warning messages ("search", "recommendations", "similar_jobs").
    """
    warnings: list[str] = []
    try:
        if total_found and total_found > 0 and not jobs:
            warnings.append(f"[{source}] API reports {total_found} jobs but parsed list is empty")
        for i, job in enumerate(jobs):
            missing = [f for f in ("job_id", "title", "company") if not job.get(f)]
            if missing:
                warnings.append(f"[{source}] job[{i}] missing fields: {', '.join(missing)}")
    except Exception as exc:
        logger.error("validate_job_list failed: %s", exc)
    for w in warnings:
        logger.warning(w)
    return warnings


def validate_company_list(companies: list, total_found: int) -> list[str]:
    """Validate a list of parsed companies from company search."""
    warnings: list[str] = []
    try:
        if total_found and total_found > 0 and not companies:
            warnings.append(f"API reports {total_found} companies but parsed list is empty")
        for i, company in enumerate(companies):
            missing = [f for f in ("group_id", "name") if not company.get(f)]
            if missing:
                warnings.append(f"company[{i}] missing fields: {', '.join(missing)}")
    except Exception as exc:
        logger.error("validate_company_list failed: %s", exc)
    for w in warnings:
        logger.warning(w)
    return warnings


def validate_profile(result: dict) -> list[str]:
    """Validate profile data returned by naukri_get_profile."""
    warnings: list[str] = []
    try:
        critical = [f for f in ("name", "total_experience", "current_location")
                    if result.get(f) is None]
        if critical:
            warnings.append(f"Profile missing critical fields: {', '.join(critical)}")
        skills = result.get("skills_with_experience", [])
        employment = result.get("employment", [])
        if not skills and not employment:
            warnings.append("Profile has both empty skills and empty employment history")
    except Exception as exc:
        logger.error("validate_profile failed: %s", exc)
    for w in warnings:
        logger.warning(w)
    return warnings


def validate_job_detail(result: dict) -> list[str]:
    """Validate a single job detail response from naukri_get_job."""
    warnings: list[str] = []
    try:
        empty = [f for f in ("title", "company", "description")
                 if not result.get(f)]
        if empty:
            warnings.append(f"Job detail missing fields: {', '.join(empty)}")
    except Exception as exc:
        logger.error("validate_job_detail failed: %s", exc)
    for w in warnings:
        logger.warning(w)
    return warnings


def validate_salary_data(result: dict) -> list[str]:
    """Validate salary data from AmbitionBox."""
    warnings: list[str] = []
    try:
        salaries = result.get("salaries", [])
        avg_salary = result.get("avg_salary")
        if not salaries and not avg_salary:
            warnings.append("No salary entries and no avg_salary — data may be missing")
        if salaries and all(not s.get("designation") for s in salaries):
            warnings.append("All salary entries have empty designation")
    except Exception as exc:
        logger.error("validate_salary_data failed: %s", exc)
    for w in warnings:
        logger.warning(w)
    return warnings


def validate_review_data(result: dict) -> list[str]:
    """Validate review data from AmbitionBox."""
    warnings: list[str] = []
    try:
        review_count = result.get("review_count")
        reviews = result.get("reviews", [])
        if review_count and review_count > 0 and not reviews:
            warnings.append(f"review_count is {review_count} but reviews list is empty")
        if reviews and all(not r.get("title") and not r.get("likes") for r in reviews):
            warnings.append("All reviews missing both title and likes")
    except Exception as exc:
        logger.error("validate_review_data failed: %s", exc)
    for w in warnings:
        logger.warning(w)
    return warnings
