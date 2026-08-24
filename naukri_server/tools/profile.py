"""Profile tools — get, update, audit, boost, dashboard, and targeting.

Tool layer — business logic + browser orchestration live in
``services/profile_service.py`` (and ``profile_update.py`` for browser-
specific DOM helpers). Direct ``browser`` imports were removed in the
Wave 2B hexagonal-completion pass; tools now drive the browser only via
the service layer's ``do_update`` / ``do_boost`` helpers.
"""

from typing import Optional

from naukri_server import mcp
from naukri_server.interfaces import api_client  # noqa: F401 — tests patch this path
from naukri_server.error_handler import handle_tool_action
from naukri_server.tools.profile_sections import write_section
from naukri_server.tools.profile_restore import (
    list_profile_snapshots,
    restore_section,
)
from naukri_server.utils import TtlCache

# ---------------------------------------------------------------------------
# Re-export business logic from profile_service (backward compat for tests
# that patch via the tools.profile path)
# ---------------------------------------------------------------------------
from naukri_server.services.profile_service import (  # noqa: F401
    _get_profile,
    _fetch_raw_profile,
    get_cached_profile,
    _get_dashboard,
    _fetch_raw_dashboard,
    get_cached_dashboard,
    _audit_profile,
    _profile_ttl_cache,
    _dashboard_ttl_cache,
    do_update as _do_update_service,
    do_boost as _do_boost_service,
)

# Backward-compat alias — tests import _TtlCache from here
_TtlCache = TtlCache


# ---------------------------------------------------------------------------
# Thin tool-layer adapters that delegate to the service-layer browser
# orchestration. Kept under their original ``_do_*`` names so daily_brief,
# auto_hunt, and tests that patch ``naukri_server.tools.profile._do_*``
# continue to work.
# ---------------------------------------------------------------------------

async def _do_update(**kw) -> dict:
    """Adapter to ``profile_service.do_update`` — preserves call site."""
    return await _do_update_service(
        fields=kw.get("fields"),
        notice_period=kw.get("notice_period"),
        expected_ctc=kw.get("expected_ctc"),
        current_ctc=kw.get("current_ctc"),
    )


async def _do_boost(**kw) -> dict:
    """Adapter to ``profile_service.do_boost`` — preserves call site."""
    return await _do_boost_service(randomize=kw.get("randomize", False))


# ---------------------------------------------------------------------------
# Single-purpose profile tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_get_profile() -> dict:
    """Fetch full Naukri profile — skills, employment, education, CTC, etc.

    Returns profile data including name, skills_with_experience, employment
    history, education, current/expected CTC, and notice period.
    """
    return await handle_tool_action(lambda: _get_profile(), "profile.get")


@mcp.tool()
async def naukri_update_profile(
    fields: Optional[dict] = None,
    notice_period: Optional[str] = None,
    expected_ctc: Optional[float] = None,
    current_ctc: Optional[float] = None,
) -> dict:
    """Update profile fields via browser UI.

    Args:
        fields: Dict of fields to change (resumeHeadline, keySkills, noticePeriod, etc.)
        notice_period: Shorthand — "Serving Notice Period", "15 Days or less", "1 Month", etc.
        expected_ctc: Shorthand — expected CTC in lakhs (e.g., 15)
        current_ctc: Shorthand — current CTC in lakhs (e.g., 12)
    """
    return await handle_tool_action(
        lambda: _do_update(
            fields=fields, notice_period=notice_period,
            expected_ctc=expected_ctc, current_ctc=current_ctc,
        ),
        "profile.update",
    )


@mcp.tool()
async def naukri_audit_profile() -> dict:
    """Audit profile completeness and get improvement suggestions.

    Returns completeness percentage, grade, strengths, gaps, and actionable tips.
    """
    return await handle_tool_action(lambda: _audit_profile(), "profile.audit")


@mcp.tool()
async def naukri_boost_profile(randomize: bool = False) -> dict:
    """Re-save headline to appear as 'recently active' in recruiter searches.

    Args:
        randomize: If True, wait random 0-300s before refreshing (avoids patterns).
    """
    return await handle_tool_action(
        lambda: _do_boost(randomize=randomize), "profile.boost",
    )


@mcp.tool()
async def naukri_dashboard() -> dict:
    """Get dashboard data — profile views, recruiter activity, completeness, notifications.

    Returns profile_views, recruiter_activity_date, ctc_lpa, experience_years,
    and other dashboard metrics.
    """
    return await handle_tool_action(lambda: _get_dashboard(), "profile.dashboard")


@mcp.tool()
async def naukri_profile_targeting() -> dict:
    """How Naukri's ad system sees your profile — 35 targeting fields, completeness gaps.

    Returns DFP targeting profile with CTC, experience, age, gender, location,
    and identifies empty fields that reduce ad relevance.
    """
    return await handle_tool_action(lambda: _do_targeting(), "profile.targeting")


