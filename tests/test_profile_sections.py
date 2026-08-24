"""The profile-section write orchestrator, pinned. Fixtures only -- no network.

`naukri_server.tools.profile_sections.write_section` writes ONE profile section
and then proves it landed: read-before, snapshot, build body, POST, read-after,
`verify_write`, `rows_lost`. This file pins the seventeen behaviours that make
that sequence worth running.

ZERO NETWORK, ZERO WRITES AGAINST naukri.com. Three seams are mocked and
nothing else: the profile READ (`api_client` in the module namespace), the
TRANSPORT (`fullprofiles_write`), and the event bus emit. The snapshot layer
runs FOR REAL against a tmp_path directory, because "a snapshot was taken
before the write" is part of the behaviour under test, not scaffolding.

THE CONTROL THAT MATTERS MOST IS 3. Six collections are sent to Naukri as
WHOLE LISTS: the editor puts every row on the wire every time, so a payload
carrying only the edited row DELETES the rest, and the API still answers 200.
The orchestrator's whole reason to exist is reading the live list and merging
into it. Control 3 is the test that notices if that merge is ever lost.

EVERY CONTROL HERE WAS SHOWN FAILING FIRST. For each one the module was broken
deliberately -- the whole-list merge replaced with the caller's row alone, the
verify result ignored, the vocabulary guard neutered, the scalar-scope guard
neutered, an id_field dropped from the spec table, and so on -- the single
target test was run, the RED message was recorded, and the source was restored
byte-for-byte and re-hashed. The full RED table and the before/after SHA-256 of
`profile_sections.py` are in
`_audit/_slices/naukri-section-tests-slice.md`. A check never observed failing
certifies nothing.

TWO DEFECTS IN THE MODULE UNDER TEST were found while writing this file and
were first recorded here as strict xfails rather than worked around. Both have
since been fixed in `profile_sections.py`; the strict markers did their job by
turning into hard failures the moment the repair landed, and the tests remain
at the bottom of this file as plain regression tests for that fix.
"""

import copy
import json

import pytest
from unittest.mock import AsyncMock

from naukri_server.config import PROFILE_API
from naukri_server.tools import profile_sections as psec
from naukri_server.tools import profile_snapshot as ps
from naukri_server.tools.profile_snapshot import WHOLE_LIST_SECTIONS
from naukri_server.tools.profile_write import WHOLE_LIST_COLLECTIONS

# A stand-in for the 64-character live id. NEVER put the real one in a test
# file -- it is account-identifying. Nothing here reaches the wire anyway.
PROFILE_ID = "0" * 64


# ---------------------------------------------------------------------------
# Fixture profiles, in the READ spelling the API actually returns.
# `profileId` is stamped on every row on purpose: the live payload does that,
# which is why a row-id detector cannot just take the first *Id key it sees.
# No personal data -- synthetic labels only.
# ---------------------------------------------------------------------------

def _language(row_id, name, proficiency="Beginner"):
    return {"languageId": row_id, "profileId": PROFILE_ID,
            "language": name, "proficiency": proficiency}


def _employment(row_id, org, designation):
    return {"employmentId": row_id, "profileId": PROFILE_ID,
            "organization": org, "designation": designation}


def _online(row_id, kind, url):
    return {"onlineProfileId": row_id, "profileId": PROFILE_ID,
            "profileType": kind, "url": url}


def base_profile():
    """Five languages, three employments, two online profiles, one scalar row.

    Five is deliberate: control 3 asserts that four untouched rows survive a
    write that edits the fifth, and four is enough that an off-by-one merge
    cannot pass by accident.
    """
    return {
        "profile": [{
            "profileId": PROFILE_ID,
            "resumeHeadline": "Backend engineer",
            "summary": "Builds services.",
            "keySkills": "Node.js,TypeScript",
        }],
        "languages": [
            _language("L1", "Lang-One", "Expert"),
            _language("L2", "Lang-Two", "Proficient"),
            _language("L3", "Lang-Three", "Beginner"),
            _language("L4", "Lang-Four", "Proficient"),
            _language("L5", "Lang-Five", "Expert"),
        ],
        "employments": [
            _employment("E1", "Org-One", "Engineer"),
            _employment("E2", "Org-Two", "Senior Engineer"),
            _employment("E3", "Org-Three", "Lead Engineer"),
        ],
        # The READ spelling of the `onlineProfiles` write key. The singular /
        # plural asymmetry is real and is exactly what resolve_section exists
        # for, so the fixture uses the spelling the API returns.
        "onlineProfile": [
            _online("O1", "Portfolio", "url-one"),
            _online("O2", "Repo", "url-two"),
        ],
    }


