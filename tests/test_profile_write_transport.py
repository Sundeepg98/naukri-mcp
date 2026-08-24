"""Transport contract for naukri_server.tools.profile_write.

These six tests pin the WIRE SHAPE measured off Naukri's shipped editor bundle
`mnj_v320.min.js` (write-up: `_audit/_slices/naukri-profile-write-contract.md`):
the v1 route, the `X-HTTP-Method-Override: PUT` header, and `profileId` at the
TOP level of the body -- plus the three refusals that keep a malformed or
id-less write off the wire entirely.

Every test mocks the transport. ZERO network calls, ZERO browser, no writes are
ever issued against naukri.com.

Each of the six was SHOWN RED before it was allowed to count: the source was
temporarily broken in the exact way the test exists to catch (route repointed to
v0, override header dropped, profileId nested inside the section, each guard
removed), the test was observed failing, and the source was restored. The
observed failure messages are recorded in
`_audit/_slices/naukri-write-transport-slice.md`.
"""

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server.tools.profile_write import _get_profile_id, fullprofiles_write

# A stand-in for the real 64-character profileId. NEVER put the live value in a
# test file: it is account-identifying.
FAKE_PROFILE_ID = "a" * 64

# The v1 route, retyped ON PURPOSE rather than imported from config. Importing
# the constant would make this test agree with any future value of it, including
# a silent repoint back to v0 -- which is the single failure this file exists to
# catch.
EXPECTED_WRITE_URL = (
    "/cloudgateway-mynaukri/resman-aggregator-services/v1/users/self/fullprofiles"
)


def _post_spy():
    """An AsyncMock standing in for api_client.post, returning a parsed body."""
    return AsyncMock(return_value={"status": 200})


def _call_of(spy):
    """(path, body, extra_headers) from the single recorded post call.

    Tolerant of positional-vs-keyword so the assertions test the WIRE SHAPE,
    not the caller's argument style.
    """
    assert spy.await_count == 1, f"expected exactly one POST, got {spy.await_count}"
    args, kwargs = spy.call_args
    path = kwargs.get("path", args[0] if len(args) > 0 else None)
    body = kwargs.get("body", args[1] if len(args) > 1 else None)
    headers = kwargs.get("extra_headers", args[2] if len(args) > 2 else None)
    return path, body, headers


# ===========================================================================
# 1. The route is the v1 path -- asserted as a literal, so a repoint fails
# ===========================================================================

@pytest.mark.asyncio
async def test_write_goes_to_the_v1_fullprofiles_route():
    spy = _post_spy()
    with patch("naukri_server.tools.profile_write.api_client.post", new=spy):
        await fullprofiles_write({"profile": {"resumeHeadline": "x"}},
                                 profile_id=FAKE_PROFILE_ID)

    path, _, _ = _call_of(spy)
    assert path == EXPECTED_WRITE_URL, (
        "the profile editor writes to the v1 fullprofiles route (bundle "
        "mnj_v320.min.js offset 44671). Got: %r" % (path,)
    )
    assert "/v0/" not in path, "v0 is not a write route for the profile editor"


# ===========================================================================
# 2. The X-HTTP-Method-Override: PUT header rides on the outgoing call
# ===========================================================================

@pytest.mark.asyncio
async def test_write_carries_the_method_override_put_header():
    spy = _post_spy()
    with patch("naukri_server.tools.profile_write.api_client.post", new=spy):
        await fullprofiles_write({"itskills": [{"entitySkill": "Node.js"}]},
                                 profile_id=FAKE_PROFILE_ID)

    _, _, headers = _call_of(spy)
    assert headers is not None, (
        "no extra_headers reached the transport -- the override header is "
        "mandatory on every fullprofiles write (21/21 bundle call sites)"
    )
    assert headers.get("X-HTTP-Method-Override") == "PUT", (
        "the wire method is POST tunnelling a PUT; got headers=%r" % (headers,)
    )


# ===========================================================================
# 3. profileId lands at the TOP level, beside the section key -- not nested
# ===========================================================================

