"""Inbox tools — read recruiter messages and invitations."""

import re
from typing import Optional

from naukri_server import mcp
from naukri_server.api import NaukriAPIError
from naukri_server.interfaces import api_client
from naukri_server.config import logger, INBOX_API, MESSAGE_API, INBOX_MARK_INTERESTED_API, INBOX_REST_API
from naukri_server.domain import safe_get
from naukri_server.error_handler import handle_tool_action
from naukri_server.validation import validate_limit, validate_page


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


#: How much of the recruiter's opening line a list row carries. Long enough to
#: tell two pitches apart, short enough that 62 rows stay one screen.
PREVIEW_CHARS = 240


def _misc_job_details(msg: dict, job_raw: dict) -> dict:
    """`miscJobDetails` from wherever this response shape put it.

    The two inbox endpoints disagree about nesting and the server uses both.
    The REST GET (`INBOX_REST_API`, the preferred path) nests it under
    `jobDetails`; the nc-services POST template (`INBOX_API`, the fallback)
    returns a flatter row. Reading only the flat position is why
    `job_description`, `salary` and `has_screening_questions` were null on
    every row of a REST response -- measured 2026-08-23 against the live
    account, 20 of 20 rows.
    """
    nested = safe_get(job_raw, "miscJobDetails", default={})
    if isinstance(nested, dict) and nested:
        return nested
    flat = safe_get(msg, "miscJobDetails", default={})
    return flat if isinstance(flat, dict) else {}


#: Naukri appends an internal requisition tag to the recruiter's display name,
#: e.g. "Example Recruiter_TAG_100200". It is theirs, not part of the person's
#: name. (Illustrative value: a real recruiter's name and requisition id stood
#: here until 2026-08-24 and were removed before this repo went public.)
_RECRUITER_TAG_RE = re.compile(r"_TAG_\d+\s*$")


def _recruiter_name(msg: dict) -> Optional[str]:
    """The recruiter's name, from the key that actually holds it.

    `vCardInfo.vname`. The code read `.get("name")`, which does not exist in
    that block, so `recruiter_name` was null on every row and the capability
    looked like a platform limitation rather than a typo. Measured 2026-08-23:
    19 of 20 live rows carry a `vname`.

    Returns None rather than "" when there is genuinely no name, so a caller
    can tell "this recruiter is anonymous" from "we failed to read it".
    """
    vcard = msg.get("vCardInfo") or {}
    raw = vcard.get("vname") or vcard.get("name")
    if not raw or not isinstance(raw, str):
        return None
    cleaned = _RECRUITER_TAG_RE.sub("", raw).strip()
    return cleaned or None


def _derive_preview(msg: dict, misc: dict) -> str:
    """The best one-line summary this row can honestly offer.

    `titleText` is what the nc-services POST template calls it, and the code
    used to read only that -- so on a REST response, which has no such key,
    `preview` was the empty string on every row. Choosing which of 62 messages
    to answer then cost 62 individual reads, which is the whole complaint.

    The REST shape does carry the recruiter's own opening sentence, as
    `miscJobDetails.dynamicContentLine`. That is strictly better than a
    truncated job description: it is what this recruiter chose to say, so two
    pitches for the same role read differently. `jobDesc` is the fallback when
    the opener is absent, and an empty string is returned rather than an
    invented summary when neither exists.
    """
    for candidate in (
        safe_get(msg, "titleText", "preview", "snippet", default=""),
        misc.get("dynamicContentLine"),
        misc.get("jobDesc"),
    ):
        if not candidate or not isinstance(candidate, str):
            continue
        text = _strip_html(candidate) if "<" in candidate else candidate.strip()
        if text:
            return text[:PREVIEW_CHARS]
    return ""


#: Naukri's inbox endpoints reject a page size above 20. Measured against the
#: live account 2026-08-24: pageSize=25 answers HTTP 424 "pageSize should be in
#: between 1-20", and the nc-services POST fallback answers the same way, so a
#: caller asking for 25 got the raw 424 instead of 25 messages.
#:
#: The cap lives HERE, at the call site, rather than as a new default inside
#: `validate_limit`: that helper is shared with endpoints which legitimately
#: serve 50 rows a page, and moving 20 into it would silently halve them. 20 is
#: THIS endpoint's stated bound, so this endpoint is where it belongs.
INBOX_MAX_PAGE_SIZE = 20