def canonical(value):
    """Byte-exact canonical form, for 'this row came back unchanged' asserts."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# The rig. Three seams, no network, and a read queue that refuses to serve a
# read the test did not arm -- an unarmed read is a behaviour change, not a
# convenience.
# ---------------------------------------------------------------------------

class _Reads:
    """Stands in for `api_client`, serving the queued profile reads."""

    def __init__(self):
        self.queue = []
        self.calls = []

    async def get(self, path, params=None, **kwargs):
        self.calls.append((path, params))
        if not self.queue:
            raise AssertionError(
                "write_section read the profile more times than the test "
                "armed (%d read(s) already served)" % len(self.calls))
        return self.queue.pop(0)


class Rig:
    """One wired call: queued reads, a mocked transport, zero network."""

    def __init__(self, monkeypatch):
        self.reads = _Reads()
        self.transport = AsyncMock(return_value={"status": 200})
        monkeypatch.setattr(psec, "api_client", self.reads)
        monkeypatch.setattr(psec, "fullprofiles_write", self.transport)

    def arm(self, *profiles):
        """Queue the reads this call is allowed to make, in order.

        Deep-copied, so the profile the module sees is a different object from
        the one the test compares against. That is what makes "byte-identical
        to what was read" a real assertion rather than an identity check.
        """
        self.reads.queue = [copy.deepcopy(p) for p in profiles]
        return self

    @property
    def sent(self):
        """The `section_body` the transport was handed, exactly once."""
        assert self.transport.await_count == 1, (
            "expected exactly one transport call, got %d"
            % self.transport.await_count)
        args, kwargs = self.transport.call_args
        return kwargs.get("section_body", args[0] if args else None)


@pytest.fixture(autouse=True)
def snapshot_home(monkeypatch, tmp_path):
    """Redirect the snapshot directory off the repo for every test here.

    DATA_DIR is the repo root, so an un-isolated snapshot dirties the checkout
    with a file full of live personal data. Same treatment
    tests/test_profile_snapshot.py gives it, for the same reason.
    """
    target = tmp_path / "profile_snapshots"
    monkeypatch.setattr(ps, "_SNAPSHOT_DIR", target)
    return target


@pytest.fixture(autouse=True)
def no_event_fanout(monkeypatch):
    """Record the ProfileUpdated emit; never dispatch it.

    The live `ProfileUpdated` subscriber writes a notification row into
    naukri.db. A unit test must not, so the bus is stubbed and the emit is
    merely observable.
    """
    import naukri_server.events as events
    emit = AsyncMock(return_value=None)
    monkeypatch.setattr(events.event_bus, "emit", emit)
    return emit


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


# ===========================================================================
# CONTROL 1 -- preview writes nothing
# ===========================================================================

class TestPreviewWritesNothing:
    """confirm=False must be inert. It is the only safe way to read a payload."""

    async def test_preview_never_reaches_the_transport(self, rig):
        rig.arm(base_profile())

        result = await psec.write_section(
            "languages", {"languageId": "L3", "proficiency": "Expert"})

        assert result["status"] == "preview"
        assert rig.transport.await_count == 0, (
            "confirm=False wrote to the transport %d time(s). A preview that "
            "writes is worse than no preview." % rig.transport.await_count)

    async def test_preview_leaves_nothing_on_disk(self, rig, snapshot_home):
        rig.arm(base_profile())

        await psec.write_section("summary", {"summary": "New summary."})

        assert not snapshot_home.exists() or not list(snapshot_home.iterdir()), (
            "a preview took a pre-write snapshot; nothing should be persisted "
            "for a call that does not write")

    async def test_the_read_asks_for_expand_level_4(self, rig):
        """The profile endpoint is a hard HTTP 400 without expand_level."""
        rig.arm(base_profile())

        await psec.write_section("summary", {"summary": "New summary."})

        assert rig.reads.calls == [(PROFILE_API, {"expand_level": "4"})]


# ===========================================================================
# CONTROL 2 -- the preview body is exactly what a confirmed call sends
# ===========================================================================

class TestPreviewMatchesTheConfirmedBody:
    """A preview that does not show the real payload is a lie with a UI."""

    @pytest.mark.parametrize("section,fields", [
        ("summary", {"summary": "New summary."}),
        ("employments", {"employmentId": "E2", "designation": "Staff Engineer"}),
        ("languages", {"languageId": "L3", "proficiency": "Expert"}),
    ])
    async def test_would_send_equals_what_the_transport_receives(
            self, rig, section, fields):
        before = base_profile()

        rig.arm(before)
        preview = await psec.write_section(section, dict(fields))

        rig.arm(before, before)
        await psec.write_section(section, dict(fields), confirm=True)

        assert canonical(preview["would_send"]) == canonical(rig.sent), (
            "the previewed body and the sent body differ for %r. Preview:\n%s\n"
            "Sent:\n%s" % (section, canonical(preview["would_send"]),
                           canonical(rig.sent)))


# ===========================================================================
# CONTROL 3 -- WHOLE_LIST merge preserves every other row
#
# The single most important test in this file. Get this wrong and rows are
# gone from the live profile, with a 200 on the response to say it went fine.
# ===========================================================================

class TestWholeListMergePreservesTheOtherRows:

    async def test_updating_one_of_five_rows_sends_all_five(self, rig):
        before = base_profile()
        rig.arm(before, before)

        await psec.write_section(
            "languages", {"languageId": "L3", "proficiency": "Expert"},
            confirm=True)

        rows = rig.sent["languages"]
        assert len(rows) == 5, (
            "a whole-list write put %d row(s) on the wire; the live list has "
            "5. Naukri replaces the whole collection with what it receives, so "
            "this drops %d row(s)." % (len(rows), 5 - len(rows)))

    async def test_the_four_untouched_rows_are_byte_identical(self, rig):
        before = base_profile()
        rig.arm(before, before)

        await psec.write_section(
            "languages", {"languageId": "L3", "proficiency": "Expert"},
            confirm=True)

        sent = {r["languageId"]: r for r in rig.sent["languages"]}
        original = {r["languageId"]: r for r in before["languages"]}
        for row_id in ("L1", "L2", "L4", "L5"):
            assert canonical(sent[row_id]) == canonical(original[row_id]), (
                "row %s was altered on its way to the wire. Read:\n%s\n"
                "Sent:\n%s" % (row_id, canonical(original[row_id]),
                               canonical(sent[row_id])))

    async def test_the_targeted_row_is_merged_not_replaced(self, rig):
        """An update is a merge: the fields not named keep their values."""
        before = base_profile()
        rig.arm(before, before)

        await psec.write_section(
            "languages", {"languageId": "L3", "proficiency": "Expert"},
            confirm=True)

        target = [r for r in rig.sent["languages"]
                  if r["languageId"] == "L3"][0]
        assert target["proficiency"] == "Expert"
        assert target["language"] == "Lang-Three", (
            "the merge dropped a field the caller did not name; an update is "
            "a merge over the live row, not a replacement of it")

    async def test_row_order_is_preserved(self, rig):
        before = base_profile()
        rig.arm(before, before)

        await psec.write_section(
            "languages", {"languageId": "L1", "proficiency": "Beginner"},
            confirm=True)

        assert [r["languageId"] for r in rig.sent["languages"]] == [
            "L1", "L2", "L3", "L4", "L5"]

    async def test_the_read_spelling_of_a_section_is_found(self, rig):
        """`onlineProfiles` is written plural and READ singular.

        If the merge cannot find the live list under the read spelling it sees
        an empty collection and sends one row -- which deletes the rest.
        """
        before = base_profile()
        rig.arm(before, before)

        await psec.write_section(
            "onlineProfiles", {"onlineProfileId": "O1", "url": "url-new"},
            confirm=True)

        assert len(rig.sent["onlineProfiles"]) == 2, (
            "the live list was read under the wrong spelling, so the merge "
            "started from empty and the other row(s) are dropped")


# ===========================================================================
# CONTROL 4 -- WHOLE_LIST create appends
# ===========================================================================

class TestWholeListCreateAppends:

    async def test_a_new_row_makes_six_and_leaves_five_intact(self, rig):
        before = base_profile()
        rig.arm(before, before)

        await psec.write_section(
            "languages", {"language": "Lang-Six", "proficiency": "Beginner"},
            confirm=True)

        rows = rig.sent["languages"]
        assert len(rows) == 6, (
            "creating a whole-list row sent %d row(s); 5 live + 1 new = 6"
            % len(rows))
        sent = {r["languageId"]: r for r in rows if "languageId" in r}
        original = {r["languageId"]: r for r in before["languages"]}
        for row_id in original:
            assert canonical(sent[row_id]) == canonical(original[row_id]), (
                "creating a row altered existing row %s" % row_id)

    async def test_the_new_row_is_last_and_carries_no_id(self, rig):
        before = base_profile()
        rig.arm(before, before)

        await psec.write_section(
            "languages", {"language": "Lang-Six", "proficiency": "Beginner"},
            confirm=True)

        new = rig.sent["languages"][-1]
        assert "languageId" not in new
        assert new["language"] == "Lang-Six"


# ===========================================================================
# CONTROL 5 -- a WHOLE_LIST id matching no live row is refused
#
# Silently appending would orphan the intended edit: the caller believes it
# updated a row, and a duplicate is created instead.
# ===========================================================================

class TestWholeListUnknownIdIsRefused:

    async def test_an_unmatched_id_is_an_error_not_an_append(self, rig):
        rig.arm(base_profile())

        result = await psec.write_section(
            "languages", {"languageId": "L99", "proficiency": "Expert"})

        assert result["status"] == "error", (
            "an id matching no live row returned %r; silently appending "
            "orphans the edit the caller meant to make" % result.get("status"))
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "L99" in result["message"], (
            "the refusal must name the id that did not match: %r"
            % result["message"])

    async def test_an_unmatched_id_never_reaches_the_transport(self, rig):
        rig.arm(base_profile(), base_profile())

        await psec.write_section(
            "languages", {"languageId": "L99", "proficiency": "Expert"},
            confirm=True)

        assert rig.transport.await_count == 0, (
            "a refused write still went to the wire")


# ===========================================================================
# CONTROL 6 -- SINGLE_ROW sends a one-element array and does not read-merge
# ===========================================================================

class TestSingleRowSendsOneRow:

    async def test_the_payload_is_exactly_one_row(self, rig):
        rig.arm(base_profile(), base_profile())

        await psec.write_section(
            "employments", {"employmentId": "E2", "designation": "Staff"},
            confirm=True)

        rows = rig.sent["employments"]
        assert isinstance(rows, list)
        assert len(rows) == 1, (
            "a single-row section put %d row(s) on the wire. The server "
            "matches on the id and merges; the other rows are neither sent "
            "nor affected." % len(rows))

    async def test_the_collection_is_not_read_merged(self, rig):
        """The body must carry the caller's fields and nothing borrowed."""
        rig.arm(base_profile(), base_profile())

        fields = {"employmentId": "E2", "designation": "Staff"}
        await psec.write_section("employments", dict(fields), confirm=True)

        assert canonical(rig.sent["employments"][0]) == canonical(fields), (
            "the single-row payload picked up fields from the live read. It "
            "must be the caller's row verbatim: %s"
            % canonical(rig.sent["employments"][0]))


