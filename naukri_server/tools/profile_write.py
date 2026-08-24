"""Profile-write TRANSPORT -- the single chokepoint for every fullprofiles write.

This module carries NO section-specific logic. It knows exactly three things,
all of them measured off Naukri's own shipped editor bundle `mnj_v320.min.js`
(848718 bytes, asset prefix /s/5/105/j/) and written up in
`_audit/_slices/naukri-profile-write-contract.md`:

  1. The route is the **v1** path `FULLPROFILES_WRITE_API` (constant `_c`,
     bundle offset 44671). The bundle contains zero references to a v0 or v2
     fullprofiles path from the profile editor.
  2. The wire method is **POST carrying `X-HTTP-Method-Override: PUT`**. All 21
     write call sites in the bundle set that header; the semantic verb is PUT,
     the wire verb is POST. `profile_update.py` matching
     `response.request.method == "POST"` is CORRECT against this contract and
     must not be "fixed" to PUT.
  3. Every payload is `Object.assign({}, <sectionBuilder>(...), {profileId})`,
     so `profileId` sits at the TOP level of the body, beside the section key.

The section layer builds `section_body`; this module owns the route, the
header, and the placement of `profileId`. Keep it that way -- one place to
change if Naukri moves the route, one place a test can pin.
"""

from typing import Optional

from naukri_server.config import FULLPROFILES_WRITE_API, PROFILE_API
from naukri_server.interfaces import api_client

# The override header, verbatim. Every fullprofiles write carries it.
METHOD_OVERRIDE_HEADER = {"X-HTTP-Method-Override": "PUT"}

# Collections the editor sends as WHOLE LISTS -- see fullprofiles_write's
# docstring. Named here so the section layer can import the list rather than
# re-typing it.
WHOLE_LIST_COLLECTIONS = (
    "onlineProfiles",
    "workSamples",
    "presentations",
    "publications",
    "patents",
    "languages",
)


def _validate_profile_id(value, source: str) -> str:
    """Return ``value`` if it is a usable profileId, else raise.

    A blank profileId is a live hazard, not a hypothetical: an empty-string
    ``profileId`` was measured on a section row of the live profile read, so
    "the key was present" is not enough to trust it. Never fabricate, never
    default, never send an empty one.
    """
    if not isinstance(value, str):
        raise ValueError(
            "profileId from %s is not a string (got %s) -- refusing to write."
            % (source, type(value).__name__)
        )
    if not value.strip():
        raise ValueError(
            "profileId from %s is empty -- refusing to write." % source
        )
    return value


async def _get_profile_id() -> str:
    """Read the live profile and return its top-level ``profileId``.

    Reads ``PROFILE_API`` with ``expand_level=4`` (the endpoint is a hard HTTP
    400 without ``expand_level``) and takes ``profile[0]["profileId"]``. The id
    is a 64-character string on the live account -- it is NOT an integer, so
    callers must not coerce it.

    Raises:
        ValueError: if the response has no usable ``profile[0]``, or the
            ``profileId`` on it is missing, empty or not a string. Raising is
            the point: a fabricated or blank id must never reach the wire.
    """
    data = await api_client.get(PROFILE_API, params={"expand_level": "4"})

    profiles = (data or {}).get("profile")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError(
            "Profile read returned no 'profile' list -- cannot resolve "
            "profileId. Is the session logged in?"
        )
    first = profiles[0]
    if not isinstance(first, dict):
        raise ValueError(
            "Profile read returned profile[0] of type %s, expected a dict -- "
            "cannot resolve profileId." % type(first).__name__
        )
    if "profileId" not in first:
        raise ValueError(
            "Profile read returned profile[0] with no 'profileId' key -- "
            "cannot resolve profileId."
        )
    return _validate_profile_id(first["profileId"], "the profile read")


async def fullprofiles_write(section_body: dict,
                             profile_id: Optional[str] = None) -> dict:
    """POST one section delta to the fullprofiles v1 write route.

    Sends ``{**section_body, "profileId": <id>}`` to ``FULLPROFILES_WRITE_API``
    as a POST carrying ``X-HTTP-Method-Override: PUT``.

    SEMANTICS ARE DELTA / MERGE. An omitted key is NOT a deletion: the server
    merges what it receives and ignores what it does not. Clearing a scalar is
    an explicit empty string (the editor's delete path sends ``summary: ""``,
    ``keySkills: ""``), and clearing tracked fields is an explicit
    ``isDelete: [...]`` array inside the section object. Deleting a ROW is a
    different route entirely (a per-row POST with
    ``X-HTTP-Method-Override: DELETE``), not something this function does.

    WARNING -- SIX COLLECTIONS ARE SENT AS WHOLE LISTS by the editor:
    ``onlineProfiles``, ``workSamples``, ``presentations``, ``publications``,
    ``patents`` and ``languages`` (see ``WHOLE_LIST_COLLECTIONS``). The editor
    sends EVERY row of these every time, so a caller that passes only the one
    row it wants to add WILL DROP THE OTHERS. That merge is the section layer's
    job -- it is deliberately NOT done here, because this module must stay a
    pure transport with no section knowledge. Every other collection
    (``employments``, ``educations``, ``schools``, ``itskills``, ``projects``,
    ``certifications``) is sent as a single-element array of just the edited
    row and is safe.

    Args:
        section_body: the section envelope alone, e.g.
            ``{"profile": {"resumeHeadline": "..."}}`` or
            ``{"itskills": [{...}]}``. Must NOT carry ``profileId`` -- this
            function owns that key and its top-level placement.
        profile_id: the 64-character profile id. Resolved via
            ``_get_profile_id()`` when omitted.

    Returns:
        The parsed response body.

    Raises:
        ValueError: if ``section_body`` is not a dict, is empty, or already
            carries ``profileId``; or if the resolved id is unusable.
    """
    if not isinstance(section_body, dict):
        raise ValueError(
            "section_body must be a dict, got %s." % type(section_body).__name__
        )
    if "profileId" in section_body:
        raise ValueError(
            "section_body must not carry 'profileId' -- fullprofiles_write "
            "owns that key and places it at the TOP level of the body. Pass "
            "the id as the profile_id argument instead."
        )
    if not section_body:
        raise ValueError(
            "section_body is empty -- refusing to send a write with no section."
        )

    if profile_id is None:
        profile_id = await _get_profile_id()
    else:
        profile_id = _validate_profile_id(profile_id, "the caller")

    body = {**section_body, "profileId": profile_id}
    return await api_client.post(
        FULLPROFILES_WRITE_API, body, extra_headers=dict(METHOD_OVERRIDE_HEADER),
    )
