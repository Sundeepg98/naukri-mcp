"""naukri_get_application's status_timeline read none of the keys the API sends.

Captured live 2026-08-22 from
`/cloudgateway-apply/whtma-services/v0/applyapi/v3/history-description`
for his InApp "MCP Developer" application. The API sends `statusValue`,
`dateTime`, `count`, `statusId`, `modifiedDate`. The parser read `status` /
`label`, `date`, `description`, `isCompleted`, `isCurrent`, `stepOrder`,
`subStatus` -- not one of which exists in the payload.

So the tool returned

    "status_timeline": [{"status": ""}, {"status": ""}, {"status": ""}]

Three well-formed rows carrying nothing. That is worse than an error: the shape
is right, the length is right, and a caller has no way to tell it apart from an
application that genuinely has three unnamed steps. It threw away a real
timeline ending in "Application Viewed" with count 6 -- a recruiter had opened
his application six times, and the tool whose job is to report that said "".
"""

import pytest

from naukri_server.domain.application_detail import _parse_timeline

# Verbatim from the live response, unedited.
LIVE_STEPS = [
    {"statusId": 1, "appId": None, "count": 1,
     "dateTime": "2026-08-21 15:04:58", "statusValue": "Applied",
     "modifiedDate": "2026-08-21 15:36:49"},
    {"statusId": 2, "appId": 107, "count": 1,
     "dateTime": "2026-08-21 15:04:58", "statusValue": "Application Sent",
     "modifiedDate": "2026-08-21 15:04:58"},
    {"statusId": 4, "appId": 107, "count": 6,
     "dateTime": "2026-08-21 15:21:26", "statusValue": "Application Viewed",
     "modifiedDate": "2026-08-21 15:36:49"},
]


class TestTheLivePayload:
    def test_every_step_is_named(self):
        """THE regression: three empty strings before the fix."""
        out = _parse_timeline(LIVE_STEPS)
        assert [s["status"] for s in out] == [
            "Applied", "Application Sent", "Application Viewed",
        ]

    def test_no_step_is_blank(self):
        out = _parse_timeline(LIVE_STEPS)
        blanks = [i for i, s in enumerate(out) if not s["status"]]
        assert not blanks, "steps %s came back unnamed" % blanks

    def test_the_view_count_survives(self):
        """`count` had no field at all -- it is how many times a recruiter
        opened the application, and it is the most actionable number here."""
        out = _parse_timeline(LIVE_STEPS)
        assert out[2]["count"] == 6

    def test_dates_survive(self):
        out = _parse_timeline(LIVE_STEPS)
        assert out[0]["date"] == "2026-08-21 15:04:58"
        assert out[2]["date"] == "2026-08-21 15:21:26"
        assert out[2]["modified_date"] == "2026-08-21 15:36:49"

    def test_status_id_survives(self):
        out = _parse_timeline(LIVE_STEPS)
        assert [s["status_id"] for s in out] == [1, 2, 4]


class TestTheLegacyShapeStillWorks:
    """This parser also serves older v0/v3 routes. Nothing may regress."""

    def test_legacy_keys_are_still_read(self):
        out = _parse_timeline([
            {"status": "Shortlisted", "date": "2026-01-01",
             "description": "d", "isCompleted": True, "isCurrent": False,
             "stepOrder": 3, "subStatus": "sub"},
        ])
        assert out[0] == {
            "status": "Shortlisted", "date": "2026-01-01", "description": "d",
            "is_completed": True, "is_current": False, "step_order": 3,
            "sub_status": "sub",
        }

    def test_label_fallback_is_still_honoured(self):
        assert _parse_timeline([{"label": "Sent"}])[0]["status"] == "Sent"

    def test_statusValue_wins_over_the_legacy_keys(self):
        out = _parse_timeline([{"statusValue": "New", "status": "Old"}])
        assert out[0]["status"] == "New"


class TestTheEmptyCases:
    def test_no_steps_gives_no_timeline(self):
        assert _parse_timeline([]) == []

    def test_a_genuinely_unnamed_step_is_still_empty(self):
        """CONTROL: the fix must not invent a name. A step with none of the
        known keys still reports "" -- that is honest, and it is what makes the
        three-blank-rows failure distinguishable from real data."""
        assert _parse_timeline([{"unknownKey": 1}])[0]["status"] == ""
