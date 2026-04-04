"""Inbox tools — read recruiter messages and invitations."""

import re
from typing import Optional

from naukri_server import mcp
from naukri_server.api import NaukriAPIError
from naukri_server.interfaces import api_client
from naukri_server.config import logger, INBOX_API, MESSAGE_API, INBOX_MARK_INTERESTED_API, INBOX_REST_API
from naukri_server.domain import safe_get
from naukri_server.error_handler import handle_tool_action
from naukri_server.models import validate_action_params
from naukri_server.validation import validate_limit, validate_page


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


async def _fetch_inbox(
    limit: int = 20,
    unread_only: bool = False,
    mail_type: str = "",
    page: int = 1,
) -> dict:
    """Fetch inbox messages from the API and return structured result."""
    limit = validate_limit(limit)
    page = validate_page(page)

    # Try REST GET first (Tier 21 — simpler, richer response)
    try:
        params = {"pageSize": str(limit), "pageNo": str(page)}
        if mail_type:
            params["mailType"] = mail_type
        data = await api_client.get(INBOX_REST_API, params=params)
        resp = data
    except Exception:
        # Fallback to battle-tested POST endpoint
        body = {
            "pageSize": limit,
            "pageNo": page,
            "mailType": mail_type,
            "isUnRead": unread_only,
        }
        data = await api_client.post(INBOX_API, body=body)
        resp = data.get("successResponse") or data

    inbox_list = resp.get("inbox", [])
    total = resp.get("totalCount") or resp.get("total", len(inbox_list))
    unread_count = resp.get("unreadCount") or resp.get("unread", 0)
    total_power_nvite = resp.get("totalPowerNvite", 0)
    unread_power_nvite = resp.get("unreadPowerNvite", 0)
    relevant_count = resp.get("relevantCount", 0)
    has_power_nvites = resp.get("hasPowerNvites", False)

    messages = []
    for msg in inbox_list:
        is_read = bool(msg.get("isRead") or msg.get("read"))
        if unread_only and is_read:
            continue

        # Extract sender name and vcardId from sender array
        sender_raw = safe_get(msg, "sender", "senderName", default="", field_name="sender", warn=True, context="inbox")
        sender_name = ""
        vcard_id = safe_get(msg, "vcardId", "vCardId", default="", field_name="vcard_id", warn=True, context="inbox")
        if isinstance(sender_raw, list) and sender_raw:
            sender_name = safe_get(sender_raw[0], "name", default="", field_name="sender_name", warn=True, context="inbox")
            if not vcard_id:
                vcard_id = safe_get(sender_raw[0], "senderId", default="", field_name="sender_id", warn=True, context="inbox")
        elif isinstance(sender_raw, str):
            sender_name = sender_raw

        # Extract job details from NVite messages
        job_raw = safe_get(msg, "jobDetails", default={})
        job_details = None
        if job_raw:
            job_exp = safe_get(job_raw, "jobExperience", default={})
            job_ctc = safe_get(job_raw, "jobCtc", default={})
            apply_url = safe_get(job_raw, "applyUrlData", default={})
            job_details = {
                "job_title": safe_get(job_raw, "jobTitle", default="", field_name="job_title", warn=True, context="inbox.jobDetails"),
                "experience_min": safe_get(job_exp, "min", default=None),
                "experience_max": safe_get(job_exp, "max", default=None),
                "ctc_min": safe_get(job_ctc, "minimum", "min", default=None),
                "ctc_max": safe_get(job_ctc, "maximum", "max", default=None),
                "location": safe_get(job_raw, "jobLocation", default=""),
                "work_mode": safe_get(job_raw, "workMode", default=""),
                "key_skills": safe_get(job_raw, "jobKeySkills", default=""),
                "nvite_job_id": safe_get(apply_url, "jobId", default="", field_name="nvite_job_id", warn=True, context="inbox.applyUrlData"),
            }

        # Extract company details
        company_raw = safe_get(msg, "companyDetails", default={})
        company_details = None
        if company_raw:
            company_details = {
                "company_name": safe_get(company_raw, "companyName", default="", field_name="company_name", warn=True, context="inbox.companyDetails"),
                "ambition_box_rating": safe_get(company_raw, "ambitionBoxRating", default=None),
                "ambition_box_reviews": safe_get(company_raw, "ambitionBoxReviews", default=None),
            }

        messages.append({
            "message_id": safe_get(msg, "messageId", "id", field_name="message_id", warn=True, context="inbox"),
            "subject": safe_get(msg, "subject", "title", default="", field_name="subject", warn=True, context="inbox"),
            "sender": sender_name,
            "date": safe_get(msg, "dateTime", "date", default=""),
            "is_read": is_read,
            "type": safe_get(msg, "messageType", "type", default=""),
            "preview": safe_get(msg, "titleText", "preview", "snippet", default=""),
            "vcard_id": vcard_id,
            "unique_id": safe_get(msg, "uniqueId", "uid", default=""),
            "power_nvite": bool(msg.get("powerNvite", 0)),
            "is_relevant": bool(msg.get("isRelevant", 0)),
            "is_applied": bool(msg.get("isApplied", False)),
            "applied_date": msg.get("appliedDate", ""),
            "job_details": job_details,
            "company_details": company_details,
            "job_description": (msg.get("miscJobDetails") or {}).get("jobDesc"),
            "conversation_id": msg.get("conversationId"),
            "mail_id": msg.get("mailId"),
            "has_screening_questions": msg.get("questionairreAttached", False),
            "recruiter_name": (msg.get("vCardInfo") or {}).get("name"),
            "recruiter_designation": (msg.get("vCardInfo") or {}).get("designation"),
            "is_interested": msg.get("interested"),
            "salary": msg.get("salary"),
        })

        if len(messages) >= limit:
            break

    return {
        "status": "success",
        "total": total,
        "count": len(messages),
        "page": page,
        "has_more": (page * limit) < total,
        "unread": unread_count,
        "total_power_nvite": total_power_nvite,
        "unread_power_nvite": unread_power_nvite,
        "relevant_count": relevant_count,
        "has_power_nvites": has_power_nvites,
        "messages": messages,
    }


