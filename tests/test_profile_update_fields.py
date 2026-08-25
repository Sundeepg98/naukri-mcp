"""What naukri_update_profile can write, and what it still refuses.

This file pins the 2026-08-25 widening. Before it, the tool accepted FIVE
fields and rejected everything else at the front door -- while this same repo
already wrote sixteen profile sections through a verified REST route. The
refusal was stale against the codebase's own capability, and a second refusal
further down the same function ("Browser UI update not yet supported for:")
could not fire at all, because the front-door gate had already rejected
everything it tested for. CONTROL 1 pins that arithmetic so the dead branch
cannot come back.

Every check here was SHOWN FAILING before it was allowed to stay -- the
widening reverted, the confirm gate forced open, the abort-on-failure removed,
the per-unit report blanked -- and each one went red on the assertion named in
its docstring.

PURE. `write_section`'s two seams (the profile READ and the write TRANSPORT)
are replaced wholesale, exactly as tests/test_profile_restore.py does it, and
the browser leg is replaced with a stub. Nothing here opens a socket, a
browser, or a real profile.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from naukri_server.tools import profile_sections as psec
from naukri_server.tools import profile_update as pu

PROFILE_ID = "P-UPDATE"


def _profile(summary="OLD SUMMARY", headline="OLD HEADLINE"):
    """The READ shape: `profile` is an ARRAY OF ONE OBJECT."""
    return {
        "profile": [{
            "profileId": PROFILE_ID,
            "summary": summary,
            "resumeHeadline": headline,
            "keySkills": "Node.js,TypeScript",
            "entityRole": {"id": 11, "value": "Backend Developer"},
            "joinDate": "2026-01-01",
        }],
    }


class FakeNaukri:
    """The profile read plus the write transport. `writes` is the ground truth.

    Every "nothing was written" assertion in this file is made against
    `writes`, never against a returned status -- a broken gate would still
    return the status.
    """

    def __init__(self, profile):
        self.profile = deepcopy(profile)
        self.writes = []
        #: 1-based index of the write that should throw. None = never.
        self.raise_on_write = None

    async def get(self, url, params=None):
        return deepcopy(self.profile)

    async def write(self, body, profile_id=None):
        attempt = len(self.writes) + 1
        self.writes.append(deepcopy(body))
        if self.raise_on_write == attempt:
            raise RuntimeError("transport exploded on write %d" % attempt)
        for key, value in body.items():
            if key == "profile":
                self.profile["profile"][0].update(value)
        return {"status": "success"}


@pytest.fixture
def fake(monkeypatch):
    server = FakeNaukri(_profile())
    monkeypatch.setattr(psec, "api_client", SimpleNamespace(get=server.get))
    monkeypatch.setattr(psec, "fullprofiles_write", server.write)
    return server


@pytest.fixture
def no_browser(monkeypatch):
    """Replace the browser leg and record what it was asked to do.

    Returns the call log. `result` is what the stub hands back, so a test can
    make the browser leg fail without a browser.
    """
    log = {"calls": [], "result": None}

    async def stub(fields):
        log["calls"].append(dict(fields))
        if log["result"] is not None:
            return log["result"]
        return {
            "status": "updated", "method": "browser_ui",
            "updated_fields": sorted(fields), "api_confirmed": True,
            "message": "Profile updated via browser UI.",
        }

    monkeypatch.setattr(pu, "_browser_update", stub)
    return log


# ---------------------------------------------------------------------------
# CONTROL 1 - the refusal that was removed, and the dead branch behind it
# ---------------------------------------------------------------------------

class TestTheOldRefusalWasStaleAndOneHalfWasUnreachable:
    def test_the_front_door_now_admits_every_writable_scalar_field(self):
        """The measurement the widening rests on: the section layer owns 15
        scalar fields and the tool used to accept 5."""
        owned = set()
        for fields in psec.SCALAR_ALLOWED_FIELDS.values():
            owned |= set(fields)

        assert len(owned) == 15, sorted(owned)
        assert owned <= pu.UPDATABLE_FIELDS, sorted(owned - pu.UPDATABLE_FIELDS)
        assert pu.BROWSER_SUPPORTED_FIELDS <= pu.UPDATABLE_FIELDS
        assert len(pu.UPDATABLE_FIELDS) == 18, sorted(pu.UPDATABLE_FIELDS)

    def test_the_second_refusal_could_never_have_fired(self):
        """The branch this widening deleted read 'Browser UI update not yet
        supported for: ...' and tested `fields - browser_handled`. The gate
        above it admitted only UPDATABLE_FIELDS, so the branch could fire only
        for a field in UPDATABLE_FIELDS and NOT browser-handled. Before the
        widening that set was empty by construction.

        It is NOT empty now, which is the whole point -- so the modern
        statement of the same property is that every such field has a real
        route rather than a refusal."""
        reachable_but_not_browser = pu.UPDATABLE_FIELDS - pu.BROWSER_SUPPORTED_FIELDS
        assert reachable_but_not_browser, (
            "if this is empty the widening has been reverted")
        for field in reachable_but_not_browser:
            assert field in pu.REST_FIELD_SECTION, field

    def test_the_field_map_is_derived_from_the_spec_table_not_retyped(self):
        """A hand-copied list drifts. This one cannot: it is built from
        SCALAR_ALLOWED_FIELDS, so deleting a field there deletes it here."""
        for field, section in pu.REST_FIELD_SECTION.items():
            assert field in psec.SCALAR_ALLOWED_FIELDS[section]

    def test_three_fields_are_browser_only_because_no_spec_owns_them(self):
        """noticePeriod / currentCtc / expectedCtc are not a refusal, they are
        a route. No SectionSpec owns them, so the modal is the only measured
        way to write them and it stays."""
        assert pu.BROWSER_ONLY_FIELDS == frozenset(
            {"noticePeriod", "currentCtc", "expectedCtc"})
        for field in pu.BROWSER_ONLY_FIELDS:
            assert field not in pu.REST_FIELD_SECTION


# ---------------------------------------------------------------------------
# CONTROL 2 - the confirm gate on every NEW write path
# ---------------------------------------------------------------------------

class TestConfirmGatesEveryRestField:
    async def test_a_rest_field_writes_nothing_without_confirm(self, fake,
                                                               no_browser):
        out = await pu._update_profile({"summary": "NEW SUMMARY"})

        assert out["status"] == "preview"
        assert out["written"] is False
        assert fake.writes == [], (
            "the gate is measured at the transport, not read off the status")
        assert fake.profile["profile"][0]["summary"] == "OLD SUMMARY"
        assert no_browser["calls"] == []

    async def test_the_preview_carries_the_exact_body_that_would_be_sent(
            self, fake, no_browser):
        """The preview IS the thing an operator approves against, so it has to
        be the payload, not a description of one."""
        out = await pu._update_profile({"summary": "NEW SUMMARY"},
                                       confirm=False)

        assert [u["would_send"] for u in out["units"]] == [
            {"profile": {"summary": "NEW SUMMARY"}}]
        assert out["units"][0]["section"] == "summary"

    async def test_a_mixed_call_does_not_run_the_browser_leg_either(
            self, fake, no_browser):
        """Consent is for the CALL. A call naming a REST field writes nothing
        at all without confirm - not the REST half and not the browser half."""
        out = await pu._update_profile({"summary": "NEW", "noticePeriod": "1 Month"})

        assert out["status"] == "preview"
        assert fake.writes == []
        assert no_browser["calls"] == [], (
            "the browser leg ran on an unconfirmed call")
        assert out["browser_fields_that_would_be_edited"] == ["noticePeriod"]
        assert out["browser_field_values"] == {"noticePeriod": "1 Month"}

    async def test_a_preview_admits_a_unit_that_would_be_refused(self, fake,
                                                                  no_browser):
        """entityRole is a controlled-vocabulary field: the API takes a bare
        string and silently ignores it, so the section layer refuses one. A
        preview that still promised the write would be describing a confirmed
        run that is not going to happen - and the preview is the artefact the
        operator says yes to."""
        out = await pu._update_profile({"entityRole": "Backend Developer"})

        assert out["status"] == "preview"
        assert out["blocked_by"] == ["careerPreferences"]
        assert out["would_write_everything_asked"] is False
        assert "WARNING" in out["message"]
        assert out["units"][0]["outcome"] == "not_sent"
        assert fake.writes == []

    async def test_a_clean_preview_does_not_cry_wolf(self, fake, no_browser):
        """The control on the warning. A preview that flagged everything would
        train the reader to ignore it."""
        out = await pu._update_profile({"entityRole": {"id": 11, "value": "BE"}})

        assert out["blocked_by"] == []
        assert out["would_write_everything_asked"] is True
        assert "WARNING" not in out["message"]

    async def test_confirm_true_actually_writes(self, fake, no_browser):
        """CONTROL on the control. The gate above only means something because
        the confirmed path genuinely writes."""
        out = await pu._update_profile({"summary": "NEW SUMMARY"}, confirm=True)

        assert out["status"] == "updated", out
        assert fake.writes == [{"profile": {"summary": "NEW SUMMARY"}}]
        assert fake.profile["profile"][0]["summary"] == "NEW SUMMARY"
        assert out["updated_fields"] == ["summary"]

    async def test_a_browser_only_call_still_needs_no_confirm(self, fake,
                                                              no_browser):
        """ZERO REGRESSION. The five original fields keep their behaviour and
        their reply shape; adding a gate to them would have broken every
        existing caller."""
        out = await pu._update_profile({"noticePeriod": "1 Month"})

        assert out["status"] == "updated"
        assert out["method"] == "browser_ui"
        assert out["updated_fields"] == ["noticePeriod"]
        assert no_browser["calls"] == [{"noticePeriod": "1 Month"}]
        assert fake.writes == [], "a browser field must not take the REST route"


# ---------------------------------------------------------------------------
# CONTROL 3 - two sections are two writes, and a partial says so
# ---------------------------------------------------------------------------

class TestPartialWritesAreReportedNotHidden:
    async def test_two_sections_are_two_writes_in_a_stated_order(self, fake,
                                                                 no_browser):
        out = await pu._update_profile(
            {"summary": "NEW", "joinDate": "2027-01-01"}, confirm=True)

        assert out["status"] == "updated", out
        assert out["atomic"] is False, (
            "two writes are not atomic and the reply must never imply it")
        assert [u["section"] for u in out["units"]] == ["careerPreferences",
                                                        "summary"]
        assert len(fake.writes) == 2

    async def test_a_failing_second_write_reports_which_one_landed(
            self, fake, no_browser):
        """THE CASE THE RULING NAMED. The caller must be able to read off
        exactly which unit landed and which did not, and the reply must not
        say `updated`."""
        fake.raise_on_write = 2
        out = await pu._update_profile(
            {"summary": "NEW", "joinDate": "2027-01-01"}, confirm=True)

        assert out["status"] == "partial", out
        assert out["status"] != "updated"
        by_section = {u["section"]: u for u in out["units"]}
        assert by_section["careerPreferences"]["outcome"] == "landed"
        assert by_section["summary"]["outcome"] == "unknown", (
            "a transport exception cannot prove the server did not apply it")
        assert out["updated_fields"] == ["joinDate"]
        assert "unknown" in out["message"]

    async def test_a_failed_rest_leg_never_opens_the_browser(self, fake,
                                                             no_browser):
        """A browser tab on top of a half-written REST leg makes the damage
        wider and the report less true."""
        fake.raise_on_write = 1
        out = await pu._update_profile(
            {"summary": "NEW", "noticePeriod": "1 Month"}, confirm=True)

        assert out["status"] == "error"
        assert out["error_code"] == "PARTIAL_WRITE"
        assert out["written"] is False
        assert no_browser["calls"] == []
        assert out["browser_fields_not_attempted"] == ["noticePeriod"]

    async def test_a_failing_browser_leg_after_a_landed_rest_leg_is_partial(
            self, fake, no_browser):
        no_browser["result"] = {
            "status": "error", "error_code": "BROWSER_ERROR",
            "message": "Could not find edit button",
        }
        out = await pu._update_profile(
            {"summary": "NEW", "noticePeriod": "1 Month"}, confirm=True)

        assert out["status"] == "partial", out
        assert out["updated_fields"] == ["summary"]
        assert fake.profile["profile"][0]["summary"] == "NEW"
        outcomes = {u["route"]: u["outcome"] for u in out["units"]}
        assert outcomes == {"rest_section_write": "landed",
                            "browser_ui": "unknown"}


# ---------------------------------------------------------------------------
# CONTROL 4 - what is still refused, and the reason it carries
# ---------------------------------------------------------------------------

class TestTheRemainingRefusalsSayWhy:
    @pytest.mark.parametrize("field", sorted(pu.NO_WRITE_ROUTE_FIELDS))
    async def test_a_field_with_no_measured_route_is_refused_by_name(
            self, fake, no_browser, field):
        out = await pu._update_profile({field: "whatever"}, confirm=True)

        assert out["status"] == "error"
        assert out["error_code"] == "VALIDATION_ERROR"
        assert "Refused on purpose" in out["message"]
        assert field in out["message"]
        assert "mnj_v320.min.js" in out["message"], (
            "a refusal without its measurement is an opinion")
        assert fake.writes == []

    async def test_a_typo_is_told_apart_from_a_policy_refusal(self, fake,
                                                              no_browser):
        out = await pu._update_profile({"resumeHedline": "x"}, confirm=True)

        assert out["status"] == "error"
        assert "Not a writable profile field" in out["message"]
        assert "Refused on purpose" not in out["message"]
        assert fake.writes == []

    async def test_row_collections_are_pointed_at_the_right_tool(self, fake,
                                                                 no_browser):
        """A flat field dict has nowhere to put a row id, so employments
        cannot be written here - and the message has to say where it CAN."""
        out = await pu._update_profile({"employments": []}, confirm=True)

        assert out["status"] == "error"
        assert "naukri_update_profile_section" in out["message"]
        assert fake.writes == []

    async def test_no_fields_is_still_a_validation_error(self, fake,
                                                         no_browser):
        out = await pu._update_profile({})
        assert out["status"] == "error"
        assert out["error_code"] == "VALIDATION_ERROR"
        assert "No fields" in out["message"]


# ---------------------------------------------------------------------------
# CONTROL 5 - a browser leg that fails partway admits what it already saved
# ---------------------------------------------------------------------------

class TestABrowserFailureNamesWhatAlreadyLanded:
    def test_it_carries_the_fields_that_had_already_saved(self):
        """Each modal saves on its own; there is no rollback. The path this
        replaces returned the bare handler error and dropped the list, so a
        headline that saved followed by a keySkills failure reported a plain
        error with nothing in it saying the headline had changed."""
        out = pu._partial_browser_error(
            {"status": "error", "error_code": "BROWSER_ERROR",
             "message": "Failed to update keySkills"},
            ["resumeHeadline"])

        assert out["status"] == "error", "existing callers branch on this"
        assert out["partial"] is True
        assert out["updated_fields"] == ["resumeHeadline"]
        assert "resumeHeadline" in out["message"]
        assert "ALREADY been saved" in out["message"]

    def test_it_leaves_a_clean_failure_alone(self):
        """The control. If nothing had saved, nothing extra may be claimed."""
        error = {"status": "error", "error_code": "AUTH_ERROR",
                 "message": "Not logged in."}
        assert pu._partial_browser_error(error, []) == error


# ---------------------------------------------------------------------------
# CONTROL 6 - the write classifier grades all four outcomes
# ---------------------------------------------------------------------------

class TestClassifySectionWrite:
    @pytest.mark.parametrize("reply,expected", [
        ({"status": "updated"}, "landed"),
        ({"status": "preview"}, "preview"),
        ({"status": "error", "error_code": "VALIDATION_ERROR"}, "not_sent"),
        ({"status": "error", "error_code": "COLLATERAL_CHANGE"},
         "landed_with_collateral"),
        ({"status": "error", "error_code": "NOT_PERSISTED"}, "unknown"),
        ({"status": "error", "error_code": "API_ERROR"}, "unknown"),
    ])
    def test_each_reply_grades_to_its_honest_outcome(self, reply, expected):
        assert pu.classify_section_write(reply) == expected

    def test_an_unrecognised_reply_defaults_to_unknown_not_to_success(self):
        """The direction of the default matters more than the default. An
        unreadable reply must never grade as landed."""
        assert pu.classify_section_write({}) == "unknown"
        assert pu.classify_section_write({"status": "weird"}) == "unknown"
