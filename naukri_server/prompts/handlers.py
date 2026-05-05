"""@mcp.prompt() handler registrations.

Each prompt is a parameterized template that a client can render and submit
to an LLM. Handlers are PURE — they accept parameters and return a list of
message dicts. They never call APIs, touch the browser, or read the filesystem.

Test seam: each prompt is exercised by tests/test_prompts.py via the FastMCP
prompt manager (mcp._prompt_manager.get_prompt(...) → render(...)).
"""

from __future__ import annotations

from typing import Any

from naukri_server import mcp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_msg(text: str) -> dict[str, Any]:
    """Build a user-role message dict in the FastMCP-expected shape."""
    return {"role": "user", "content": text}


def _assistant_msg(text: str) -> dict[str, Any]:
    """Build an assistant-role message dict (used for few-shot examples)."""
    return {"role": "assistant", "content": text}


# ---------------------------------------------------------------------------
# 1. cover_letter — generate a cover letter for a specific job
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="naukri/cover_letter",
    title="Generate cover letter",
    description=(
        "Draft a tailored cover letter for a specific Naukri job. Caller passes "
        "either job_id or job_url plus optional candidate context (years_experience, "
        "highlight_skills). The prompt asks the LLM to fetch the job via "
        "naukri_get_job and then produce a 3-paragraph cover letter."
    ),
)
def cover_letter_prompt(
    job_id: str = "",
    job_url: str = "",
    years_experience: str = "",
    highlight_skills: str = "",
) -> list[dict[str, Any]]:
    """Prompt the LLM to draft a cover letter for the given job."""
    job_ref = f"job_id={job_id}" if job_id else f"job_url={job_url}"
    if not job_id and not job_url:
        job_ref = "(no job_id / job_url provided — ask the user)"

    context_lines = []
    if years_experience:
        context_lines.append(f"- Years of experience: {years_experience}")
    if highlight_skills:
        context_lines.append(f"- Skills to highlight: {highlight_skills}")
    context_block = "\n".join(context_lines) if context_lines else "- (no extra context provided)"

    body = (
        "Draft a cover letter for the following Naukri job.\n\n"
        f"Job reference: {job_ref}\n\n"
        "Steps:\n"
        "1. Call naukri_get_job to fetch the job description, required skills, and company.\n"
        "2. Call naukri_get_profile (or read the naukri://profile resource) for the candidate's background.\n"
        "3. Produce a 3-paragraph cover letter:\n"
        "   - Para 1: hook that ties the role to the candidate's strongest matching skill.\n"
        "   - Para 2: 1-2 concrete past achievements aligned with the job's responsibilities.\n"
        "   - Para 3: clear call-to-action and availability.\n\n"
        "Candidate context:\n"
        f"{context_block}\n\n"
        "Tone: confident but not boastful. Length: 200–300 words. No buzzwords."
    )
    return [_user_msg(body)]


# ---------------------------------------------------------------------------
# 2. interview_prep — interview prep questions for a company + role
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="naukri/interview_prep",
    title="Interview preparation questions",
    description=(
        "Generate a structured interview prep pack for a given company and role. "
        "The prompt instructs the LLM to fetch company intel + similar past interviews "
        "and return a categorized question set (technical, behavioral, role-specific)."
    ),
)
def interview_prep_prompt(
    company: str,
    role: str,
    years_experience: str = "",
) -> list[dict[str, Any]]:
    """Prompt the LLM to build an interview prep pack."""
    if not company or not role:
        return [_user_msg(
            "Cannot build interview prep: both `company` and `role` are required. "
            "Ask the user for the missing values."
        )]

    body = (
        f"Build an interview preparation pack for: {role} at {company}.\n\n"
        "Steps:\n"
        "1. Call naukri_company_intel(company=..., intel_type='interviews') to fetch real "
        "   interview experiences from AmbitionBox.\n"
        "2. Call naukri_company_intel(company=..., intel_type='reviews') for culture cues.\n"
        "3. (Optional) Call naukri_taxonomy or read naukri://taxonomy for canonical skill names.\n\n"
        "Output format:\n"
        "  ## Technical questions (5)\n"
        "  ## Behavioral / culture questions (5)\n"
        "  ## Role-specific questions (5)\n"
        "  ## Questions YOU should ask the interviewer (3)\n\n"
        f"Calibrate difficulty for ~{years_experience or 'mid-level'} experience. "
        "For each technical question, include a one-line hint about what a strong answer covers."
    )
    return [_user_msg(body)]