async def _read_message(message_id: str, vcard_id: str, unique_id: str) -> dict:
    """Fetch a single message's full content from the API."""
    data = await api_client.get(MESSAGE_API, params={
        "vcardId": vcard_id,
        "messageId": message_id,
        "uniqueId": unique_id,
    })

    mail = safe_get(data, "mail", default=data)
    raw_content = safe_get(mail, "titleText", "body", "content", default="", field_name="content", warn=True, context="read_message")
    content = _strip_html(raw_content) if "<" in raw_content else raw_content

    return {
        "status": "success",
        "message_id": message_id,
        "subject": safe_get(mail, "subject", default="", field_name="subject", warn=True, context="read_message"),
        "content": content,
        "date": safe_get(mail, "dateTime", "date", default=""),
        "type": safe_get(mail, "messageType", "type", default=""),
        "conversation_id": safe_get(mail, "conversationId", default=None),
    }


async def _mark_interested(mail_id: str, conversation_id: str, interested: bool) -> dict:
    """Mark a message as interested/not interested via the API."""
    await api_client.post(
        INBOX_MARK_INTERESTED_API,
        body={
            "mailId": str(mail_id),
            "conversationId": str(conversation_id),
            "interested": interested,
        },
    )
    action = "interested" if interested else "not interested"
    return {"status": "success", "mail_id": mail_id, "interested": interested, "message": f"Marked as {action}."}


async def _accept_nvite(nvite_job_id: str, answers: Optional[dict], title: Optional[str], company: Optional[str]) -> dict:
    """Accept a recruiter NVite by applying to the associated job."""
    from naukri_server.tools.apply import _apply_single

    return await _apply_single(
        job_id=nvite_job_id,
        answers=answers,
        title=title,
        company=company,
        tracking_extra={"source": "nvite"},
    )