#: The unread walk's hard ceiling, in HTTP round trips per call. At 20 rows a
#: page it reaches 200 messages -- three times the 66 the live account holds --
#: so it binds a runaway and never a real inbox. On reaching it the walk STOPS
#: and reports the shortfall in `not_listed` rather than spinning.
INBOX_MAX_PAGES_WALKED = 10


async def _fetch_inbox_page(
    page_size: int,
    page: int,
    mail_type: str,
    unread_only: bool,
) -> dict:
    """One inbox page: REST GET first, the nc-services POST as the fallback.

    THE REST GET HAS NO UNREAD FILTER, so `unread_only` is not sent on it.
    Probed against the live endpoint 2026-08-24, baseline pageSize=20&pageNo=1
    (20 rows, 3 of them unread, unreadCount 11, total 66): `isUnread`,
    `unreadOnly`, `unread`, `readStatus` and `isRead` each returned 200 with
    the byte-same mixed page, and `isUnRead` returned 404. There is no
    parameter to pass, which is why the caller walks pages instead.

    The POST fallback DOES filter server-side and still gets `isUnRead`; that
    path's behaviour is unchanged.
    """
    try:
        params = {"pageSize": str(page_size), "pageNo": str(page)}
        if mail_type:
            params["mailType"] = mail_type
        return await api_client.get(INBOX_REST_API, params=params)
    except Exception:
        body = {
            "pageSize": page_size,
            "pageNo": page,
            "mailType": mail_type,
            "isUnRead": unread_only,
        }
        data = await api_client.post(INBOX_API, body=body)
        return data.get("successResponse") or data