# ===========================================================================
# CONTROL 7 -- create vs update routing
# ===========================================================================

class TestCreateVersusUpdateRouting:

    @pytest.mark.parametrize("section,fields,expected", [
        ("employments", {"employmentId": "E2", "designation": "Staff"}, "update"),
        ("employments", {"organization": "Org-New", "startDate": "2020-01-01",
                         "employmentType": "full-time"}, "create"),
        ("languages", {"languageId": "L3", "proficiency": "Expert"}, "update"),
        ("languages", {"language": "Lang-Six"}, "create"),
    ])
    async def test_the_id_field_decides_the_action(
            self, rig, section, fields, expected):
        rig.arm(base_profile())

        result = await psec.write_section(section, fields)

        assert result["action"] == expected, (
            "%r with fields %s routed as %r, expected %r. Presence of the "
            "spec's id_field is what separates a create from an update."
            % (section, sorted(fields), result["action"], expected))

    async def test_the_action_survives_into_the_confirmed_result(self, rig):
        before = base_profile()
        rig.arm(before, before)

        result = await psec.write_section(
            "employments", {"employmentId": "E2", "designation": "Staff"},
            confirm=True)

        assert result["action"] == "update"


# ===========================================================================
# CONTROL 8 -- required-on-create is enforced only when creating
# ===========================================================================