# ---------------------------------------------------------------------------
# ISP param validation
# ---------------------------------------------------------------------------

_VALID_PARAMS_PER_ACTION = {
    "list": {"limit", "unread_only", "mail_type", "page"},
    "read": {"message_id", "vcard_id", "unique_id"},
    "mark_interested": {"mail_id", "conversation_id", "interested"},
    "accept_nvite": {"nvite_job_id", "answers", "title", "company"},
}


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_inbox(
    action: str = "list",
    message_id: Optional[str] = None,
    vcard_id: Optional[str] = None,
    unique_id: Optional[str] = None,
    mail_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    interested: bool = True,
    nvite_job_id: Optional[str] = None,
    answers: Optional[dict] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
    limit: int = 20,
    unread_only: bool = False,
    mail_type: str = "",
    page: int = 1,
) -> dict:
    """[Deprecated — use naukri_list_inbox, naukri_read_message, naukri_mark_interested, naukri_accept_nvite instead.]

    Unified inbox management — list messages, read, mark interest, accept NVites.

    Actions:
      - "list": Fetch inbox messages (use limit/page/unread_only/mail_type for filtering)
      - "read": Read a specific message (requires message_id, vcard_id, unique_id)
      - "mark_interested": Signal interest to recruiter (requires mail_id, conversation_id; optional interested)
      - "accept_nvite": Accept recruiter NVite by applying (requires nvite_job_id; optional answers, title, company)

    Args:
        action: "list" | "read" | "mark_interested" | "accept_nvite"
        message_id: Required for read — message ID from list results
        vcard_id: Required for read — VCard ID from list results
        unique_id: Required for read — unique ID from list results
        mail_id: Required for mark_interested — mail ID from list/read results
        conversation_id: Required for mark_interested — from read results
        interested: For mark_interested — True=interested, False=not interested (default True)
        nvite_job_id: Required for accept_nvite — from list → job_details.nvite_job_id
        answers: For accept_nvite — screening question answers {question_id: answer}
        title: For accept_nvite — job title for tracking
        company: For accept_nvite — company name for tracking
        limit: For list — max messages (default 20)
        unread_only: For list — only unread messages (default False)
        mail_type: For list — filter by type e.g. "powerNvite" (default "" for all)
        page: For list — page number (default 1)

    Returns:
        - list: {status, total, count, page, has_more, unread, total_power_nvite, unread_power_nvite, messages: [...]}
        - read: {status, message_id, subject, content, date, type, conversation_id}
        - mark_interested: {status, mail_id, interested, message}
        - accept_nvite: {status: "applied"|"needs_input"|"already_applied"|"error", ...}
        - {status: "error", message} on failure
    """
    # ── ISP: warn about params irrelevant to chosen action ─────────────
    _provided = {
        "message_id": message_id, "vcard_id": vcard_id, "unique_id": unique_id,
        "mail_id": mail_id, "conversation_id": conversation_id,
        "interested": interested if not interested else None,
        "nvite_job_id": nvite_job_id, "answers": answers,
        "title": title, "company": company,
        "limit": limit if limit != 20 else None,
        "unread_only": unread_only if unread_only else None,
        "mail_type": mail_type if mail_type else None,
        "page": page if page != 1 else None,
    }
    _unused = validate_action_params(action, _provided, _VALID_PARAMS_PER_ACTION)

    def _attach_unused(result: dict) -> dict:
        if _unused and isinstance(result, dict):
            result["unused_params"] = _unused
        return result

    # ── list ───────────────────────────────────────────────────────────
    if action == "list":
        return _attach_unused(await handle_tool_action(lambda: _fetch_inbox(limit=limit, unread_only=unread_only, mail_type=mail_type, page=page), "inbox.list"))

    # ── read ───────────────────────────────────────────────────────────
    elif action == "read":
        if not message_id or not vcard_id or not unique_id:
            return {"status": "error", "message": "read requires message_id, vcard_id, and unique_id.", "error_code": "VALIDATION_ERROR"}
        return _attach_unused(await handle_tool_action(lambda: _read_message(message_id, vcard_id, unique_id), "inbox.read"))

    # ── mark_interested ────────────────────────────────────────────────
    elif action == "mark_interested":
        if not mail_id or not conversation_id:
            return {"status": "error", "message": "mark_interested requires mail_id and conversation_id.", "error_code": "VALIDATION_ERROR"}
        return _attach_unused(await handle_tool_action(lambda: _mark_interested(mail_id, conversation_id, interested), "inbox.mark_interested"))

    # ── accept_nvite ──────────────────────────────────────────────────
    elif action == "accept_nvite":
        if not nvite_job_id:
            return {"status": "error", "message": "accept_nvite requires nvite_job_id.", "error_code": "VALIDATION_ERROR"}
        return _attach_unused(await handle_tool_action(lambda: _accept_nvite(nvite_job_id, answers, title, company), "inbox.accept_nvite"))

    # ── unknown action ─────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: list, read, mark_interested, accept_nvite", "error_code": "VALIDATION_ERROR"}


