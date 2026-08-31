"""The dashboard request shape, guarded against the reader that consumes it.

WHY THIS FILE EXISTS. Every pre-existing dashboard test mocks `api_client.get`
and feeds `_get_dashboard` a hand-built payload. That makes the whole suite
blind to the only thing that mattered: WHICH REQUEST the code actually issues.
A test that invents `{"profilePerformance": {"views": 12}}` passes forever
against an endpoint that has never sent a key by that name, and four fields the
operator reads went `None` for months underneath a green suite.

So these guards do not mock the payload. They assert on the REQUEST, and they
check the reader's key names against a REAL response captured from the live
endpoint on 2026-08-31 (`_sweep/dashboard-payloads.json`, and the probe that
captured it, `_sweep/dashboard_properties_probe.py`).

THE MEASUREMENT BEHIND THE FIX, so the next reader does not re-litigate it:

    bare request        -> 46 top-level keys, 3110 serialized bytes
    ?properties=<ten>   -> 31 top-level keys, 1883 serialized bytes

    (production keys) - (bare keys) = EMPTY. The parameter buys nothing.
    (bare keys) - (production keys) = 15 keys, four of which the reader named.

An UNRECOGNIZED property name is ignored and the endpoint falls back to the
full payload; a RECOGNIZED one narrows it. That asymmetry is the instrument,
and it is what proves the parameter binds rather than being ignored.
"""

import ast
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "naukri_server" / "services" / "profile_service.py"

# The live bare payload's top-level key set, captured 2026-08-31 through the
# same REST path production uses. Key NAMES only -- no values, no PII.
# Regenerate with `_sweep/dashboard_properties_probe.py`.
LIVE_BARE_KEYS = frozenset({
    'ca', 'cem_pcs', 'cvInfo', 'desig', 'designationId', 'desiredRole',
    'education', 'eligibleFlagForAIMockInterview', 'expectedCtc',
    'expectedCtcCurrency', 'firstNaukri', 'hasInboxFlag',
    'isAIResumeEligible', 'isPaidUser', 'isPremium', 'jbSearchStatus',
    'lastActiveDate', 'lookupData', 'modDtGtThanSixMonths', 'mod_dt', 'mr',
    'mrt', 'mvn', 'name', 'newLocationPrefId', 'organization', 'pc',
    'photoInfo', 'predictiveFuncArea', 'profileFlag', 'profileId',
    'profileSegment', 'profileViewCount', 'rawCtc', 'rawTotalExperience',
    'recruiterActionsLatestDate', 'res360NotifType', 'similarCompToFollow',
    'totalPowerNvite', 'totalSearchAppearancesCount',
    'totalSearchAppearancesLatestDate', 'unreadMostRelevantMail',
    'unreadPowerNvite', 'unseenOthersMailPresent', 'username',
    'videoProfile',
})

# The four the operator was reading as `null` and taking for an answer.
# `unread_invites` is unread RECRUITER invites on a live job search.
REPAIRED_BY_THE_BARE_CALL = {
    "unread_invites": "unreadPowerNvite",
    "total_invites": "totalPowerNvite",
    "unread_relevant_mail": "unreadMostRelevantMail",
    "total_matches": "mrt",
}


def _dashboard_func():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_get_dashboard":
                return node
    raise AssertionError("_get_dashboard not found in profile_service.py")


