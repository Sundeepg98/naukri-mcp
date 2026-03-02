"""Shared job-list parsing helpers used by search and company tools."""

from naukri_server.config import NAUKRI_BASE, LAKHS_MULTIPLIER


def _parse_job_list(job_details: list, limit: int) -> list:
    """Parse Naukri's jobDetails array into a clean list of job dicts."""
    jobs = []
    for job in job_details[:limit]:
        salary = job.get("salaryDetail", {})
        sal_min = salary.get("minimumSalary", 0)
        sal_max = salary.get("maximumSalary", 0)
        sal_label = salary.get("label", "")
        salary_str = sal_label if sal_label else (
            f"{sal_min/LAKHS_MULTIPLIER:.1f}-{sal_max/LAKHS_MULTIPLIER:.1f} LPA" if sal_max else "Not Disclosed"
        )

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
            "salary": salary_str,
            "salary_min_lakhs": round(sal_min / LAKHS_MULTIPLIER, 1) if sal_min else None,
            "salary_max_lakhs": round(sal_max / LAKHS_MULTIPLIER, 1) if sal_max else None,
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
            "work_mode": job.get("workMode") or job.get("wfhType"),
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
        })
        # Structured salary detail from bulk fetch API — add raw fields if available
        if isinstance(salary, dict):
            jobs[-1]["salary_min_raw"] = salary.get("minimumSalary")
            jobs[-1]["salary_max_raw"] = salary.get("maximumSalary")
            jobs[-1]["salary_hidden"] = salary.get("hideSalary", False)
    return jobs