def _parse_inbox_row(msg: dict) -> dict:
    """Read one raw inbox row into the shape every inbox tool returns."""
    is_read = bool(msg.get("isRead") or msg.get("read"))

    # Extract sender name and vcardId from sender array
    sender_raw = safe_get(msg, "sender", "senderName", default="", field_name="sender", warn=True, context="inbox")
    sender_name = ""
    # warn=False deliberately: the REST shape has NEITHER key -- it carries
    # the id as sender[0].senderId, read just below -- so warning here fired
    # once per message on every successful call, 62 lines of "missing" for
    # a field that was then found. A warning that fires on the healthy path
    # trains the reader to ignore the log.
    vcard_id = safe_get(msg, "vcardId", "vCardId", default="", field_name="vcard_id", warn=False, context="inbox")
    if isinstance(sender_raw, list) and sender_raw:
        sender_name = safe_get(sender_raw[0], "name", default="", field_name="sender_name", warn=True, context="inbox")
        if not vcard_id:
            vcard_id = safe_get(sender_raw[0], "senderId", default="", field_name="sender_id", warn=True, context="inbox")
    elif isinstance(sender_raw, str):
        sender_name = sender_raw

    # Extract job details from NVite messages
    job_raw = safe_get(msg, "jobDetails", default={})
    misc = _misc_job_details(msg, job_raw if isinstance(job_raw, dict) else {})
    job_details = None
    if job_raw:
        job_exp = safe_get(job_raw, "jobExperience", default={})
        job_ctc = safe_get(job_raw, "jobCtc", default={})
        apply_url = safe_get(job_raw, "applyUrlData", default={})
        job_details = {
            "job_title": safe_get(job_raw, "jobTitle", default="", field_name="job_title", warn=True, context="inbox.jobDetails"),
            # `minimum`/`maximum` FIRST. The experience block uses the same
            # long key names as `jobCtc` beside it, but this pair read only
            # the short form and so reported null on every live row while
            # ctc, one line down, worked. Short form kept as the fallback:
            # the nc-services template is a different shape.
            "experience_min": safe_get(job_exp, "minimum", "min", default=None),
            "experience_max": safe_get(job_exp, "maximum", "max", default=None),
            "ctc_min": safe_get(job_ctc, "minimum", "min", default=None),
            "ctc_max": safe_get(job_ctc, "maximum", "max", default=None),
            "location": safe_get(job_raw, "jobLocation", default=""),
            "work_mode": safe_get(job_raw, "workMode", default=""),
            "key_skills": safe_get(job_raw, "jobKeySkills", default=""),
            "nvite_job_id": safe_get(apply_url, "jobId", default="", field_name="nvite_job_id", warn=True, context="inbox.applyUrlData"),
            # Who is actually hiring. On a consultancy posting the sender
            # is the agency and this is the end client -- the distinction
            # decides whether a reply reaches the employer at all.
            "consultant": bool(misc.get("consultant")),
            "consultant_name": misc.get("consultantName") or "",
            "client_name": misc.get("clientName") or "",
        }

    # Extract company details
    company_raw = safe_get(msg, "companyDetails", default={})
    company_details = None
    if company_raw:
        company_details = {
            "company_name": safe_get(company_raw, "companyName", default="", field_name="company_name", warn=True, context="inbox.companyDetails"),
            "ambition_box_rating": safe_get(company_raw, "ambitionBoxRating", default=None),
            "ambition_box_reviews": safe_get(company_raw, "ambitionBoxReviews", default=None),
            # The payload carries the review URL and it was being dropped;
            # a 3.3 from 44858 reviews is worth reading before answering.
            "ambition_box_url": safe_get(company_raw, "ambitionBoxUrl", default=""),
        }

    return {
        "message_id": safe_get(msg, "messageId", "id", field_name="message_id", warn=True, context="inbox"),
        "subject": safe_get(msg, "subject", "title", default="", field_name="subject", warn=True, context="inbox"),
        "sender": sender_name,
        "date": safe_get(msg, "dateTime", "date", default=""),
        "is_read": is_read,
        "type": safe_get(msg, "messageType", "type", default=""),
        "preview": _derive_preview(msg, misc),
        "vcard_id": vcard_id,
        "unique_id": safe_get(msg, "uniqueId", "uid", default=""),
        "power_nvite": bool(msg.get("powerNvite", 0)),
        "is_relevant": bool(msg.get("isRelevant", 0)),
        "is_applied": bool(msg.get("isApplied", False)),
        "applied_date": msg.get("appliedDate", ""),
        "job_details": job_details,
        "company_details": company_details,
        "job_description": misc.get("jobDesc"),
        "conversation_id": msg.get("conversationId"),
        "mail_id": msg.get("mailId"),
        # `questionairreAttached` (the platform's spelling) lives under
        # jobDetails on the REST shape; read flat it was False on every
        # row, which silently understated how many of these need answers.
        "has_screening_questions": bool(
            safe_get(job_raw, "questionairreAttached", default=None)
            if isinstance(job_raw, dict) else None
        ) or bool(msg.get("questionairreAttached", False)),
        # `vname`, not `name`. The vCard block genuinely carries the
        # recruiter's name -- 19 of 20 live rows had one -- and reading the
        # wrong key is the entire reason this looked unavailable.
        "recruiter_name": _recruiter_name(msg),
        # Left as-is deliberately: `designation` IS the right key and it is
        # null on every live row. Naukri does not populate it, so this
        # stays null rather than being backfilled from something adjacent.
        "recruiter_designation": (msg.get("vCardInfo") or {}).get("designation"),
        "is_interested": msg.get("interested"),
        "salary": misc.get("salary") or msg.get("salary"),
    }


def _as_count(value, fallback: int = 0) -> int:
    """An API count as an int, or `fallback`. `True` is not a count."""
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