class TestRequiredOnCreate:

    async def test_a_create_missing_required_fields_is_refused(self, rig):
        rig.arm(base_profile())

        result = await psec.write_section(
            "employments", {"designation": "Staff"})

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        for field in ("organization", "startDate", "employmentType"):
            assert field in result["message"], (
                "the refusal must name every missing required field; %r is "
                "absent from %r" % (field, result["message"]))

    async def test_a_create_missing_required_fields_never_reads_or_writes(
            self, rig):
        rig.arm()

        result = await psec.write_section(
            "employments", {"designation": "Staff"}, confirm=True)

        assert result["status"] == "error"
        assert rig.reads.calls == [], (
            "a payload refused on shape still read the live profile")
        assert rig.transport.await_count == 0

    async def test_an_update_is_not_held_to_the_create_requirements(self, rig):
        """An update is a merge; requiring create-only fields would break it."""
        rig.arm(base_profile())

        result = await psec.write_section(
            "employments", {"employmentId": "E2", "designation": "Staff"})

        assert result["status"] == "preview", (
            "an update was refused for missing create-only fields: %r"
            % result.get("message"))

    async def test_a_create_with_every_required_field_passes(self, rig):
        rig.arm(base_profile())

        result = await psec.write_section("employments", {
            "organization": "Org-New", "startDate": "2020-01-01",
            "employmentType": "full-time"})

        assert result["status"] == "preview"
        assert result["action"] == "create"