# ---------------------------------------------------------------------------
# Single-purpose MCP tools (preferred over deprecated naukri_inbox)
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_list_inbox(
    limit: int = 20,
    unread_only: bool = False,
    mail_type: Optional[str] = None,
    page: int = 1,
) -> dict:
    """List recruiter messages and NVite invitations from your Naukri inbox.

    Args:
        limit: Max messages per page (default 20, max 50)
        unread_only: Only return unread messages (default False)
        mail_type: Filter by type e.g. "powerNvite" (default None for all)
        page: Page number (default 1)

    Returns:
        {status, total, count, page, has_more, unread, total_power_nvite,
         unread_power_nvite, relevant_count, has_power_nvites, messages: [...]}
    """
    return await handle_tool_action(
        lambda: _fetch_inbox(limit=limit, unread_only=unread_only, mail_type=mail_type or "", page=page),
        "list_inbox",
    )


@mcp.tool()
async def naukri_read_message(
    message_id: Optional[str] = None,
    vcard_id: Optional[str] = None,
    unique_id: Optional[str] = None,
) -> dict:
    """Read full content of a specific inbox message.

    Args:
        message_id: Message ID from list results
        vcard_id: VCard ID from list results
        unique_id: Unique ID from list results

    Returns:
        {status, message_id, subject, content, date, type, conversation_id}
    """
    if not message_id or not vcard_id or not unique_id:
        return {"status": "error", "message": "message_id, vcard_id, and unique_id are all required.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _read_message(message_id, vcard_id, unique_id),
        "read_message",
    )


@mcp.tool()
async def naukri_mark_interested(
    mail_id: str,
    conversation_id: Optional[str] = None,
    interested: bool = True,
) -> dict:
    """Mark a recruiter message as interested or not interested.

    Args:
        mail_id: Mail ID from list/read results
        conversation_id: Conversation ID from read results
        interested: True=interested, False=not interested (default True)

    Returns:
        {status, mail_id, interested, message}
    """
    if not mail_id or not conversation_id:
        return {"status": "error", "message": "mail_id and conversation_id are required.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _mark_interested(mail_id, conversation_id, interested),
        "mark_interested",
    )


@mcp.tool()
async def naukri_accept_nvite(
    nvite_job_id: str,
    answers: Optional[dict] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
) -> dict:
    """Accept a recruiter NVite by applying to the associated job.

    Args:
        nvite_job_id: Job ID from list → job_details.nvite_job_id
        answers: Screening question answers {question_id: answer} (optional)
        title: Job title for tracking (optional)
        company: Company name for tracking (optional)

    Returns:
        {status: "applied"|"needs_input"|"already_applied"|"error", ...}
    """
    if not nvite_job_id:
        return {"status": "error", "message": "nvite_job_id is required.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _accept_nvite(nvite_job_id, answers, title, company),
        "accept_nvite",
    )