def _db_read_chains():
    """Every `safe_get(db, "k1", "k2", ...)` alias chain, as (lineno, keys)."""
    out = []
    for node in ast.walk(_dashboard_func()):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "safe_get"):
            continue
        if not node.args:
            continue
        if not (isinstance(node.args[0], ast.Name) and node.args[0].id == "db"):
            continue
        keys = [a.value for a in node.args[1:]
                if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if keys:
            out.append((node.lineno, tuple(keys)))
    return out


async def _run(dash: dict) -> dict:
    from naukri_server.services import profile_service as S
    with patch.object(S, "api_client") as client:
        client.get = AsyncMock(return_value={"dashBoard": dash})
        return await S._get_dashboard()


class TestTheRequestIsBare:
    """The narrowed request is the defect. Pin the shape, not the reader."""

    @pytest.mark.asyncio
    async def test_no_properties_parameter_is_sent(self):
        """MEASURED: the ten-property request is a strict SUBSET of the bare one.

        `(production keys) - (bare keys)` is empty, so the parameter costs
        1227 bytes of nothing and drops 15 keys, four of which are read a few
        lines below the call. Any `properties` value re-narrows the response
        and silently re-breaks those four.
        """
        from naukri_server.services import profile_service as S
        with patch.object(S, "api_client") as client:
            client.get = AsyncMock(return_value={"dashBoard": {}})
            await S._get_dashboard()

        assert client.get.await_count == 1
        _, kwargs = client.get.await_args
        params = kwargs.get("params") or {}
        assert "properties" not in params, (
            "the dashboard call is narrowing its own response again. A "
            "recognized property name narrows the payload; every field the "
            "reader names outside the requested groups then reads None with "
            "nothing said. Measured 2026-08-31: bare 46 keys, narrowed 31, "
            "and the narrowed set contains nothing the bare set lacks."
        )

    @pytest.mark.asyncio
    async def test_the_four_repaired_fields_resolve_from_a_real_payload(self):
        """Feed the REAL captured key set; the four must come back non-None."""
        payload = {k: "SENTINEL" for k in LIVE_BARE_KEYS}
        out = await _run(payload)
        broken = [name for name in REPAIRED_BY_THE_BARE_CALL if out.get(name) is None]
        assert broken == [], (
            "these read None against a payload carrying every key the live "
            "endpoint actually sends: %s" % broken
        )


class TestEveryDashboardReadIsReachable:
    """A key the reader names but the endpoint never sends is a silent None.

    This is the direction the pre-existing guard could not see. `test_dashboard
    .py::test_every_requested_property_is_either_parsed_or_passed_through`
    asserts nothing REQUESTED may be discarded; it is blind to a field READ
    that is never sent, which is the entire defect class here.
    """

    # MEASURED 2026-08-31 by `_sweep/dashboard_reader_census.py`: 38 of 59
    # alias chains in `_get_dashboard` name no key the live endpoint sends,
    # under ANY request shape. They are not this wave's four -- those are real
    # keys that the narrowing dropped. These are invented names, and several
    # have an obvious real counterpart sitting unread in the payload
    # (`desig` not `designation`, `organization` not `company`, `username`
    # not `email`, `mvn` not `phone`, `profileId` not `userId`).
    #
    # They are LEDGERED, not fixed: each needs a semantic call about which real
    # key it meant, which is a separate piece of work. This ledger exists so
    # the population cannot grow in silence -- add a 39th and this fails.
    ACKNOWLEDGED_UNREACHABLE = {
        "acceptedInvites", "acceptedNvite", "activeStatus", "activityStatus",
        "appliedCount", "appliedThisWeek", "appliedToday", "assessments",
        "city", "company", "currentCompany", "currentDesignation",
        "declinedInvites", "declinedNvite", "designation", "email", "emailId",
        "emailVerified", "imageUrl", "isEmailVerified", "isPhoneVerified",
        "isProfileVerified", "jobAlertsCount", "lastActive", "lastLoginDate",
        "location", "locationPref", "mobileNo", "mobileVerified",
        "noticePeriod", "pendingInvites", "pendingNvite", "phone", "photoUrl",
        "preferredLocations", "productName", "profileImageUrl",
        "profileQuality", "profileRank", "profileScore", "profileStrength",
        "profileTitle", "profileVerified", "profileViewsLastMonth",
        "profileViewsLastWeek", "profileViewsTrend", "recommendedJobsCount",
        "recruiterActionsCount", "resId", "resdexVisible", "savedJobsCount",
        "showProfile", "subscriptionEndDate", "subscriptionExpiry",
        "subscriptionType", "title", "totalApplied", "totalJobAlerts",
        "totalRecommendedJobs", "totalRecruiterActions", "totalSavedJobs",
        "totalUnreadMail", "unreadDirectMail", "unreadForwardedMail",
        "unreadMailCount", "userId", "viewsTrend",
    }

    def test_the_census_is_not_vacuous(self):
        """CONTROL. A census over zero chains passes for the wrong reason."""
        chains = _db_read_chains()
        assert len(chains) > 40, len(chains)
        assert len(LIVE_BARE_KEYS) == 46, len(LIVE_BARE_KEYS)

    def test_no_new_unreachable_dashboard_read(self):
        chains = _db_read_chains()
        unreachable = {
            k
            for _, keys in chains
            if not any(k in LIVE_BARE_KEYS for k in keys)
            for k in keys
        }
        new = sorted(unreachable - self.ACKNOWLEDGED_UNREACHABLE)
        assert new == [], (
            "these dashboard keys are read but appear in no live response: %s. "
            "The endpoint sends 46 top-level keys and none of these is among "
            "them, so each reads None on every call with nothing logged. "
            "Either name the key Naukri actually sends, or add it to "
            "ACKNOWLEDGED_UNREACHABLE with a reason." % new
        )

    def test_the_repaired_four_are_no_longer_unreachable(self):
        """The inverse: this wave's four must NOT be in the ledger."""
        for name in REPAIRED_BY_THE_BARE_CALL.values():
            assert name in LIVE_BARE_KEYS, name
            assert name not in self.ACKNOWLEDGED_UNREACHABLE, name


class TestPassThroughNamesRealKeys:
    """`requested_unparsed` may only name keys that occur as top-level keys.

    `profilePerformance` is a property NAME that expands into four FLAT fields
    (`profileViewCount`, `recruiterActionsLatestDate`,
    `totalSearchAppearancesCount`, `totalSearchAppearancesLatestDate`). It is
    a key in no response ever observed, so the branch reading it could not
    fire and `absent` listed it forever. `aiInterviewEligibility` is the same
    shape: the flag arrives as `eligibleFlagForAIMockInterview`.
    """

    @pytest.mark.asyncio
    async def test_pass_through_does_not_name_a_non_key(self):
        out = await _run({k: "SENTINEL" for k in LIVE_BARE_KEYS})
        block = out["requested_unparsed"]
        named = set(block["present"]) | set(block["absent"])
        # snake_case forms of names that are never keys
        offenders = named & {"profile_performance", "ai_interview_eligibility"}
        assert offenders == set(), (
            "the pass-through block names %s, which occur as top-level keys in "
            "no response the endpoint has ever produced. Their data arrives "
            "under other names and is already read. A branch that cannot fire "
            "reports 'absent' forever and reads as a gap that is not one."
            % sorted(offenders)
        )

    @pytest.mark.asyncio
    async def test_pass_through_only_names_real_top_level_keys(self):
        """Every name it CAN report must be a key the endpoint can send."""
        from naukri_server.services import profile_service as S
        out = await _run({k: "SENTINEL" for k in LIVE_BARE_KEYS})
        block = out["requested_unparsed"]
        camel = {
            "incomplete_section": "incompleteSection",
            "profile_segment": "profileSegment",
            "res360_notif_type": "res360NotifType",
            "campus_data": "campusData",
            "profile_performance": "profilePerformance",
            "ai_interview_eligibility": "aiInterviewEligibility",
        }
        # incompleteSection and campusData are RECOGNIZED property names that
        # are empty for this account (pc: 100, and he is not a campus hire) --
        # they are legitimately absent, not invented. profilePerformance and
        # aiInterviewEligibility are neither.
        legitimate = {"incompleteSection", "campusData",
                      "profileSegment", "res360NotifType"}
        for name in set(block["present"]) | set(block["absent"]):
            key = camel.get(name, name)
            assert key in LIVE_BARE_KEYS or key in legitimate, (
                "pass-through names %r, which is neither a live top-level key "
                "nor a recognized-but-empty property" % name
            )