# ===========================================================================
# CONTROL 9 -- a verified write returns "updated"
# ===========================================================================

class TestVerifiedWriteReportsUpdated:

    async def test_a_scalar_write_that_lands_reports_updated(self, rig):
        before = base_profile()
        after = copy.deepcopy(before)
        after["profile"][0]["summary"] = "Rewritten summary."
        rig.arm(before, after)

        result = await psec.write_section(
            "summary", {"summary": "Rewritten summary."}, confirm=True)

        assert result["status"] == "updated", (
            "a write confirmed in the re-read reported %r (%s)"
            % (result.get("status"), result.get("message")))
        assert result["verified"] is True

    async def test_a_verified_write_emits_nothing_alarming(self, rig):
        before = base_profile()
        after = copy.deepcopy(before)
        after["profile"][0]["summary"] = "Rewritten summary."
        rig.arm(before, after)

        result = await psec.write_section(
            "summary", {"summary": "Rewritten summary."}, confirm=True)

        assert result["collateral"] == [], (
            "an isolated scalar edit reported collateral: %r"
            % result["collateral"])
        assert result["rows_lost"] == []
        assert "error_code" not in result

    async def test_a_verified_whole_list_write_reports_updated(self, rig):
        before = base_profile()
        after = copy.deepcopy(before)
        after["languages"][2]["proficiency"] = "Expert"
        rig.arm(before, after)

        result = await psec.write_section(
            "languages", {"languageId": "L3", "proficiency": "Expert"},
            confirm=True)

        assert result["status"] == "updated", result.get("message")
        assert result["rows_lost"] == []
        assert result["collateral"] == []

    async def test_a_verified_write_takes_a_snapshot_before_writing(
            self, rig, snapshot_home):
        before = base_profile()
        after = copy.deepcopy(before)
        after["profile"][0]["summary"] = "Rewritten summary."
        rig.arm(before, after)

        await psec.write_section(
            "summary", {"summary": "Rewritten summary."}, confirm=True)

        files = sorted(p.name for p in snapshot_home.iterdir())
        assert len(files) == 1, (
            "expected exactly one pre-write snapshot, found %r" % files)
        assert files[0].startswith("profile-")


# ===========================================================================
# CONTROL 10 -- an unverified write is an error, not a success
#
# The module's core promise. A 200 that did not persist must never report
# success: the response cannot tell you, and only the re-read can.
# ===========================================================================

class TestUnverifiedWriteIsNotPersisted:

    async def test_a_write_the_reread_does_not_show_is_an_error(self, rig):
        before = base_profile()
        after = copy.deepcopy(before)          # the change did NOT land
        rig.arm(before, after)

        result = await psec.write_section(
            "summary", {"summary": "Rewritten summary."}, confirm=True)

        assert result["status"] == "error", (
            "the re-read does not contain the change, yet the write reported "
            "%r. A 200 is acceptance, not persistence."
            % result.get("status"))
        assert result["error_code"] == "NOT_PERSISTED"
        assert result["verified"] is False

    async def test_the_transport_did_run_and_still_the_answer_is_error(
            self, rig):
        """The write happened. That is exactly why the verdict matters."""
        before = base_profile()
        rig.arm(before, copy.deepcopy(before))

        result = await psec.write_section(
            "summary", {"summary": "Rewritten summary."}, confirm=True)

        assert rig.transport.await_count == 1
        assert result["error_code"] == "NOT_PERSISTED"
        assert "restore" in result["message"].lower(), (
            "an unverifiable write must tell the caller how to recover: %r"
            % result["message"])

    async def test_a_snapshot_exists_on_disk_for_the_failed_write(
            self, rig, snapshot_home):
        before = base_profile()
        rig.arm(before, copy.deepcopy(before))

        await psec.write_section(
            "summary", {"summary": "Rewritten summary."}, confirm=True)

        files = sorted(p.name for p in snapshot_home.iterdir())
        assert len(files) == 1, (
            "an unverifiable write left no snapshot to restore from: %r"
            % files)


