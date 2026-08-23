"""naukri_create_alert must not claim a creation it cannot show.

Every test is PURE: no network, no browser, no file I/O.

WHY THIS FILE EXISTS
--------------------
``naukri_create_alert`` used to return ``{"status": "success"}`` and emit
``AlertCreated`` for ANY response the POST did not raise on -- an empty body,
a body with no alert id, a body reporting its own failure. That is the worst
failure shape a write tool has: the caller is told the alert exists, the event
bus tells nine subscribers the alert exists, and nothing on Naukri does.

The tool's endpoint made that lie likely rather than theoretical. The constant
entered the codebase in ``863e9a4`` ("catalog 15 new API endpoints from deep
exploration + webpack analysis") -- read off the front-end bundle, never once
observed creating an alert -- while the one endpoint the live probing round
DID watch create an alert (``probing/alerts-saved-search-report.md``, a
server-issued ``searchId`` 90000000 that the same round then tried to delete)
was a different path entirely. The tool's own response parsing had always been
written for that other endpoint's payload, which is the tell.

So the guard here is not "did the POST throw". It is: **Naukri must hand back
an alert id, or the alert must be findable in the alert list afterwards.**
Absent both, the tool refuses and says which of the two checks it ran.
"""

import pytest
from unittest.mock import AsyncMock, patch


CREATED = {"info": {"searchId": 90000000, "totalRes": 34238}, "list": []}


def _patch_post(return_value):
    return patch("naukri_server.interfaces.api_client.post",
                 new=AsyncMock(return_value=return_value))


def _patch_list(alerts):
    return patch("naukri_server.tools.alerts._get_alerts_list",
                 new=AsyncMock(return_value={"status": "success",
                                             "count": len(alerts),
                                             "alerts": alerts}))


class TestCreateAlertRefusesWhatItCannotShow:

    @pytest.mark.asyncio
    async def test_empty_response_and_empty_list_is_an_error_not_a_success(self):
        """THE RED ONE. Naukri created nothing; the tool must not say it did.

        This is the exact shape a dead endpoint returns: a 2xx the POST does
        not raise on, carrying no alert id, with the alert list unchanged
        afterwards. Before the fix this returned status "success".
        """
        from naukri_server.tools.alerts import naukri_create_alert
        with _patch_post({}), _patch_list([]):
            result = await naukri_create_alert(name="Node Remote", keywords="node.js")
        assert result["status"] == "error", result
        assert result["error_code"] == "API_ERROR", result
        # It must say what it checked, so the caller can tell this apart from
        # a network failure -- the POST was accepted, the creation was not.
        assert "not" in result["message"].lower()
        assert "alert" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_response_reporting_its_own_failure_is_an_error(self):
        """A 200 whose body says it failed is still a failure."""
        from naukri_server.tools.alerts import naukri_create_alert
        with _patch_post({"status": "FAILURE", "error": "Validation Error"}), _patch_list([]):
            result = await naukri_create_alert(name="Node Remote", keywords="node.js")
        assert result["status"] == "error", result

    @pytest.mark.asyncio
    async def test_unconfirmed_create_does_not_emit_AlertCreated(self):
        """No alert, no event. Nine subscribers believe this bus."""
        from naukri_server.tools.alerts import naukri_create_alert
        emitted = []

        async def _capture(event):
            emitted.append(event)

        with _patch_post({}), _patch_list([]), \
                patch("naukri_server.events.event_bus.emit", new=AsyncMock(side_effect=_capture)):
            await naukri_create_alert(name="Node Remote", keywords="node.js")
        assert emitted == [], "AlertCreated was emitted for an alert that was never created"