# ---------------------------------------------------------------------------
# 3. daily_brief_intro — opening for the morning daily brief
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="naukri/daily_brief_intro",
    title="Daily brief opening",
    description=(
        "Short intro paragraph that opens the user's morning daily brief. "
        "Caller passes the date (ISO-format, e.g. 2026-05-05). Used by the "
        "daily_brief tool / agents that need a friendly opener."
    ),
)
def daily_brief_intro_prompt(date: str = "") -> list[dict[str, Any]]:
    """Prompt the LLM to write a 2-3 sentence opener for today's brief."""
    date_clause = f"for {date}" if date else "for today"
    body = (
        f"Write a 2–3 sentence opening paragraph {date_clause} that introduces the user's "
        "Naukri daily brief.\n\n"
        "Constraints:\n"
        "- Friendly, professional tone (no emojis).\n"
        "- Reference the date naturally if provided.\n"
        "- Lead the reader into the rest of the brief (recommendations, stale apps, "
        "  recruiter activity, follow-ups).\n"
        "- Do NOT invent metrics — only the date is known here; the brief itself "
        "  appends the data.\n\n"
        "Return only the paragraph text — no headings or bullets."
    )
    return [_user_msg(body)]


# ---------------------------------------------------------------------------
# 4. recruiter_reply — draft a reply to a recruiter inbox message
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="naukri/recruiter_reply",
    title="Draft recruiter reply",
    description=(
        "Draft a reply to a recruiter inbox message. Caller passes the inbox "
        "thread_id and an `intent` (interested / not_interested / need_info / "
        "salary_negotiation). The LLM fetches the message via naukri_read_message."
    ),
)
def recruiter_reply_prompt(
    thread_id: str,
    intent: str = "interested",
) -> list[dict[str, Any]]:
    """Prompt the LLM to draft a recruiter reply with the given intent."""
    valid_intents = {"interested", "not_interested", "need_info", "salary_negotiation"}
    intent_norm = intent.strip().lower() if intent else "interested"
    if intent_norm not in valid_intents:
        return [_user_msg(
            f"Invalid intent '{intent}'. Use one of: {', '.join(sorted(valid_intents))}."
        )]

    if not thread_id:
        return [_user_msg(
            "Cannot draft reply: `thread_id` is required. Ask the user which inbox "
            "thread to reply to (use naukri_list_inbox to find one)."
        )]

    intent_guidance = {
        "interested": (
            "Express clear interest, ask about next steps (interview format, timeline), "
            "and offer 2–3 availability windows."
        ),
        "not_interested": (
            "Politely decline. Be brief and respectful. Leave the door open for future roles "
            "without committing to anything specific."
        ),
        "need_info": (
            "Ask 3 specific clarifying questions: role responsibilities / tech stack, "
            "team size & reporting structure, compensation range."
        ),
        "salary_negotiation": (
            "Acknowledge the offer / discussion, anchor on the candidate's expected CTC "
            "(call naukri_get_profile to read it), and ask for the company's range."
        ),
    }[intent_norm]

    body = (
        f"Draft a reply to the recruiter on inbox thread `{thread_id}`.\n\n"
        "Steps:\n"
        "1. Call naukri_read_message to load the thread context (subject, recruiter name, "
        "   any job details).\n"
        f"2. Reply with intent: **{intent_norm}**.\n\n"
        f"Guidance for this intent:\n{intent_guidance}\n\n"
        "Constraints:\n"
        "- Subject line: short, references the job/role.\n"
        "- Body: 4–6 sentences max.\n"
        "- Address the recruiter by name if available.\n"
        "- Sign off with the candidate's name (read from profile if not given).\n"
        "- No emojis, no exclamation marks beyond the greeting."
    )
    return [_user_msg(body)]


# ---------------------------------------------------------------------------
# 5. profile_summary — generate "About me" for the profile
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="naukri/profile_summary",
    title="Profile 'About me' summary",
    description=(
        "Generate the profile headline / 'About me' paragraph. Caller passes "
        "years_exp and a comma-separated skills list; the LLM uses those to craft "
        "a recruiter-facing summary."
    ),
)
def profile_summary_prompt(
    years_exp: str = "",
    skills: str = "",
    target_role: str = "",
) -> list[dict[str, Any]]:
    """Prompt the LLM to draft a profile summary."""
    years_clause = f"{years_exp} years" if years_exp else "(unspecified)"
    skills_clause = skills if skills else "(none provided — read from naukri://profile)"
    target_clause = f"\nTarget role: {target_role}" if target_role else ""

    body = (
        "Draft a Naukri profile 'About me' summary for the following candidate.\n\n"
        f"Years of experience: {years_clause}\n"
        f"Top skills: {skills_clause}{target_clause}\n\n"
        "Format:\n"
        "  Line 1: a one-line resume headline (≤80 chars) that recruiters scan first.\n"
        "  Line 2: blank.\n"
        "  Lines 3+: a 4–6 sentence paragraph covering — current role/level, top 2–3 "
        "  technical strengths with concrete impact, and what kind of role the candidate "
        "  is looking for next.\n\n"
        "Style:\n"
        "- First person, professional, no superlatives.\n"
        "- Use specific tech names (e.g. 'React 18, Node.js 20, Postgres') over vague terms.\n"
        "- Avoid clichés like 'passionate', 'rockstar', 'ninja'."
    )
    return [_user_msg(body)]


