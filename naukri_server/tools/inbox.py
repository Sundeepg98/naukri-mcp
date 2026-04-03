"""Inbox tools — read recruiter messages and invitations."""

import re
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import logger, INBOX_API, MESSAGE_API, INBOX_MARK_INTERESTED_API, INBOX_REST_API
from naukri_server.error_handler import handle_tool_action
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
        data = await api_get(INBOX_REST_API, params=params)
        resp = data
    except Exception:
        # Fallback to battle-tested POST endpoint
        body = {
            "pageSize": limit,
            "pageNo": page,
            "mailType": mail_type,
            "isUnRead": unread_only,
        }
        data = await api_post(INBOX_API, body=body)
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
        sender_raw = msg.get("sender") or msg.get("senderName") or ""
        sender_name = ""
        vcard_id = msg.get("vcardId") or msg.get("vCardId") or ""
        if isinstance(sender_raw, list) and sender_raw:
            sender_name = sender_raw[0].get("name", "")
            if not vcard_id:
                vcard_id = sender_raw[0].get("senderId", "")
        elif isinstance(sender_raw, str):
            sender_name = sender_raw

        # Extract job details from NVite messages
        job_raw = msg.get("jobDetails") or {}
        job_details = None
        if job_raw:
            job_exp = job_raw.get("jobExperience") or {}
            job_ctc = job_raw.get("jobCtc") or {}
            apply_url = job_raw.get("applyUrlData") or {}
            job_details = {
                "job_title": job_raw.get("jobTitle", ""),
                "experience_min": job_exp.get("min"),
                "experience_max": job_exp.get("max"),
                "ctc_min": job_ctc.get("minimum") or job_ctc.get("min"),
                "ctc_max": job_ctc.get("maximum") or job_ctc.get("max"),
                "location": job_raw.get("jobLocation", ""),
                "work_mode": job_raw.get("workMode", ""),
                "key_skills": job_raw.get("jobKeySkills", ""),
                "nvite_job_id": apply_url.get("jobId", ""),
            }

        # Extract company details
        company_raw = msg.get("companyDetails") or {}
        company_details = None
        if company_raw:
            company_details = {
                "company_name": company_raw.get("companyName", ""),
                "ambition_box_rating": company_raw.get("ambitionBoxRating"),
                "ambition_box_reviews": company_raw.get("ambitionBoxReviews"),
            }

        messages.append({
            "message_id": msg.get("messageId") or msg.get("id"),
            "subject": msg.get("subject") or msg.get("title", ""),
            "sender": sender_name,
            "date": msg.get("dateTime") or msg.get("date", ""),
            "is_read": is_read,
            "type": msg.get("messageType") or msg.get("type", ""),
            "preview": msg.get("titleText") or msg.get("preview") or msg.get("snippet", ""),
            "vcard_id": vcard_id,
            "unique_id": msg.get("uniqueId") or msg.get("uid", ""),
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
    data = await api_get(MESSAGE_API, params={
        "vcardId": vcard_id,
        "messageId": message_id,
        "uniqueId": unique_id,
    })

    mail = data.get("mail") or data
    raw_content = mail.get("titleText") or mail.get("body") or mail.get("content", "")
    content = _strip_html(raw_content) if "<" in raw_content else raw_content

    return {
        "status": "success",
        "message_id": message_id,
        "subject": mail.get("subject", ""),
        "content": content,
        "date": mail.get("dateTime") or mail.get("date", ""),
        "type": mail.get("messageType") or mail.get("type", ""),
        "conversation_id": mail.get("conversationId"),
    }


async def _mark_interested(mail_id: str, conversation_id: str, interested: bool) -> dict:
    """Mark a message as interested/not interested via the API."""
    await api_post(
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
    """Unified inbox management — list messages, read, mark interest, accept NVites.

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
    # ── list ───────────────────────────────────────────────────────────
    if action == "list":
        return await handle_tool_action(lambda: _fetch_inbox(limit=limit, unread_only=unread_only, mail_type=mail_type, page=page), "inbox.list")

    # ── read ───────────────────────────────────────────────────────────
    elif action == "read":
        if not message_id or not vcard_id or not unique_id:
            return {"status": "error", "message": "read requires message_id, vcard_id, and unique_id.", "error_code": "VALIDATION_ERROR"}
        return await handle_tool_action(lambda: _read_message(message_id, vcard_id, unique_id), "inbox.read")

    # ── mark_interested ────────────────────────────────────────────────
    elif action == "mark_interested":
        if not mail_id or not conversation_id:
            return {"status": "error", "message": "mark_interested requires mail_id and conversation_id.", "error_code": "VALIDATION_ERROR"}
        return await handle_tool_action(lambda: _mark_interested(mail_id, conversation_id, interested), "inbox.mark_interested")

    # ── accept_nvite ──────────────────────────────────────────────────
    elif action == "accept_nvite":
        if not nvite_job_id:
            return {"status": "error", "message": "accept_nvite requires nvite_job_id.", "error_code": "VALIDATION_ERROR"}
        return await handle_tool_action(lambda: _accept_nvite(nvite_job_id, answers, title, company), "inbox.accept_nvite")

    # ── unknown action ─────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: list, read, mark_interested, accept_nvite", "error_code": "VALIDATION_ERROR"}