# ===========================================================================
# CONTROL 11 -- collateral damage is surfaced
# ===========================================================================

class TestCollateralDamageIsSurfaced:

    def _after_with_l5_gone(self):
        before = base_profile()
        after = copy.deepcopy(before)
        after["languages"][2]["proficiency"] = "Expert"   # the intended change
        after["languages"] = [r for r in after["languages"]
                              if r["languageId"] != "L5"]  # and a casualty
        return before, after

    async def test_the_intended_change_landing_does_not_excuse_a_lost_row(
            self, rig):
        before, after = self._after_with_l5_gone()
        rig.arm(before, after)

        result = await psec.write_section(
            "languages", {"languageId": "L3", "proficiency": "Expert"},
            confirm=True)

        assert result["verified"] is True, (
            "precondition: the intended change is present in the re-read")
        assert result["status"] == "error", (
            "a row vanished and the write reported %r" % result.get("status"))
        assert result["error_code"] == "COLLATERAL_CHANGE"

    async def test_rows_lost_names_the_vanished_id(self, rig):
        before, after = self._after_with_l5_gone()
        rig.arm(before, after)

        result = await psec.write_section(
            "languages", {"languageId": "L3", "proficiency": "Expert"},
            confirm=True)

        assert result["rows_lost"] == ["L5"], (
            "the vanished row must be named, not merely counted: %r"
            % result["rows_lost"])
        assert result["collateral"], (
            "a removed row must appear as collateral; a removal is never "
            "accounted for by the expected write")


# ===========================================================================
# CONTROL 12 -- controlled-vocabulary guard
#
# The API accepts a bare string here and then does not apply it: a 200 that
# changes nothing. Refusing turns a silent no-op into a clear error.
# ===========================================================================

class TestControlledVocabularyGuard:

    async def test_a_bare_string_in_a_vocab_field_is_refused(self, rig):
        rig.arm(base_profile())

        result = await psec.write_section(
            "careerPreferences", {"entityDepartment": "Engineering"})

        assert result["status"] == "error", (
            "a bare string in a controlled-vocabulary field returned %r. The "
            "API accepts it and silently ignores it."
            % result.get("status"))
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "entityDepartment" in result["message"]

    async def test_the_object_form_is_accepted(self, rig):
        rig.arm(base_profile())

        result = await psec.write_section(
            "careerPreferences",
            {"entityDepartment": {"id": 1, "value": "Engineering"}})

        assert result["status"] == "preview", (
            "the {'id': .., 'value': ..} form is what the editor sends and "
            "must be accepted: %r" % result.get("message"))
        assert result["would_send"]["profile"]["entityDepartment"] == {
            "id": 1, "value": "Engineering"}

    async def test_a_bare_vocab_field_never_reaches_the_transport(self, rig):
        rig.arm(base_profile(), base_profile())

        await psec.write_section(
            "careerPreferences", {"entityDepartment": "Engineering"},
            confirm=True)

        assert rig.transport.await_count == 0


# ===========================================================================
# CONTROL 13 -- scalar scope guard
#
# All four scalar sections share the `profile` envelope, so the section name
# is the ONLY thing scoping the write. Without this, asking to write the
# summary and passing resumeHeadline rewrites the headline, snapshots it under
# the wrong label, and verifies clean.
# ===========================================================================

class TestScalarScopeGuard:

    async def test_a_section_may_not_write_another_sections_field(self, rig):
        rig.arm(base_profile())

        result = await psec.write_section("summary", {"resumeHeadline": "x"})

        assert result["status"] == "error", (
            "write_section('summary', {'resumeHeadline': ...}) returned %r. "
            "Every scalar section shares the 'profile' envelope, so nothing "
            "else stops this from rewriting the headline."
            % result.get("status"))
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "resumeHeadline" in result["message"]

    async def test_a_section_writing_its_own_field_is_allowed(self, rig):
        rig.arm(base_profile())

        result = await psec.write_section("summary", {"summary": "Mine."})

        assert result["status"] == "preview"
        assert result["would_send"] == {"profile": {"summary": "Mine."}}

    async def test_a_stray_field_never_reaches_the_transport(self, rig):
        rig.arm(base_profile(), base_profile())

        await psec.write_section(
            "summary", {"summary": "Mine.", "keySkills": "Go"}, confirm=True)

        assert rig.transport.await_count == 0, (
            "a mixed payload reached the wire and would have rewritten a "
            "field belonging to another section")


