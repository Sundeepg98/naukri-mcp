"""Naukri has no jobseeker interview surface, and the interview tools must say so.

Every test is PURE: no network, no browser, no real file I/O.

THE MEASUREMENT THIS FILE STANDS FOR (2026-08-23)
-------------------------------------------------
Four candidate interview endpoints were probed live and do not exist, against
family-local controls on the same services that answered. Three live
applications were read and expose exactly three states -- Applied, Application
Sent, Application Viewed -- with no interview state among them. The logged-in
navigation carries no interview tab. `interview_rounds` in `naukri.db` held 0
rows, so nothing had ever round-tripped either.

So `naukri_add_interview_round` and `naukri_list_interview_rounds` are a
PRIVATE NOTEBOOK. They cannot reach the platform because there is nothing on
the platform to reach. That is a fine thing for them to be -- it is not fine
for them to be that silently, under a `naukri_`-prefixed name that reads like
a channel to Naukri.

These tests pin both halves: the absence (no tool may claim to schedule, and
the write may not touch Naukri), and the disclosure (the docstrings and the
returned payload must keep saying so). If somebody deletes the disclosure
because it looks like clutter, this file fails and points them back at the
probes above.
"""

import pytest
from unittest.mock import AsyncMock, patch


NAUKRI_API_SINKS = ("api_get", "api_post", "api_put", "api_delete")


class TestNoToolClaimsToScheduleAnInterview:

    def test_no_interview_scheduling_tool_is_registered(self):
        """A tool that schedules or answers an interview would be one that cannot work.

        Naukri offers a jobseeker no interview endpoint, no interview tab and
        no interview application state. Re-run the probes in this module's
        docstring before registering any of these names.
        """
        import naukri_server

        registered = naukri_server.mcp._tool_manager._tools
        for banned in ("naukri_schedule_interview", "naukri_confirm_interview",
                       "naukri_accept_interview_slot", "naukri_decline_interview",
                       "naukri_respond_to_interview", "naukri_reschedule_interview"):
            assert banned not in registered, (
                "%s is registered, but Naukri exposes no jobseeker interview "
                "surface -- re-run the probes in this file's docstring before "
                "keeping it" % banned
            )

    def test_the_two_notebook_tools_are_still_registered(self):
        """Control: the ban above is a ban on four names, not on the feature."""
        import naukri_server

        registered = naukri_server.mcp._tool_manager._tools
        assert "naukri_add_interview_round" in registered
        assert "naukri_list_interview_rounds" in registered


class TestTheWriteNeverReachesNaukri:

    @pytest.mark.asyncio
    async def test_recording_a_round_makes_no_naukri_api_call(self):
        """The whole point: this write is local. Nothing is sent to Naukri.

        The event bus is stubbed out so this measures the tool's OWN path. The
        subscriber it would otherwise fire reads AmbitionBox (a third party),
        which is a read and is documented in the tool's docstring -- it is not
        a write to Naukri and is not what this test is about.
        """
        from naukri_server.tools.tracking import naukri_add_interview_round
        import naukri_server.api as api

        sinks = {name: AsyncMock(return_value={}) for name in NAUKRI_API_SINKS}
        with patch("naukri_server.database.add_interview_round", new=AsyncMock(return_value=1)), \
                patch("naukri_server.database.list_interview_rounds", new=AsyncMock(return_value=[])), \
                patch("naukri_server.events.event_bus.emit", new=AsyncMock()), \
                patch.multiple(api, **sinks):
            result = await naukri_add_interview_round(job_id="123", round_type="technical")

        assert result["status"] == "success", result
        for name, mock in sinks.items():
            assert mock.await_count == 0, (
                "%s was called -- an interview round must not reach Naukri" % name
            )

    @pytest.mark.asyncio
    async def test_CONTROL_the_sink_mocks_can_actually_fire(self):
        """Proves the instrument above is live, not four mocks nobody touches.

        Without this, `await_count == 0` is equally consistent with "patched
        the wrong module path" -- the failure mode that makes a leak test
        certify nothing. Here the same patch is applied and the same sink IS
        called, through the same lazy import the production code uses.
        """
        import naukri_server.api as api
        from naukri_server.interfaces import api_client

        sinks = {name: AsyncMock(return_value={}) for name in NAUKRI_API_SINKS}
        with patch.multiple(api, **sinks):
            await api_client.post("/anything", {})
        assert sinks["api_post"].await_count == 1, (
            "the api_post patch never fired, so the absence test above proves nothing"
        )

    @pytest.mark.asyncio
    async def test_listing_rounds_makes_no_naukri_api_call(self):
        from naukri_server.tools.tracking import naukri_list_interview_rounds
        import naukri_server.api as api

        sinks = {name: AsyncMock(return_value={}) for name in NAUKRI_API_SINKS}
        with patch("naukri_server.database.list_interview_rounds", new=AsyncMock(return_value=[])), \
                patch.multiple(api, **sinks):
            result = await naukri_list_interview_rounds()

        assert result["status"] == "success", result
        for name, mock in sinks.items():
            assert mock.await_count == 0, "%s was called on a local read" % name


class TestThePayloadAndDocsKeepSayingSo:

    @pytest.mark.asyncio
    async def test_add_round_payload_declares_local_scope(self):
        """A caller that reads only the result must still learn this is local."""
        from naukri_server.tools.tracking import naukri_add_interview_round
        with patch("naukri_server.database.add_interview_round", new=AsyncMock(return_value=1)), \
                patch("naukri_server.database.list_interview_rounds", new=AsyncMock(return_value=[])), \
                patch("naukri_server.events.event_bus.emit", new=AsyncMock()):
            result = await naukri_add_interview_round(job_id="123", round_type="hr")
        assert result["scope"] == "local", result

    @pytest.mark.asyncio
    async def test_list_rounds_payload_declares_local_scope(self):
        from naukri_server.tools.tracking import naukri_list_interview_rounds
        with patch("naukri_server.database.list_interview_rounds", new=AsyncMock(return_value=[])):
            result = await naukri_list_interview_rounds()
        assert result["scope"] == "local", result

    def test_both_docstrings_disclose_that_naukri_is_not_reached(self):
        """The disclosure is load-bearing: the tool name implies the opposite.

        `naukri_add_interview_round` reads as a call to Naukri. The docstring
        is the only thing standing between that reading and a user who thinks
        a recruiter was notified.
        """
        from naukri_server.tools.tracking import (
            naukri_add_interview_round, naukri_list_interview_rounds,
        )

        for tool in (naukri_add_interview_round, naukri_list_interview_rounds):
            doc = (tool.__doc__ or "").lower()
            assert "naukri" in doc
            assert "local" in doc, (
                "%s no longer says its scope is local" % tool.__name__
            )
            assert ("nothing reaches naukri" in doc
                    or "naukri is not consulted" in doc), (
                "%s no longer discloses that the platform is not reached"
                % tool.__name__
            )

    def test_add_round_docstring_admits_the_ambitionbox_read(self):
        """"Local write" is exact; "no network call" would be false.

        The round emits an event whose subscriber fetches company interview
        data from AmbitionBox over HTTP. A docstring that claimed silence
        would be wrong in the other direction.
        """
        from naukri_server.tools.tracking import naukri_add_interview_round
        doc = (naukri_add_interview_round.__doc__ or "").lower()
        assert "ambitionbox" in doc
