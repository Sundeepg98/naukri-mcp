"""Resume tailor — job-specific profile optimization suggestions."""

import asyncio
import re
from typing import Optional

from naukri_server import mcp
from naukri_server.config import logger

# Common English stopwords to filter from keyword extraction
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "shall", "can", "need", "must", "not", "no", "nor", "so",
    "if", "then", "than", "that", "this", "these", "those", "it", "its",
    "we", "you", "he", "she", "they", "them", "their", "our", "your", "my",
    "who", "what", "which", "when", "where", "how", "why", "all", "each",
    "every", "any", "few", "more", "most", "other", "some", "such", "only",
    "own", "same", "also", "just", "about", "above", "after", "again", "as",
    "because", "before", "between", "both", "during", "into", "through",
    "up", "out", "over", "under", "very", "too", "here", "there",
    "work", "working", "experience", "year", "years", "role", "job", "team",
    "company", "ability", "strong", "good", "well", "etc", "including",
    "required", "preferred", "minimum", "maximum", "looking", "candidate",
    "responsible", "responsibilities", "requirements", "qualifications",
})


def _extract_keywords(text: str) -> set:
    """Extract meaningful keywords from text, filtering stopwords."""
    if not text:
        return set()
    # Strip HTML tags
    clean = re.sub(r'<[^>]+>', ' ', text)
    # Extract words (letters, digits, plus/sharp for C++/C#)
    words = re.findall(r'[a-zA-Z0-9#+]+', clean.lower())
    # Filter stopwords and very short words
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def _extract_phrases(text: str) -> set:
    """Extract multi-word technical phrases from text."""
    if not text:
        return set()
    clean = re.sub(r'<[^>]+>', ' ', text)
    # Common technical phrase patterns (2-3 words)
    phrases = set()
    # Look for capitalized multi-word terms
    for match in re.finditer(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+', clean):
        phrases.add(match.group(0).lower())
    # Look for tech terms with common patterns
    for match in re.finditer(r'(?:machine learning|deep learning|data science|data engineering|'
                              r'natural language processing|computer vision|cloud computing|'
                              r'full stack|front end|back end|devops|ci/cd|'
                              r'rest api|micro\s*services?|distributed systems?|'
                              r'big data|real time|event driven|'
                              r'problem solving|communication skills|'
                              r'agile|scrum|kanban|sprint)',
                              clean.lower()):
        phrases.add(match.group(0))
    return phrases


@mcp.tool()
async def naukri_resume_tailor(job_id: str) -> dict:
    """Get specific suggestions to tailor your profile for a job.

    Compares the job description, skills, and requirements against your current
    Naukri profile and provides actionable suggestions for headline, skills,
    and experience emphasis.

    Args:
        job_id: Naukri job ID or URL

    Returns:
        - {status: "success", job_title, company, suggestions: {headline,
           skills_to_add, skills_to_reorder, experience_emphasis, keyword_gaps}}
        - {status: "error", message}
    """
    from naukri_server.tools.jobs import naukri_get_job
    from naukri_server.tools.profile import naukri_get_profile

    # Parallel fetch
    job_result, profile_result = await asyncio.gather(
        naukri_get_job(job_id_or_url=job_id),
        naukri_get_profile(),
        return_exceptions=True,
    )

    if isinstance(job_result, Exception) or job_result.get("status") == "error":
        msg = str(job_result) if isinstance(job_result, Exception) else job_result.get("message")
        return {"status": "error", "message": f"Failed to fetch job: {msg}"}

    if isinstance(profile_result, Exception) or profile_result.get("status") == "error":
        msg = str(profile_result) if isinstance(profile_result, Exception) else profile_result.get("message")
        return {"status": "error", "message": f"Failed to fetch profile: {msg}"}

    # Extract job data
    job_title = job_result.get("title", "")
    job_company = job_result.get("company", "")
    job_desc = job_result.get("description", "")
    job_skills = [s for s in job_result.get("skills", []) if isinstance(s, str)]
    job_skills_lower = set(s.lower() for s in job_skills)

    # Extract profile data — key_skills may be comma-separated string or list
    raw_profile_skills = profile_result.get("key_skills", [])
    if isinstance(raw_profile_skills, str):
        profile_skills = [s.strip() for s in raw_profile_skills.split(",") if s.strip()]
    else:
        profile_skills = [s for s in raw_profile_skills if isinstance(s, str)]
    profile_skills_lower = set(s.lower() for s in profile_skills)
    headline = profile_result.get("resume_headline", "")
    employment = profile_result.get("employment", [])

    # --- Analysis ---

    # 1. Skills to add (in job but not in profile)
    skills_to_add = [s for s in job_skills if s.lower() not in profile_skills_lower]

    # 2. Skills to reorder (in profile but not near top, and required by job)
    skills_to_reorder = []
    if profile_skills:
        top_5 = set(s.lower() for s in profile_skills[:5])
        for s in job_skills:
            if s.lower() in profile_skills_lower and s.lower() not in top_5:
                # Find current position
                for i, ps in enumerate(profile_skills):
                    if ps.lower() == s.lower():
                        skills_to_reorder.append(f"Move '{ps}' higher (currently #{i+1})")
                        break

    # 3. Headline suggestion
    headline_lower = headline.lower() if headline else ""
    headline_missing = [s for s in job_skills[:5] if s.lower() not in headline_lower]
    headline_suggestion = None
    if headline_missing:
        # Suggest incorporating top missing skills
        headline_suggestion = f"Consider adding to headline: {', '.join(headline_missing[:3])}"

    # 4. Experience emphasis — find which employment entries are most relevant
    experience_emphasis = []
    job_keywords = _extract_keywords(job_desc)
    for emp in employment:
        emp_desc = emp.get("description", "") or ""
        emp_designation = emp.get("designation", "") or ""
        emp_company = emp.get("organization", "") or ""
        emp_keywords = _extract_keywords(emp_desc)
        overlap = job_keywords & emp_keywords
        if overlap and len(overlap) >= 3:
            experience_emphasis.append({
                "role": f"{emp_designation} at {emp_company}",
                "relevant_keywords": sorted(list(overlap))[:10],
                "suggestion": f"Emphasize {', '.join(sorted(list(overlap))[:5])} in this role's description",
            })

    # 5. Keyword gaps — terms in JD not found anywhere in profile
    profile_text = " ".join([
        headline or "",
        " ".join(profile_skills),
        " ".join(emp.get("description", "") or "" for emp in employment),
        " ".join(emp.get("designation", "") or "" for emp in employment),
    ]).lower()
    profile_keywords = _extract_keywords(profile_text)
    jd_phrases = _extract_phrases(job_desc)
    keyword_gaps = sorted(list((job_keywords - profile_keywords - _STOPWORDS) - job_skills_lower))
    # Filter to only meaningful gaps (3+ chars, appear significant)
    keyword_gaps = [k for k in keyword_gaps if len(k) >= 3][:20]

    # Also check for technical phrases missing
    phrase_gaps = sorted(list(jd_phrases - set(profile_text.split())))

    return {
        "status": "success",
        "job_title": job_title,
        "company": job_company,
        "suggestions": {
            "headline": headline_suggestion,
            "current_headline": headline,
            "skills_to_add": skills_to_add,
            "skills_to_reorder": skills_to_reorder,
            "experience_emphasis": experience_emphasis,
            "keyword_gaps": keyword_gaps,
            "phrase_gaps": phrase_gaps[:10],
        },
    }