# ===========================================================================
# CONTROL 14 -- unknown section
# ===========================================================================

class TestUnknownSection:

    async def test_an_unknown_section_is_rejected_with_the_valid_list(
            self, rig):
        rig.arm()

        result = await psec.write_section("linkedInProfile", {"url": "x"})

        # Structured, not raised: every other bad input on this path returns a
        # VALIDATION_ERROR dict, and a typo'd section name is bad input like
        # any other. An MCP caller gets an answer it can read, not a traceback.
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        message = result["message"]
        assert "linkedInProfile" in message
        for section in ("summary", "employments", "languages"):
            assert section in message, (
                "the error must list the writable sections; %r is missing "
                "from %r" % (section, message))

    async def test_an_unknown_section_never_reads_or_writes(self, rig):
        rig.arm()

        result = await psec.write_section("linkedInProfile", {"url": "x"})

        assert result["status"] == "error"
        assert rig.reads.calls == []
        assert rig.transport.await_count == 0


# ===========================================================================
# CONTROL 15 -- empty or non-dict fields
# ===========================================================================

class TestFieldsMustBeANonEmptyDict:

    @pytest.mark.parametrize("fields", [
        {}, None, [], "summary", 0, ("summary",),
    ])
    async def test_a_useless_fields_argument_is_refused(self, rig, fields):
        rig.arm()

        result = await psec.write_section("summary", fields)

        assert result["status"] == "error", (
            "fields=%r returned %r; a write with nothing to write is a bug in "
            "the caller and must say so" % (fields, result.get("status")))
        assert result["error_code"] == "VALIDATION_ERROR"

    async def test_a_useless_fields_argument_never_reads_or_writes(self, rig):
        rig.arm()

        await psec.write_section("languages", {}, confirm=True)

        assert rig.reads.calls == []
        assert rig.transport.await_count == 0


# ===========================================================================
# CONTROL 16 -- a transport exception is an error, never a success
# ===========================================================================

class TestTransportFailure:

    async def test_an_exception_from_the_transport_returns_an_error(self, rig):
        rig.arm(base_profile())
        rig.transport.side_effect = RuntimeError("connection reset by peer")

        result = await psec.write_section(
            "summary", {"summary": "Mine."}, confirm=True)

        assert result["status"] == "error", (
            "the transport raised and the call reported %r"
            % result.get("status"))
        assert result["error_code"] == "API_ERROR"
        assert result["written"] is False, (
            "a failed transport must state that nothing was written")

    async def test_the_failure_is_summarised_not_dumped(self, rig):
        rig.arm(base_profile())
        rig.transport.side_effect = RuntimeError("connection reset by peer")

        result = await psec.write_section(
            "summary", {"summary": "Mine."}, confirm=True)

        assert "RuntimeError" in result["message"]
        assert "connection reset by peer" in result["message"]
        assert "Traceback" not in result["message"], (
            "a raw traceback is not an error report: %r" % result["message"])

    async def test_a_failed_write_still_left_a_snapshot_on_disk(
            self, rig, snapshot_home):
        rig.arm(base_profile())
        rig.transport.side_effect = RuntimeError("connection reset by peer")

        await psec.write_section("summary", {"summary": "Mine."}, confirm=True)

        files = sorted(p.name for p in snapshot_home.iterdir())
        assert len(files) == 1, (
            "the snapshot must be taken BEFORE the write is attempted, so a "
            "failure still has something to restore from: %r" % files)

    async def test_no_reread_is_attempted_after_a_failed_write(self, rig):
        rig.arm(base_profile())
        rig.transport.side_effect = RuntimeError("connection reset by peer")

        result = await psec.write_section(
            "summary", {"summary": "Mine."}, confirm=True)

        assert result["status"] == "error"
        assert len(rig.reads.calls) == 1


# ===========================================================================
# CONTROL 17 -- the spec table is internally consistent
#
# Table-driven, so a section added later without an id_field fails loudly here
# instead of quietly dropping rows in production.
# ===========================================================================

