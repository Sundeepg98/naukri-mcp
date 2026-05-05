"""Tests for @mcp.prompt() handlers in naukri_server.prompts.handlers.

Pure unit tests — every prompt handler is itself pure (no I/O), so the only
fixtures we need are the FastMCP prompt manager. Each registered prompt has:

  1. A registration test — name present in the FastMCP prompt manager.
  2. A render test — render with sample args, verify message structure
     (role, content) and at least one expected substring in the body.
  3. (Where applicable) an edge-case test — missing required args, invalid
     enum values, etc.

The 7 prompts are also asserted as a group in test_all_seven_registered.
"""

import pytest

from naukri_server import mcp
# Import package to trigger @mcp.prompt() registration.
import naukri_server.prompts  # noqa: F401
from naukri_server.prompts.handlers import PROMPT_NAMES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registered_names() -> set[str]:
    """Return the set of registered prompt names."""
    return {p.name for p in mcp._prompt_manager.list_prompts()}


async def _render(name: str, **arguments) -> list:
    """Render a prompt and return the list[Message] result."""
    return await mcp._prompt_manager.render_prompt(name, arguments=arguments or None)


def _all_text(messages) -> str:
    """Concatenate every message's text content into a single searchable string.

    Each rendered Message has a .content ContentBlock; for our prompts this is
    always TextContent (we never returned resource/image blocks). Pulling .text
    keeps the assertion robust if the inner shape changes.
    """
    parts = []
    for m in messages:
        # m is a Message (from prompts/base.py) whose content is a ContentBlock.
        c = getattr(m, "content", None)
        if c is None:
            continue
        text = getattr(c, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


# ===========================================================================
# Group test — all 7 prompts registered
# ===========================================================================


def test_all_seven_registered():
    """All 7 expected prompts must be registered after package import."""
    registered = _registered_names()
    expected = set(PROMPT_NAMES)
    missing = expected - registered
    assert not missing, f"Missing prompts: {missing}. Registered: {sorted(registered)}"
    assert len(PROMPT_NAMES) == 7


def test_prompt_names_match_handlers():
    """The PROMPT_NAMES tuple must match what's actually registered for the package."""
    registered = _registered_names()
    for name in PROMPT_NAMES:
        assert name in registered, f"{name} listed in PROMPT_NAMES but not registered"


# ===========================================================================
# 1. naukri/cover_letter
# ===========================================================================


@pytest.mark.asyncio
async def test_cover_letter_with_job_id():
    msgs = await _render("naukri/cover_letter", job_id="270226007446",
                          years_experience="5", highlight_skills="Python, Django")
    assert len(msgs) == 1
    assert msgs[0].role == "user"
    text = _all_text(msgs)
    assert "270226007446" in text
    assert "naukri_get_job" in text
    assert "Python, Django" in text


@pytest.mark.asyncio
async def test_cover_letter_with_job_url_only():
    msgs = await _render("naukri/cover_letter", job_url="https://www.naukri.com/job/123")
    text = _all_text(msgs)
    assert "https://www.naukri.com/job/123" in text


@pytest.mark.asyncio
async def test_cover_letter_with_no_job_ref():
    """When neither job_id nor job_url is given, the prompt should ask for it."""
    msgs = await _render("naukri/cover_letter")
    text = _all_text(msgs)
    assert "no job_id / job_url provided" in text


# ===========================================================================
# 2. naukri/interview_prep
# ===========================================================================


@pytest.mark.asyncio
async def test_interview_prep_happy_path():
    msgs = await _render("naukri/interview_prep", company="Google",
                          role="Senior SDE", years_experience="6")
    assert len(msgs) == 1
    text = _all_text(msgs)
    assert "Google" in text
    assert "Senior SDE" in text
    assert "naukri_company_intel" in text
    assert "Technical questions" in text


@pytest.mark.asyncio
async def test_interview_prep_missing_company():
    msgs = await _render("naukri/interview_prep", company="", role="SDE")
    text = _all_text(msgs)
    assert "company" in text and "required" in text


@pytest.mark.asyncio
async def test_interview_prep_missing_role():
    msgs = await _render("naukri/interview_prep", company="Acme", role="")
    text = _all_text(msgs)
    assert "role" in text and "required" in text


# ===========================================================================
# 3. naukri/daily_brief_intro
# ===========================================================================


@pytest.mark.asyncio
async def test_daily_brief_intro_with_date():
    msgs = await _render("naukri/daily_brief_intro", date="2026-05-05")
    text = _all_text(msgs)
    assert "2026-05-05" in text
    assert "2–3 sentence" in text or "2-3 sentence" in text


@pytest.mark.asyncio
async def test_daily_brief_intro_without_date():
    msgs = await _render("naukri/daily_brief_intro")
    text = _all_text(msgs)
    assert "for today" in text


# ===========================================================================
# 4. naukri/recruiter_reply
# ===========================================================================


@pytest.mark.asyncio
async def test_recruiter_reply_interested():
    msgs = await _render("naukri/recruiter_reply", thread_id="THR-1", intent="interested")
    text = _all_text(msgs)
    assert "THR-1" in text
    assert "interested" in text
    assert "naukri_read_message" in text


@pytest.mark.asyncio
async def test_recruiter_reply_invalid_intent():
    msgs = await _render("naukri/recruiter_reply", thread_id="THR-2", intent="ghost")
    text = _all_text(msgs)
    assert "Invalid intent" in text


@pytest.mark.asyncio
async def test_recruiter_reply_missing_thread():
    msgs = await _render("naukri/recruiter_reply", thread_id="", intent="interested")
    text = _all_text(msgs)
    assert "thread_id" in text and "required" in text


@pytest.mark.asyncio
async def test_recruiter_reply_salary_negotiation():
    msgs = await _render("naukri/recruiter_reply", thread_id="T9",
                          intent="salary_negotiation")
    text = _all_text(msgs)
    assert "salary_negotiation" in text
    assert "expected CTC" in text


# ===========================================================================
# 5. naukri/profile_summary
# ===========================================================================


@pytest.mark.asyncio
async def test_profile_summary_with_full_args():
    msgs = await _render("naukri/profile_summary", years_exp="7",
                          skills="Python, AWS, Kubernetes", target_role="Staff Engineer")
    text = _all_text(msgs)
    assert "7 years" in text
    assert "Python, AWS, Kubernetes" in text
    assert "Staff Engineer" in text


@pytest.mark.asyncio
async def test_profile_summary_minimal():
    msgs = await _render("naukri/profile_summary")
    text = _all_text(msgs)
    assert "(unspecified)" in text
    assert "naukri://profile" in text


# ===========================================================================
# 6. naukri/follow_up_email
# ===========================================================================


@pytest.mark.asyncio
async def test_follow_up_email_with_days():
    msgs = await _render("naukri/follow_up_email", job_id="J999",
                          days_since_applied="14")
    text = _all_text(msgs)
    assert "J999" in text
    assert "14 days" in text
    assert "naukri_get_application" in text


@pytest.mark.asyncio
async def test_follow_up_email_without_days():
    msgs = await _render("naukri/follow_up_email", job_id="J100")
    text = _all_text(msgs)
    assert "J100" in text
    assert "applied_at" in text


@pytest.mark.asyncio
async def test_follow_up_email_missing_job_id():
    msgs = await _render("naukri/follow_up_email", job_id="")
    text = _all_text(msgs)
    assert "job_id" in text and "required" in text


# ===========================================================================
# 7. naukri/screening_answer
# ===========================================================================


@pytest.mark.asyncio
async def test_screening_answer_text_question():
    msgs = await _render("naukri/screening_answer",
                          question="Why are you looking to switch?",
                          question_type="text")
    text = _all_text(msgs)
    assert "Why are you looking to switch?" in text
    assert "Question type: text" in text


@pytest.mark.asyncio
async def test_screening_answer_choice_question_with_options():
    msgs = await _render("naukri/screening_answer",
                          question="Notice period?",
                          question_type="choice",
                          options="Immediate, 15 days, 30 days, 60+ days")
    text = _all_text(msgs)
    assert "Available options" in text
    assert "Immediate, 15 days" in text


@pytest.mark.asyncio
async def test_screening_answer_choice_no_options():
    msgs = await _render("naukri/screening_answer",
                          question="Pick one",
                          question_type="choice")
    text = _all_text(msgs)
    assert "no options provided" in text


@pytest.mark.asyncio
async def test_screening_answer_unknown_type_falls_back_to_text():
    msgs = await _render("naukri/screening_answer",
                          question="Anything?", question_type="bogus_type")
    text = _all_text(msgs)
    assert "Question type: text" in text


@pytest.mark.asyncio
async def test_screening_answer_missing_question():
    msgs = await _render("naukri/screening_answer", question="")
    text = _all_text(msgs)
    assert "question" in text and "required" in text
