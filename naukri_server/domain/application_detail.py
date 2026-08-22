"""ApplicationDetail — parsing and validation for application status API responses.

Consolidates the ~174 .get() calls from get_application_detail() into a single
factory method with structured logging for missing fields.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _parse_timeline(status_steps: list) -> list[dict]:
    """Parse status timeline entries from the history-description response.

    Every key this looked for was wrong. Captured live 2026-08-22, the API
    sends:

        {"statusId": 4, "appId": 107, "count": 6,
         "dateTime": "2026-08-21 15:21:26", "statusValue": "Application Viewed",
         "modifiedDate": "2026-08-21 15:36:49"}

    while the parser read `status`/`label`, `date`, `description`,
    `isCompleted`, `isCurrent`, `stepOrder`, `subStatus` -- none of which
    exist. So it emitted `[{"status": ""}, {"status": ""}, {"status": ""}]`:
    three well-formed rows carrying nothing, which is worse than an error
    because the shape looks right. On his InApp application it discarded a real
    three-step timeline whose last step was "Application Viewed", count 6.

    `count` is the interesting one and had no field at all -- it is how many
    times a recruiter opened the application.

    The legacy key names are kept as fallbacks: this parser also serves the
    v0/v3 shapes, and nothing here should regress if a route still sends them.
    """
    timeline = []
    for step in status_steps:
        entry = {
            "status": step.get("statusValue")
            or step.get("status")
            or step.get("label", "")
        }
        date = step.get("dateTime") or step.get("date")
        if date:
            entry["date"] = date
        if step.get("count") is not None:
            entry["count"] = step["count"]
        if step.get("statusId") is not None:
            entry["status_id"] = step["statusId"]
        if step.get("modifiedDate"):
            entry["modified_date"] = step["modifiedDate"]
        if step.get("description"):
            entry["description"] = step["description"]
        if step.get("isCompleted") is not None:
            entry["is_completed"] = step["isCompleted"]
        if step.get("isCurrent") is not None:
            entry["is_current"] = step["isCurrent"]
        if step.get("stepOrder") is not None:
            entry["step_order"] = step["stepOrder"]
        if step.get("subStatus"):
            entry["sub_status"] = step["subStatus"]
        timeline.append(entry)
    return timeline


def _parse_screening_questions(data: dict) -> list[dict]:
    """Parse screening questions from 3 fallback keys.

    Logs debug if no screening question data found.
    """
    raw = (
        data.get("screeningQuestions")
        or data.get("questionnaire")
        or data.get("screeningResponses")
        or []
    )
    if not raw:
        logger.debug("No screening question data found across 3 fallback keys")
    questions = []
    for sq in raw:
        q_entry = {
            "question": sq.get("question") or sq.get("questionText") or sq.get("title", ""),
            "answer": sq.get("answer") or sq.get("response") or sq.get("answerText"),
        }
        if sq.get("questionId"):
            q_entry["question_id"] = sq["questionId"]
        if sq.get("questionType"):
            q_entry["question_type"] = sq["questionType"]
        if sq.get("isMandatory") is not None:
            q_entry["is_mandatory"] = sq["isMandatory"]
        questions.append(q_entry)
    return questions


def _parse_recruiter(data: dict, job_details: dict) -> Optional[dict]:
    """Parse recruiter info from 4 fallback paths.

    Tries data.recruiterDetails, data.recruiter, jobDetails.recruiterDetails,
    jobDetails.recruiter in order. Logs warning if none found.
    """
    raw = (
        data.get("recruiterDetails")
        or data.get("recruiter")
        or job_details.get("recruiterDetails")
        or job_details.get("recruiter")
        or {}
    )
    if not isinstance(raw, dict) or not raw:
        logger.debug("No recruiter data found across 4 fallback keys")
        return None
    recruiter = {
        "name": raw.get("name") or raw.get("recruiterName"),
        "designation": raw.get("designation") or raw.get("title"),
        "company": raw.get("company") or raw.get("companyName"),
        "profile_url": raw.get("profileUrl") or raw.get("recruiterProfileUrl"),
        "image_url": raw.get("imageUrl") or raw.get("photoUrl"),
        "last_active": raw.get("lastActive") or raw.get("lastActiveDate"),
    }
    recruiter = {k: v for k, v in recruiter.items() if v is not None}
    return recruiter if recruiter else None


def _parse_company_rating(data: dict) -> Optional[dict]:
    """Parse company rating from companyRating or ambitionBoxData.

    Logs debug if no company rating found.
    """
    raw = data.get("companyRating") or data.get("ambitionBoxData")
    if not isinstance(raw, dict):
        logger.debug("No company rating data found (companyRating/ambitionBoxData)")
        return None
    return {
        "rating": raw.get("Rating") or raw.get("rating"),
        "reviews": raw.get("ReviewsCount") or raw.get("reviewsCount"),
    }


def from_api_response(data: dict, job_id: str) -> dict:
    """Parse the full application detail API response into a clean dict.

    This is the SINGLE place where all .get() calls for application detail
    parsing happen. Logs warnings for missing critical fields and debug
    messages for optional missing fields.

    Returns the same dict shape as the legacy get_application_detail().
    """
    job_details = data.get("jobDetails") or {}
    if not job_details:
        logger.warning("Application %s: jobDetails missing from API response", job_id)

    status_steps = data.get("status") or []
    if not status_steps:
        logger.warning("Application %s: status timeline missing from API response", job_id)

    timeline = _parse_timeline(status_steps)
    screening_questions = _parse_screening_questions(data)
    recruiter = _parse_recruiter(data, job_details)
    company_rating = _parse_company_rating(data)

    # --- Build result dict (same shape as legacy function) ---
    result = {
        "status": "success",
        "job_id": job_id,
        "title": job_details.get("jobTitle"),
        "company": job_details.get("company"),
        "location": job_details.get("location"),
        "is_open": job_details.get("isOpen"),
        "total_applicants": data.get("totalApplicants"),
        "recruiter_activity": job_details.get("jobActivity"),
        "recruiter_activity_date": job_details.get("jobActivityDate"),
        "match_rating": data.get("starRating"),
        "feedback_stored": data.get("feedbackStored"),
        "status_timeline": timeline,
        "matching_results": data.get("matchingResults"),
    }

    # Fields with fallback chains
    result["application_date"] = (
        data.get("applicationDate") or data.get("appliedDate") or data.get("applyDate")
    )
    result["current_status"] = (
        data.get("currentStatus") or data.get("applicationStatus")
    )
    result["apply_type"] = (
        data.get("applyType") or data.get("applicationMode")
    )
    result["job_url"] = (
        job_details.get("jobUrl") or job_details.get("url") or data.get("jobUrl")
    )
    result["salary_range"] = (
        job_details.get("salaryRange") or job_details.get("salary") or job_details.get("ctcRange")
    )
    result["experience_range"] = (
        job_details.get("experienceRange") or job_details.get("experience")
    )
    result["job_description_snippet"] = (
        job_details.get("jobDescription") or job_details.get("snippet")
    )
    result["job_type"] = (
        job_details.get("jobType") or job_details.get("employmentType")
    )
    result["industry"] = job_details.get("industry")
    result["functional_area"] = job_details.get("functionalArea")
    result["role_category"] = job_details.get("roleCategory")
    result["posted_date"] = (
        job_details.get("postedDate") or job_details.get("createdDate")
    )
    result["expiry_date"] = (
        job_details.get("expiryDate") or job_details.get("validTill")
    )

    # Conditional nested objects
    if screening_questions:
        result["screening_questions"] = screening_questions
    if recruiter:
        result["recruiter"] = recruiter

    # Additional top-level fields with fallbacks
    result["view_count"] = data.get("viewCount") or data.get("applicationViewCount")
    result["shortlisted"] = data.get("shortlisted") or data.get("isShortlisted")
    result["rejected"] = data.get("rejected") or data.get("isRejected")
    result["ars_score"] = data.get("arsScore") or data.get("ars")
    result["star_rating"] = data.get("starRating")
    result["apply_flow_type"] = data.get("applyFlowType")
    result["job_activity"] = data.get("jobActivity")
    result["job_activity_date"] = data.get("jobActivityDate")
    result["is_crawled"] = data.get("isCrawled")

    if company_rating:
        result["company_rating"] = company_rating

    result["apply_source"] = data.get("applySource") or data.get("source")
    result["resume_used"] = data.get("resumeUsed") or data.get("resumeName")
    result["cover_letter_used"] = data.get("coverLetterUsed") or data.get("hasCoverLetter")

    # Strip None values, then ensure status key is always present
    result = {k: v for k, v in result.items() if v is not None}
    result["status"] = "success"
    return result