async def _fetch_inbox(
    limit: int = 20,
    unread_only: bool = False,
    mail_type: str = "",
    page: int = 1,
) -> dict:
    """Fetch inbox messages from the API and return structured result.

    ROWS AND PAGES ARE TWO DIFFERENT MEASUREMENTS, and conflating them broke
    this helper twice over. `limit` is how many rows the CALLER wants;
    `pageSize` is how many the ENDPOINT will put in one response, and it is
    capped at `INBOX_MAX_PAGE_SIZE` no matter what the caller asked. Passing
    the caller's number straight to the wire is why `limit=25` returned a raw
    HTTP 424 instead of 25 messages.

    UNREAD_ONLY WALKS PAGES. The REST GET cannot filter by read state (see
    `_fetch_inbox_page`), so an unread-only result is filtered HERE, after a
    mixed page has already been fetched -- and on a newest-first inbox whose
    recent rows are read ones, that empties the page. Measured 2026-08-24: 11
    unread in the account, only 3 of them on page 1, so every caller of this
    helper reported 3 of 11 and none of them said so. When `unread_only` is
    set the walk therefore continues into later pages until the caller's limit
    is met, the server's own unread count is satisfied, the pages run out, or
    `INBOX_MAX_PAGES_WALKED` round trips have been spent.

    LISTED AND NOT_LISTED exist so a partial answer can never pass for a
    complete one. `listed` is how many rows are in `messages`; `not_listed` is
    how many unread the server counts that are NOT among them. One rule on
    both paths: `not_listed = max(0, unread - unread rows returned)`. A caller
    seeing `not_listed > 0` is holding a partial answer and knows it.
    """
    limit = validate_limit(limit)
    page = validate_page(page)
    page_size = min(limit, INBOX_MAX_PAGE_SIZE)

    messages: list = []
    total = 0
    unread_count = 0
    total_power_nvite = 0
    unread_power_nvite = 0
    relevant_count = 0
    has_power_nvites = False
    pages_walked = 0
    current = page

    while True:
        resp = await _fetch_inbox_page(page_size, current, mail_type, unread_only)
        pages_walked += 1

        inbox_list = resp.get("inbox", [])
        total = resp.get("totalCount") or resp.get("total", len(inbox_list))
        unread_count = resp.get("unreadCount") or resp.get("unread", 0)
        total_power_nvite = resp.get("totalPowerNvite", 0)
        unread_power_nvite = resp.get("unreadPowerNvite", 0)
        relevant_count = resp.get("relevantCount", 0)
        has_power_nvites = resp.get("hasPowerNvites", False)

        for msg in inbox_list:
            if unread_only and bool(msg.get("isRead") or msg.get("read")):
                continue
            messages.append(_parse_inbox_row(msg))
            if len(messages) >= limit:
                break

        # A mixed call is one page, exactly as before: nothing is filtered
        # away, so there is no shortfall to chase.
        if not unread_only:
            break
        if len(messages) >= limit:
            break
        # The server's own unread total is satisfied -- there is nothing later
        # to find, so stop rather than pay for a page that cannot help.
        if len(messages) >= _as_count(unread_count, fallback=len(messages)):
            break
        # A raw page shorter than requested is the end of the data, whatever
        # `total` says. Same rule `_triage_inbox` learned the hard way.
        if len(inbox_list) < page_size:
            break
        # Arithmetic exhaustion, for a server whose pages never run short.
        if (current - page + 1) * page_size >= _as_count(total, fallback=0):
            break
        if pages_walked >= INBOX_MAX_PAGES_WALKED:
            break
        current += 1

    listed = len(messages)
    unread_returned = sum(1 for m in messages if not m["is_read"])
    not_listed = max(0, _as_count(unread_count, fallback=unread_returned) - unread_returned)

    return {
        "status": "success",
        "total": total,
        # `count` is how many rows this call LISTED. It is not an unread
        # total and never was; a caller wanting that reads `unread`, and one
        # wanting to know whether the two agree reads `not_listed`.
        "count": listed,
        "listed": listed,
        "not_listed": not_listed,
        "page": page,
        "page_size": page_size,
        "pages_walked": pages_walked,
        "next_page": current + 1,
        "has_more": (not_listed > 0) if unread_only
                    else ((current * page_size) < _as_count(total, fallback=0)),
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

    conversation_id = safe_get(mail, "conversationId", default=None)

    return {
        "status": "success",
        "message_id": message_id,
        "subject": safe_get(mail, "subject", default="", field_name="subject", warn=True, context="read_message"),
        "content": content,
        "date": safe_get(mail, "dateTime", "date", default=""),
        "type": safe_get(mail, "messageType", "type", default=""),
        "conversation_id": conversation_id,
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

    # Emit RecruiterEngaged so subscribers can react (notifications, scoring updates)
    try:
        from naukri_server.events import event_bus, RecruiterEngaged
        await event_bus.emit(RecruiterEngaged(
            recruiter_name="",
            company="",
            action="MARKED_INTERESTED" if interested else "MARKED_NOT_INTERESTED",
            job_id="",
            title="",
        ))
    except Exception as e:
        logger.warning("Failed to emit RecruiterEngaged: %s", e)

    return {"status": "success", "mail_id": mail_id, "interested": interested, "message": f"Marked as {action}."}


async def _accept_nvite(nvite_job_id: str, answers: Optional[dict], title: Optional[str], company: Optional[str]) -> dict:
    """Accept a recruiter NVite by applying to the associated job."""
    from naukri_server.tools.apply import _apply_single

    result = await _apply_single(
        job_id=nvite_job_id,
        answers=answers,
        title=title,
        company=company,
        tracking_extra={"source": "nvite"},
    )

    # Emit InboxInviteAccepted only when the underlying apply succeeded
    if isinstance(result, dict) and result.get("status") == "applied":
        try:
            from naukri_server.events import event_bus, InboxInviteAccepted
            await event_bus.emit(InboxInviteAccepted(
                invite_id=str(nvite_job_id),
                recruiter_id="",
            ))
        except Exception as e:
            logger.warning("Failed to emit InboxInviteAccepted: %s", e)

    return result


# ---------------------------------------------------------------------------
# Single-purpose MCP tools
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
        limit: Max messages to return (default 20, max 50). The endpoint
            itself caps a PAGE at 20, so a larger limit is served by walking
            pages rather than by a bigger request -- asking for 25 in one page
            used to come back as a raw HTTP 424.
        unread_only: Only return unread messages (default False). The endpoint
            has no unread filter, so this walks pages until the server's own
            unread count is satisfied; `not_listed` reports any shortfall.
        mail_type: Filter by type e.g. "powerNvite" (default None for all)
        page: Page number to start from (default 1)

    Returns:
        {status, total, count, listed, not_listed, page, page_size,
         pages_walked, next_page, has_more, unread, total_power_nvite,
         unread_power_nvite, relevant_count, has_power_nvites, messages: [...]}

        `count`/`listed` are rows in hand. `unread` is the server's
        authoritative unread total, and `not_listed` is how many of it are
        NOT in `messages` -- 0 when the answer is complete.
    """
    return await handle_tool_action(
        lambda: _fetch_inbox(limit=limit, unread_only=unread_only, mail_type=mail_type or "", page=page),
        "list_inbox",
    )


async def _triage_inbox(limit: int, unread_only: bool, min_fit_score: Optional[int]) -> dict:
    """Fetch the inbox across pages, score each row, and rank it."""
    page_size = validate_limit(limit if limit and limit <= 50 else 50)

    rows, page, total, unread_total = [], 1, 0, 0
    while len(rows) < limit:
        batch = await _fetch_inbox(limit=page_size, unread_only=unread_only, page=page)
        total = batch.get("total") or 0
        unread_total = batch.get("unread") or 0
        got = batch.get("messages") or []
        rows.extend(got)
        # A SHORT PAGE ENDS THE WALK, and it outranks `has_more`. `has_more` is
        # arithmetic on the server's `total` (`page * limit < total`), so when
        # that total disagrees with the rows actually delivered -- a filtered
        # `unread_only` page, a total counting mails this endpoint does not
        # return, a stale count -- it keeps saying yes while every further page
        # adds nothing new. Trusting it alone spun this loop seven times over
        # one message. Row count is what was really delivered; believe it.
        #
        # SHORT AGAINST WHAT, THOUGH. The comparison is to the page size
        # `_fetch_inbox` ACTUALLY put on the wire, which is capped at
        # INBOX_MAX_PAGE_SIZE and so is not the `page_size` computed here.
        # Comparing 20 delivered rows against a locally-guessed 50 makes every
        # full page look short and ends the walk on page one -- the same
        # rows-versus-pages conflation this whole file just had fixed.
        wire_page_size = batch.get("page_size") or page_size
        if not got or len(got) < wire_page_size or not batch.get("has_more"):
            break
        # `_fetch_inbox` walks pages itself when `unread_only` is set, so the
        # next page to ask for is the one AFTER the last it consumed, not
        # `page + 1` -- which would re-read pages it already returned.
        page = batch.get("next_page") or (page + 1)
    rows = rows[:limit]

    # Scoring is best-effort and says so. A profile that cannot be read must
    # not cost the operator his triage -- the ordering degrades to recency and
    # `scored` reports what happened, rather than every row silently showing
    # fit_score null as though the jobs were all bad matches.
    #
    # `can_score` REPORTS ON THE PROFILE READ. `scored` REPORTS ON THE ROWS.
    # They were one flag, and that flag was set True before a single row had
    # been scored and cleared only when the profile read failed -- so a run
    # where the profile loaded fine and then EVERY row raised shipped
    # `scored: true` beside a column of nulls. Two different measurements
    # need two different fields; `scoring_error` stays with the profile read,
    # which is a genuinely different cause and stays distinguishable.
    can_score, scoring_error = True, None
    scoring_attempted, scored_rows = 0, 0
    profile_skills, profile = set(), {}
    try:
        from naukri_server.scoring import parse_skills, score_job
        from naukri_server.services.profile_service import get_cached_profile

        profile = await get_cached_profile() or {}
        profile_skills = parse_skills(
            safe_get(profile, "key_skills", field_name="key_skills", warn=False, default=[]))
    except Exception as e:  # pragma: no cover - exercised via the failure test
        can_score, scoring_error = False, "%s: %s" % (type(e).__name__, e)
        logger.warning("Inbox triage could not score: %s", e)

    out = []
    for m in rows:
        jd = m.get("job_details") or {}
        cd = m.get("company_details") or {}
        fit = None
        if can_score:
            scoring_attempted += 1
            try:
                fit_obj = score_job(
                    parse_skills(jd.get("key_skills") or ""),
                    profile_skills,
                    "",
                    safe_get(profile, "total_experience", field_name="total_experience", warn=False),
                    job_location=jd.get("location"),
                    profile_location=safe_get(profile, "current_location", field_name="current_location", warn=False),
                    job_work_mode=jd.get("work_mode"),
                    job_salary=m.get("salary"),
                    profile_expected_ctc=safe_get(profile, "expected_ctc", field_name="expected_ctc", warn=False),
                    experience_min=jd.get("experience_min"),
                    experience_max=jd.get("experience_max"),
                )
                # `overall_score`, NOT `fit_score`. `FitScore.to_dict()` has
                # keys bonuses / experience_match / overall_score / reasons /
                # recommendation / skill_match and has never had a `fit_score`
                # -- so `.get("fit_score")` returned None on EVERY row, raised
                # nothing, never reached the `except` below, and logged no
                # warning. A missing key is not an error to `.get`, which is
                # exactly why this was invisible: 3 of 3 live rows came back
                # with fit_score null beside `scored: true`. Read the attribute
                # rather than the dict, so a future rename breaks loudly here
                # instead of silently returning None.
                fit = fit_obj.overall_score
                scored_rows += 1
            except Exception as e:
                logger.warning("Inbox triage could not score one row: %s", e)

        out.append({
            "mail_id": m.get("mail_id"),
            "message_id": m.get("message_id"),
            "vcard_id": m.get("vcard_id"),
            "unique_id": m.get("unique_id"),
            "conversation_id": m.get("conversation_id"),
            "unread": not m.get("is_read"),
            "date": m.get("date"),
            "subject": m.get("subject"),
            "company": cd.get("company_name") or m.get("sender") or "",
            "recruiter_name": m.get("recruiter_name"),
            "job_title": jd.get("job_title") or "",
            "location": jd.get("location") or "",
            "work_mode": jd.get("work_mode") or "",
            "key_skills": jd.get("key_skills") or "",
            "salary": m.get("salary"),
            "ctc_min": jd.get("ctc_min"),
            "ctc_max": jd.get("ctc_max"),
            "company_rating": cd.get("ambition_box_rating"),
            "company_reviews": cd.get("ambition_box_reviews"),
            "consultant": jd.get("consultant"),
            "client_name": jd.get("client_name") or "",
            "has_screening_questions": m.get("has_screening_questions"),
            "is_applied": m.get("is_applied"),
            "nvite_job_id": jd.get("nvite_job_id") or "",
            "preview": m.get("preview") or "",
            "fit_score": fit,
        })

    if min_fit_score is not None:
        out = [r for r in out if r["fit_score"] is not None and r["fit_score"] >= min_fit_score]

    # Unread first, then best fit, then most recent. Unread leads because it is
    # the only axis the operator cannot recover by looking -- a read message he
    # has already judged once.
    out.sort(key=lambda r: (
        0 if r["unread"] else 1,
        -(r["fit_score"] if r["fit_score"] is not None else -1),
        r["date"] or "",
    ))

    # HOW MANY UNREAD THIS ANSWER DOES NOT CONTAIN. Counted off the rows that
    # survive to `messages`, so a `min_fit_score` that filtered unread rows
    # away is included in the gap rather than hidden by it. Zero means the
    # answer is complete against the server's own unread count.
    unread_returned = sum(1 for r in out if r["unread"])
    not_listed = max(0, (unread_total if isinstance(unread_total, int)
                         and not isinstance(unread_total, bool)
                         else unread_returned) - unread_returned)

    return {
        "status": "success",
        "total_in_inbox": total,
        "unread_in_inbox": unread_total,
        "returned": len(out),
        "unread_returned": unread_returned,
        "not_listed": not_listed,
        # `scored` is now a statement about the ROWS: it is True only if at
        # least one of them actually carries a score. `scored_rows` of
        # `scoring_attempted` is the honest form of the same fact, and the
        # pair makes the old "scored: true beside three nulls" unrepresentable.
        "scored": scored_rows > 0,
        "scored_rows": scored_rows,
        "scoring_attempted": scoring_attempted,
        "scoring_error": scoring_error,
        "min_fit_score": min_fit_score,
        "needs_answers": sum(1 for r in out if r["has_screening_questions"]),
        "already_applied": sum(1 for r in out if r["is_applied"]),
        "from_consultancies": sum(1 for r in out if r["consultant"]),
        "messages": out,
    }


@mcp.tool()
async def naukri_triage_inbox(
    limit: int = 60,
    unread_only: bool = False,
    min_fit_score: Optional[int] = None,
) -> dict:
    """Rank the whole inbox in ONE call, so choosing what to answer is cheap.

    The problem this exists for: the inbox holds 62 recruiter messages. Picking
    which ones deserve a response used to mean reading them one at a time,
    because the list rows came back with an empty preview and no recruiter
    name. Both are now populated, so a single call can carry enough of each
    message to decide on it.

    Every row is scored through the SAME engine as the rest of the family
    (`naukri_server.scoring.score_job`), so a fit score here means what it
    means in `naukri_score_saved_jobs` and on the Uplers server. Rows are
    ordered unread first, then by fit, then most recent.

    What it deliberately does NOT do: reply. Naukri exposes no reply, compose
    or thread surface to a jobseeker -- the inbox is one-directional, and the
    only actions the platform offers on a recruiter message are Apply and
    Not Interested (`naukri_accept_nvite`, `naukri_mark_interested`). See
    `naukri_draft_follow_up` for text to send through another channel.

    Args:
        limit: Max messages to triage (default 60, pages as needed)
        unread_only: Only triage unread messages (default False)
        min_fit_score: Drop rows scoring below this (0-100). Rows that could
            not be scored are dropped by this filter rather than kept, since
            an unscored row cannot be said to clear a bar.

    Returns:
        {status, total_in_inbox, unread_in_inbox, returned, unread_returned,
         not_listed, scored, scored_rows, scoring_attempted, scoring_error,
         min_fit_score, needs_answers, already_applied, from_consultancies,
         messages: [...]}

        `not_listed` is how many unread the server counts that are NOT in
        `messages` -- 0 when this answer is complete. It exists because the
        endpoint cannot filter by read state, so an unread-only answer is
        assembled by walking pages and could in principle come up short.

        `scored` reports on the ROWS, not on the call: it is True only if at
        least one row actually carries a fit score, and `scored_rows` of
        `scoring_attempted` says how many. `scoring_error` is reserved for a
        failed PROFILE read -- a different cause with a different remedy --
        and when it is set nothing was attempted at all; the ranking then
        falls back to recency rather than pretending every job is a poor
        match.
    """
    if limit is not None and limit < 1:
        return {"status": "error", "message": "limit must be at least 1.", "error_code": "VALIDATION_ERROR"}
    if min_fit_score is not None and not 0 <= min_fit_score <= 100:
        return {"status": "error", "message": "min_fit_score must be between 0 and 100.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _triage_inbox(limit or 60, unread_only, min_fit_score),
        "triage_inbox",
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