# ---------------------------------------------------------------------------
# Section writes and their restore path
#
# `profile_sections.write_section` is the verified write layer and
# `profile_restore` reads a snapshot back through that same gate. Both are
# registration-only here -- no logic lives in the tool layer.
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_update_profile_section(
    section: str,
    fields: dict,
    confirm: bool = False,
) -> dict:
    """Write ONE profile section through the verified REST write route.

    NOTHING IS WRITTEN UNLESS confirm=True. The default returns the exact
    body that would be sent, so the payload can be read before it is fired.

    THE WRITE IS VERIFIED BY RE-READING THE PROFILE. A 200 from Naukri means
    the request was accepted -- not that the field changed. So the profile is
    read again afterwards and `verified` is reported off that second read. A
    write that was accepted but did not persist comes back as an error naming
    the snapshot to restore from; it is never reported as a success.

    A PRE-WRITE SNAPSHOT IS ALWAYS TAKEN and its name is returned in
    `snapshot`. List them with naukri_list_profile_snapshots and act on one
    with naukri_restore_profile_section.

    SIX COLLECTIONS ARE SENT AS WHOLE LISTS by Naukri's own editor:
    languages, onlineProfiles, workSamples, presentations, publications and
    patents. A payload carrying one row would DELETE every other row in the
    section, so this tool reads the live list and merges your row into it
    before sending -- the other rows survive. The reply also carries
    `rows_lost` and `collateral`, which is the only way a silent deletion can
    be seen at all: the API returns success while doing it.

    TO UPDATE A ROW, PASS ITS ID FIELD. TO CREATE ONE, OMIT IT.

    Args:
        section: one of --
            scalar blocks: summary, resumeHeadline, keySkills,
                careerPreferences
            one row per call (id field): employments (employmentId),
                educations (educationId), schools (schoolId),
                itskills (sid), projects (projectId),
                certifications (certificationsId)
            whole lists (id field): languages (languageId),
                onlineProfiles (onlineProfileId), workSamples (workSampleId),
                presentations (presentationId),
                publications (publicationId), patents (patentId)
        fields: the fields to set. Include the section's id field to UPDATE
            an existing row; omit it to CREATE one. Controlled-vocabulary
            fields must be objects like {"id": <id>, "value": <label>} -- a
            bare string is accepted by the API and then silently ignored.
        confirm: True writes. False (the default) previews and writes nothing.
    """
    return await handle_tool_action(
        lambda: write_section(section=section, fields=fields, confirm=confirm),
        "profile.write_section",
    )


@mcp.tool()
async def naukri_list_profile_snapshots(limit: int = 20) -> dict:
    """List the pre-write profile snapshots, newest first. METADATA ONLY.

    Every confirmed section write takes a full profile snapshot first. This
    lists them: file name, when it was taken, its label, its size on disk and
    how many sections it holds. No profile field VALUES are returned here --
    pass a `file` value to naukri_restore_profile_section to act on one.

    Args:
        limit: how many to return, newest first. Default 20.
    """
    return await handle_tool_action(
        lambda: list_profile_snapshots(limit=limit),
        "profile.list_snapshots",
    )


@mcp.tool()
async def naukri_restore_profile_section(
    snapshot: str,
    section: str,
    confirm: bool = False,
) -> dict:
    """Restore ONE section from a snapshot, through the same verified write.

    NOTHING IS WRITTEN UNLESS confirm=True; the default returns the exact
    body the restore would send. The restore is issued as an ordinary section
    write, so it inherits the confirm gate, its own pre-write snapshot, the
    post-write re-read that decides `verified`, and the collateral detector.

    WHAT IT RESTORES:
        - the scalar blocks (summary, resumeHeadline, keySkills,
          careerPreferences) -- fully, in one write.
        - the whole-list collections (languages, onlineProfiles, workSamples,
          presentations, publications, patents) -- fully in one write when
          the live list and the snapshot differ in exactly one addressable
          row, which is the shape of undoing the write this snapshot was
          taken for.

    WHAT IT REFUSES RATHER THAN HALF-DOING:
        - the one-row-per-call collections (employments, educations, schools,
          itskills, projects, certifications). It returns status
          "unsupported", names the row ids that differ, and writes NOTHING.
          Restore those one at a time with naukri_update_profile_section,
          passing each row's id field.
        - any restore that would need more than one write. A partial restore
          reported as success is worse than a refusal.

    A ROW DELETED ON NAUKRI.COM CANNOT BE RECREATED WITH ITS ORIGINAL ID.
    Re-sending it creates a NEW row with a NEW id, and anything that
    referenced the old id still points at nothing.

    Args:
        snapshot: the `file` name from naukri_list_profile_snapshots.
        section: which section to restore.
        confirm: True writes. False (the default) previews and writes nothing.
    """
    return await handle_tool_action(
        lambda: restore_section(snapshot=snapshot, section=section,
                                confirm=confirm),
        "profile.restore_section",
    )


# ---------------------------------------------------------------------------
# Backward compatibility — re-export symbols that moved to new modules
# ---------------------------------------------------------------------------
from naukri_server.tools.profile_update import (  # noqa: E402, F401
    _update_profile, _boost_visibility,
    _SAVE_MODAL_JS, _fill_ctc_input, _click_save_modal,
    UPDATABLE_FIELDS, BROWSER_SUPPORTED_FIELDS,
    _update_headline, _update_key_skills, _update_notice_period,
    _update_current_ctc, _update_expected_ctc,
    _STANDALONE_HANDLERS, _CAREER_FIELD_HANDLERS, _FIELD_HANDLERS,
    _open_career_profile_modal, _ctc_find_js,
)
from naukri_server.tools.profile_targeting import _do_targeting  # noqa: E402, F401