@pytest.mark.asyncio
async def test_profile_id_is_top_level_and_not_nested_in_the_section():
    spy = _post_spy()
    section = {"profile": {"resumeHeadline": "Senior Backend Engineer"}}
    with patch("naukri_server.tools.profile_write.api_client.post", new=spy):
        await fullprofiles_write(section, profile_id=FAKE_PROFILE_ID)

    _, body, _ = _call_of(spy)
    assert body["profileId"] == FAKE_PROFILE_ID, (
        "every payload is Object.assign({}, builder(...), {profileId}) -- the "
        "id sits at the TOP level. Got body keys: %r" % (sorted(body),)
    )
    assert "profileId" not in body["profile"], (
        "profileId must NOT be nested inside the section object"
    )
    # The section survives intact alongside it.
    assert body["profile"] == {"resumeHeadline": "Senior Backend Engineer"}
    # ...and the caller's dict was not mutated on the way through.
    assert "profileId" not in section


# ===========================================================================
# 4. A section_body that already carries profileId is REJECTED
# ===========================================================================

@pytest.mark.asyncio
async def test_section_body_carrying_profile_id_is_rejected():
    spy = _post_spy()
    with patch("naukri_server.tools.profile_write.api_client.post", new=spy):
        with pytest.raises(ValueError, match="profileId"):
            await fullprofiles_write(
                {"profile": {"summary": "x"}, "profileId": FAKE_PROFILE_ID},
                profile_id=FAKE_PROFILE_ID,
            )
    assert spy.await_count == 0, "a rejected write must never reach the wire"


# ===========================================================================
# 5. An empty section_body is REJECTED
# ===========================================================================

@pytest.mark.asyncio
async def test_empty_section_body_is_rejected():
    spy = _post_spy()
    with patch("naukri_server.tools.profile_write.api_client.post", new=spy):
        with pytest.raises(ValueError, match="empty"):
            await fullprofiles_write({}, profile_id=FAKE_PROFILE_ID)
    assert spy.await_count == 0, (
        "an id-only body would be a write with no section -- never send it"
    )


# ===========================================================================
# 6. An unusable profileId from the READ raises rather than sending
#
# Three shapes, all measured-plausible: the key absent, the key present but an
# empty string (this WAS observed on a live section row), and a non-string.
# ===========================================================================

@pytest.mark.parametrize("profile_row, label", [
    ({"name": "x"}, "profileId key absent"),
    ({"profileId": ""}, "profileId empty string"),
    ({"profileId": 12345}, "profileId non-string"),
])
@pytest.mark.asyncio
async def test_unusable_profile_id_from_the_read_raises_before_sending(
        profile_row, label):
    get_spy = AsyncMock(return_value={"profile": [profile_row]})
    post_spy = _post_spy()
    with patch("naukri_server.tools.profile_write.api_client.get", new=get_spy), \
            patch("naukri_server.tools.profile_write.api_client.post", new=post_spy):
        with pytest.raises(ValueError, match="profileId"):
            await fullprofiles_write({"profile": {"summary": "x"}})

    assert post_spy.await_count == 0, (
        "%s must abort the write, not send a fabricated or blank id" % label
    )


@pytest.mark.asyncio
async def test_profile_read_with_no_profile_list_raises():
    get_spy = AsyncMock(return_value={})
    post_spy = _post_spy()
    with patch("naukri_server.tools.profile_write.api_client.get", new=get_spy), \
            patch("naukri_server.tools.profile_write.api_client.post", new=post_spy):
        with pytest.raises(ValueError, match="profile"):
            await fullprofiles_write({"profile": {"summary": "x"}})
    assert post_spy.await_count == 0


@pytest.mark.asyncio
async def test_get_profile_id_returns_the_id_when_the_read_is_healthy():
    """CONTROL for #6: without this, "always raises" would pass every case."""
    get_spy = AsyncMock(return_value={
        "profile": [{"profileId": FAKE_PROFILE_ID, "resumeHeadline": "x"}],
    })
    with patch("naukri_server.tools.profile_write.api_client.get", new=get_spy):
        assert await _get_profile_id() == FAKE_PROFILE_ID

    get_spy.assert_awaited_once()
    _, kwargs = get_spy.call_args
    assert kwargs.get("params") == {"expand_level": "4"}, (
        "the profile read is HTTP 400 without expand_level"
    )
