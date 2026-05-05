"""MCP resources for Naukri server — URI-addressable read-only data.

Resources complement tools: tools perform computation/side effects, resources expose
cacheable read-only state. Resources are loaded lazily by clients (only on read), so
they don't pollute the LLM's context window the way tool definitions can.

Registered resources (5):
  naukri://taxonomy            — SkillTaxonomy export (88 canonical skills + 150 aliases)
  naukri://profile             — cached profile snapshot
  naukri://dashboard           — cached dashboard snapshot
  naukri://settings            — formatted settings + consent flags
  naukri://resume/templates    — resume builder template catalog

The existing @mcp.tool() versions (naukri_taxonomy, naukri_get_profile,
naukri_dashboard, naukri_get_settings, naukri_resume_templates) are KEPT for
clients that don't yet support resources (notably Claude Code CLI as of May 2026).
"""

from naukri_server.resources import handlers  # noqa: F401 — triggers @mcp.resource() registration

__all__ = ["handlers"]
