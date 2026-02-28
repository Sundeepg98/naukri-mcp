"""Inbox tools — read recruiter messages and invitations."""

import re
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import logger, INBOX_API, MESSAGE_API


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


@mcp.tool()
async def naukri_get_inbox(
    limit: int = 20,
    unread_only: bool = False,
    mail_type: str = "",
    page: int = 1,
) -> dict:
    """List recruiter messages and invitations from your Naukri inbox.

    For reading a specific message, use naukri_read_message.

    Supports filtering by mail type — pass mail_type="powerNvite" to see only
    paid NVite invitations from recruiters.  Leave empty for all messages.

    Args:
        limit: Max messages to return (default 20)
        unread_only: If True, only return unread messages
        mail_type: Filter by message type (e.g. "powerNvite" for NVites only, "" for all)
        page: Page number for pagination (default 1)

    Returns:
        - {status: "success", total, unread, count, total_power_nvite, unread_power_nvite,
           messages: [{message_id, subject, sender, date, is_read, type, preview,
                       vcard_id, unique_id, power_nvite, is_relevant, is_applied,
                       applied_date, job_details, company_details}]}
        - {status: "error", message}
    """
    try:
        body = {
            "pageSize": limit,
            "pageNo": page,
            "mailType": mail_type,
            "isUnRead": False,
        }
        data = await api_post(INBOX_API, body=body)

        resp = data.get("successResponse") or data
        inbox_list = resp.get("inbox", [])
        total = resp.get("total", len(inbox_list))
        unread_count = resp.get("unread", 0)
        total_power_nvite = resp.get("totalPowerNvite", 0)
        unread_power_nvite = resp.get("unreadPowerNvite", 0)

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
                    "ctc_min": job_ctc.get("min"),
                    "ctc_max": job_ctc.get("max"),
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
                "preview": msg.get("preview") or msg.get("snippet", ""),
                "vcard_id": vcard_id,
                "unique_id": msg.get("uniqueId") or msg.get("uid", ""),
                "power_nvite": bool(msg.get("powerNvite", 0)),
                "is_relevant": bool(msg.get("isRelevant", 0)),
                "is_applied": bool(msg.get("isApplied", False)),
                "applied_date": msg.get("appliedDate", ""),
                "job_details": job_details,
                "company_details": company_details,
            })

            if len(messages) >= limit:
                break

        return {
            "status": "success",
            "total": total,
            "unread": unread_count,
            "total_power_nvite": total_power_nvite,
            "unread_power_nvite": unread_power_nvite,
            "count": len(messages),
            "messages": messages,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get inbox: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_read_message(message_id: str, vcard_id: str, unique_id: str) -> dict:
    """Read a specific message from your Naukri inbox.

    Requires: message_id, vcard_id, and unique_id from naukri_get_inbox results.

    Args:
        message_id: Message ID from inbox listing
        vcard_id: VCard ID from inbox listing
        unique_id: Unique ID from inbox listing

    Returns:
        - {status: "success", message_id, subject, content, date, type}
        - {status: "error", message}
    """
    try:
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
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to read message: {type(e).__name__}: {e}"}
