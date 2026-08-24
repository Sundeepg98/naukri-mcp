"""A missing count field must report UNKNOWN, never a confident zero.

MEASURED on the live account 2026-08-24 through the server's own read-only
GET (naukri_debug api_fetch), which is the probe the 08-24 sweep did NOT run:

    GET /cloudgateway-mynaukri/notification-center-services/v0
        /naukrinotificationcentre/user/self/count
      -> HTTP 200, content-length 11, body {"count": 8}
    naukri_notification_count() -> {"status": "success", "count": 8}

So the KEY IS CORRECT and the tool agrees with the endpoint. The sweep's
"count=0 while siblings see 4 to 11" did not reproduce, and the sweep document
already flags its own mechanism for that row as derived-from-reading-the-code
rather than probed.

What IS real is the latent hazard the sweep named: ``safe_get(data, "count",
default=0)`` was called with no ``field_name`` and no ``warn``, so if the key
ever went missing or drifted, the tool would answer a confident ``0`` with
``status: "success"``, no log line, and no error channel -- a zero that is
really "the field was not there". These tests pin the distinction.
"""

import pytest
from unittest.mock import AsyncMock, patch


async def _count_with_payload(payload):
    from naukri_server.tools.notifications import naukri_notification_count
    with patch("naukri_server.tools.notifications.api_client.get",
               new_callable=AsyncMock) as mock_get:
        mock_get.return_value = payload
        return await naukri_notification_count()


class TestAMissingFieldIsUnknownNotZero:

    @pytest.mark.asyncio
    async def test_absent_count_key_reports_unknown(self):
        result = await _count_with_payload({})
        assert result["count"] is None
        assert result["counted"] is False
        assert result["reason"]

    @pytest.mark.asyncio
    async def test_drifted_key_reports_unknown(self):
        """A renamed field is the same failure as an absent one."""
        result = await _count_with_payload({"unreadCount": 11})
        assert result["count"] is None
        assert result["counted"] is False

    @pytest.mark.asyncio
    async def test_non_dict_body_reports_unknown(self):
        result = await _count_with_payload("not a dict")
        assert result["count"] is None
        assert result["counted"] is False

    @pytest.mark.asyncio
    async def test_null_count_reports_unknown(self):
        result = await _count_with_payload({"count": None})
        assert result["count"] is None
        assert result["counted"] is False

    @pytest.mark.asyncio
    async def test_the_missing_field_is_logged_not_swallowed(self, caplog):
        """The anti-corruption layer's own warning must be switched ON here --
        a silent miss is how this went unnoticed for a whole sweep."""
        import logging
        with caplog.at_level(logging.WARNING):
            await _count_with_payload({})
        assert any("count" in r.getMessage() for r in caplog.records)


class TestARealMeasurementStillReadsAsOne:

    @pytest.mark.asyncio
    async def test_the_live_payload_shape_returns_its_number(self):
        """The exact body measured on 2026-08-24."""
        result = await _count_with_payload({"count": 8})
        assert result["status"] == "success"
        assert result["count"] == 8
        assert result["counted"] is True

    @pytest.mark.asyncio
    async def test_CONTROL_a_genuine_zero_is_still_a_zero(self):
        """The fix must not turn every zero into 'unknown' -- an endpoint
        that really says nothing is unread has MEASURED that."""
        result = await _count_with_payload({"count": 0})
        assert result["count"] == 0
        assert result["counted"] is True

    @pytest.mark.asyncio
    async def test_a_numeric_string_is_still_a_number(self):
        """Preserves the pre-fix behaviour for a gateway that stringifies."""
        result = await _count_with_payload({"count": "8"})
        assert result["count"] == 8
        assert result["counted"] is True