class TestSpecTableConsistency:

    KINDS = (psec.SCALAR_BLOCK, psec.SINGLE_ROW, psec.WHOLE_LIST)

    def test_every_spec_declares_one_of_the_three_kinds(self):
        for section, spec in sorted(psec.SECTION_SPECS.items()):
            assert spec.kind in self.KINDS, (
                "section %r declares kind %r, which is not one of %r"
                % (section, spec.kind, list(self.KINDS)))

    def test_every_row_section_declares_an_id_field(self):
        for section, spec in sorted(psec.SECTION_SPECS.items()):
            if spec.kind == psec.SCALAR_BLOCK:
                continue
            assert spec.id_field, (
                "section %r is a %s collection with no id_field. Without one "
                "there is no way to target an update, and a whole-list merge "
                "cannot match the caller's row against the live list."
                % (section, spec.kind))

    def test_every_spec_declares_an_envelope(self):
        for section, spec in sorted(psec.SECTION_SPECS.items()):
            assert spec.envelope, "section %r has no envelope" % section

    def test_every_scalar_section_declares_the_fields_it_owns(self):
        """A scalar section with no allow-list can write any profile field."""
        for section, spec in sorted(psec.SECTION_SPECS.items()):
            if spec.kind != psec.SCALAR_BLOCK:
                continue
            assert psec.SCALAR_ALLOWED_FIELDS.get(section), (
                "scalar section %r has no entry in SCALAR_ALLOWED_FIELDS, so "
                "nothing scopes it inside the shared 'profile' envelope"
                % section)

    def test_the_whole_list_set_agrees_with_the_transport_and_the_rails(self):
        """Three modules name these six. A disagreement is silent data loss."""
        from_specs = {name for name, spec in psec.SECTION_SPECS.items()
                      if spec.kind == psec.WHOLE_LIST}
        assert from_specs == set(WHOLE_LIST_COLLECTIONS), (
            "the spec table and profile_write.WHOLE_LIST_COLLECTIONS "
            "disagree. Only in specs: %r. Only in transport: %r. A section "
            "the transport calls whole-list but the spec table calls "
            "single-row will delete every other row in it."
            % (sorted(from_specs - set(WHOLE_LIST_COLLECTIONS)),
               sorted(set(WHOLE_LIST_COLLECTIONS) - from_specs)))
        assert from_specs == set(WHOLE_LIST_SECTIONS), (
            "the spec table and profile_snapshot.WHOLE_LIST_SECTIONS "
            "disagree: %r vs %r"
            % (sorted(from_specs), sorted(WHOLE_LIST_SECTIONS)))

    def test_a_vocab_field_of_a_scalar_section_is_a_field_it_owns(self):
        for section, spec in sorted(psec.SECTION_SPECS.items()):
            if spec.kind != psec.SCALAR_BLOCK:
                continue
            allowed = psec.SCALAR_ALLOWED_FIELDS.get(section, set())
            for name in spec.vocab_fields:
                assert name in allowed, (
                    "section %r guards %r as a vocabulary field but the "
                    "scalar scope guard would refuse it anyway -- one of the "
                    "two tables is wrong" % (section, name))


# ===========================================================================
# DEFECT LEDGER -- strict xfails against the module under test
#
# The tripwire worked: both went XPASS(strict) the moment the module was
# repaired, which is what forced these markers off rather than leaving them
# behind as a stale excuse. They are now plain regression tests for the fix.
# ===========================================================================

class TestTheSnapshotIsNamedInEveryErrorPayload:
    """Regression: write_section once read snapshot.get('name'), but
    save_snapshot returns the filename under 'file'. Every error payload
    reported snapshot=None and told the caller to "restore from snapshot
    None" -- at the one moment a restore is needed. Fixed to .get('file')."""

    async def test_an_unverified_write_names_the_snapshot(
            self, rig, snapshot_home):
        before = base_profile()
        rig.arm(before, copy.deepcopy(before))

        result = await psec.write_section(
            "summary", {"summary": "Rewritten summary."}, confirm=True)

        on_disk = sorted(p.name for p in snapshot_home.iterdir())
        assert result["snapshot"] == on_disk[0], (
            "the error names snapshot %r; the file on disk is %r"
            % (result["snapshot"], on_disk[0]))

    async def test_a_failed_transport_names_the_snapshot(
            self, rig, snapshot_home):
        rig.arm(base_profile())
        rig.transport.side_effect = RuntimeError("connection reset by peer")

        result = await psec.write_section(
            "summary", {"summary": "Mine."}, confirm=True)

        on_disk = sorted(p.name for p in snapshot_home.iterdir())
        assert result["snapshot"] == on_disk[0], (
            "the error names snapshot %r; the file on disk is %r"
            % (result["snapshot"], on_disk[0]))