# ---------------------------------------------------------------------------
# 6. follow_up_email — follow-up email after a stale application
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="naukri/follow_up_email",
    title="Follow-up email for stale application",
    description=(
        "Draft a polite follow-up email for a stale job application. Caller passes "
        "job_id and days_since_applied. The prompt instructs the LLM to read the "
        "underlying application and write a concise 4-sentence nudge."
    ),
)
def follow_up_email_prompt(
    job_id: str,
    days_since_applied: str = "",
) -> list[dict[str, Any]]:
    """Prompt the LLM to draft a follow-up email for a stale application."""
    if not job_id:
        return [_user_msg(
            "Cannot draft follow-up: `job_id` is required. Use "
            "naukri_stale_applications to find a candidate, then pass its job_id."
        )]

    days_clause = (
        f"It has been {days_since_applied} days since the original application."
        if days_since_applied
        else "Compute the elapsed time from naukri_get_application's applied_at field."
    )

    body = (
        f"Draft a follow-up email for the stale application on job {job_id}.\n\n"
        "Steps:\n"
        "1. Call naukri_get_application(job_id) for title / company / current_status.\n"
        "2. (Optional) Call naukri_draft_follow_up(job_id) for a baseline draft.\n\n"
        f"Context: {days_clause}\n\n"
        "Output (3 fields, one per line, label-prefixed):\n"
        "  Subject: <a single-line subject referencing the role>\n"
        "  Body: <4 sentences max — restate interest, reference one detail from the JD, "
        "        offer to provide more info, polite close>\n"
        "  Suggested send time: <weekday morning IST>\n\n"
        "Tone: polite, brief, low-pressure. Do not apologize for following up."
    )
    return [_user_msg(body)]


# ---------------------------------------------------------------------------
# 7. screening_answer — template for answering a screening question
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="naukri/screening_answer",
    title="Answer a screening question",
    description=(
        "Help the user answer a Naukri screening question. Caller passes the "
        "question text, an optional question_type (text|choice|number|boolean), "
        "and any options (for choice questions). The LLM produces a single answer "
        "plus a one-line rationale."
    ),
)
def screening_answer_prompt(
    question: str,
    question_type: str = "text",
    options: str = "",
) -> list[dict[str, Any]]:
    """Prompt the LLM to answer a single screening question."""
    if not question:
        return [_user_msg(
            "Cannot answer: `question` is required. Pass the screening question text."
        )]

    qtype_norm = (question_type or "text").strip().lower()
    valid_types = {"text", "choice", "number", "boolean"}
    if qtype_norm not in valid_types:
        qtype_norm = "text"

    options_clause = ""
    if qtype_norm == "choice":
        if options:
            options_clause = f"\n\nAvailable options:\n  {options}"
        else:
            options_clause = "\n\n(Choice question but no options provided — ask the user.)"

    body = (
        f"A Naukri screening question needs an answer. Use the candidate's profile "
        f"(naukri_get_profile or the naukri://profile resource) for context.\n\n"
        f"Question: {question}\n"
        f"Question type: {qtype_norm}"
        f"{options_clause}\n\n"
        "Output format (exactly two labeled lines):\n"
        "  Answer: <the literal value to submit>\n"
        "  Rationale: <one sentence explaining why this answer fits the candidate>\n\n"
        "Rules:\n"
        "- For boolean questions, answer 'Yes' or 'No' only.\n"
        "- For number questions, return a bare number.\n"
        "- For choice questions, return EXACTLY one of the listed options (case-sensitive).\n"
        "- For text questions, keep the answer to ≤2 sentences.\n"
        "- Never fabricate experience the candidate doesn't have."
    )
    return [_user_msg(body)]


# ---------------------------------------------------------------------------
# Public list for tests — keeps registered prompt names in one discoverable place.
# ---------------------------------------------------------------------------

PROMPT_NAMES: tuple[str, ...] = (
    "naukri/cover_letter",
    "naukri/interview_prep",
    "naukri/daily_brief_intro",
    "naukri/recruiter_reply",
    "naukri/profile_summary",
    "naukri/follow_up_email",
    "naukri/screening_answer",
)