class TestCreateAlertConfirmsWhatItCanShow:
    """The controls. A guard that only ever refuses proves nothing."""

    @pytest.mark.asyncio
    async def test_server_issued_alert_id_is_a_success(self):
        """The measured create response: id + match count nested under `info`."""
        from naukri_server.tools.alerts import naukri_create_alert
        with _patch_post(CREATED), _patch_list([]):
            result = await naukri_create_alert(name="Node Remote", keywords="node.js")
        assert result["status"] == "success", result
        assert result["alert_id"] == "90000000", result
        assert result["total_matched"] == 34238, result
        assert result["confirmed_by"] == "alert_id"

    @pytest.mark.asyncio
    async def test_totalRes_is_read_from_info_not_the_top_level(self):
        """The count lives at `info.totalRes`. Read at the top level it is always None.

        Fails against the old parser, which called `response.get("totalRes")`
        on a payload that never carries it there.
        """
        from naukri_server.tools.alerts import naukri_create_alert
        with _patch_post(CREATED), _patch_list([]):
            result = await naukri_create_alert(name="Node Remote", keywords="node.js")
        assert result.get("total_matched") == 34238

    @pytest.mark.asyncio
    async def test_no_id_but_the_alert_is_in_the_list_is_a_success(self):
        """Read-back is the second chance, so the guard is not just `if not body`."""
        from naukri_server.tools.alerts import naukri_create_alert
        with _patch_post({}), _patch_list([{"alert_id": "999", "name": "Node Remote"}]):
            result = await naukri_create_alert(name="Node Remote", keywords="node.js")
        assert result["status"] == "success", result
        assert result["alert_id"] == "999", result
        assert result["confirmed_by"] == "read_back"

    @pytest.mark.asyncio
    async def test_confirmed_create_does_emit_AlertCreated(self):
        """The mirror of the no-event test -- the event still fires when real."""
        from naukri_server.tools.alerts import naukri_create_alert
        emitted = []

        async def _capture(event):
            emitted.append(event)

        with _patch_post(CREATED), _patch_list([]), \
                patch("naukri_server.events.event_bus.emit", new=AsyncMock(side_effect=_capture)):
            await naukri_create_alert(name="Node Remote", keywords="node.js")
        assert len(emitted) == 1, emitted
        assert emitted[0].alert_name == "Node Remote"

    @pytest.mark.asyncio
    async def test_a_read_back_that_errors_does_not_manufacture_a_success(self):
        """If the list call itself fails we still do not know -- so we refuse."""
        from naukri_server.tools.alerts import naukri_create_alert
        with _patch_post({}), patch("naukri_server.tools.alerts._get_alerts_list",
                                    new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await naukri_create_alert(name="Node Remote", keywords="node.js")
        assert result["status"] == "error", result


class TestCreateAlertPostsTheMeasuredShape:

    @pytest.mark.asyncio
    async def test_posts_to_the_endpoint_that_was_watched_creating(self):
        """Pins the endpoint against a silent revert to the bundle-derived path.

        `/alertapi/v2/ssa` came from webpack analysis and was never seen to
        create anything; the live round recorded it as "Failed to fetch".
        """
        from naukri_server.tools.alerts import naukri_create_alert
        from naukri_server.config import JOB_ALERT_API
        assert JOB_ALERT_API == "/alertapi/v1/cja"
        post = AsyncMock(return_value=CREATED)
        with patch("naukri_server.interfaces.api_client.post", new=post), _patch_list([]):
            await naukri_create_alert(name="Node Remote", keywords="node.js")
        assert post.await_args.args[0] == "/alertapi/v1/cja"

    @pytest.mark.asyncio
    async def test_sends_both_keyword_spellings(self):
        """The measured working body used `keywords`; the bundle doc said `keyword`.

        Which one the server binds is unsettled -- the probing round recorded
        `keywords: null` on the created alert even having sent it -- so send
        both. An unknown extra key costs nothing; a missing one costs the
        keywords on a real alert.
        """
        from naukri_server.tools.alerts import naukri_create_alert
        post = AsyncMock(return_value=CREATED)
        with patch("naukri_server.interfaces.api_client.post", new=post), _patch_list([]):
            await naukri_create_alert(name="Node Remote", keywords="node.js")
        body = post.await_args.args[1]
        assert body["keyword"] == "node.js"
        assert body["keywords"] == "node.js"
        assert body["name"] == "Node Remote"
