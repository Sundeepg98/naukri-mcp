"""MCP prompts for Naukri server — reusable templates for AI consumers.

Prompts complement tools and resources:
  - Tools = perform computation / side effects
  - Resources = expose cacheable read-only state (URI-addressable)
  - Prompts = parameterized message templates a client can render and submit

Registered prompts (7):
  naukri/cover_letter        — generate a cover letter for a specific job
  naukri/interview_prep      — interview prep questions for a company + role
  naukri/daily_brief_intro   — opening line for the morning daily brief
  naukri/recruiter_reply     — draft reply to a recruiter inbox message
  naukri/profile_summary     — generate "About me" copy for the profile
  naukri/follow_up_email     — follow-up email template for a stale application
  naukri/screening_answer    — template for answering a screening question

Each handler returns a list of {role, content} message dicts that the client
will render. Handlers are pure (no I/O) — any data the prompt needs is passed
as parameters by the caller.
"""

from naukri_server.prompts import handlers  # noqa: F401 — triggers @mcp.prompt() registration

__all__ = ["handlers"]
