"""The restore path, pinned. Fixtures only - no network, no live profile.

Eight controls, one per promise this layer makes. Every one of them was SHOWN
FAILING before it was allowed to stay: the source was broken deliberately
(the diff blanked, the row-collection refusal removed, the traversal guard
bypassed, the confirm gate forced open, and so on), the run went red, and the
observed message was recorded. The RED table is in
`_audit/_slices/naukri-section-tools-slice.md`. A check that has never been
seen failing certifies nothing.

CONTROL 4 IS THE ONE THAT MATTERS. The entire reason this module refuses a
row collection is that a HALF restore reported as success is worse than a
refusal - so the refusal has to be provably TOTAL. That is measured by
counting transport calls, never by reading a status string, because a status
string is exactly what a broken refusal would still return.

PURE: dict arithmetic plus a tmp_path snapshot directory (redirected for
every test by the autouse `snapshot_home` fixture in conftest.py). Nothing
here opens a socket, and `fullprofiles_write` is replaced wholesale.
"""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from naukri_server.tools import profile_restore as pr
from naukri_server.tools import profile_sections as psec
from naukri_server.tools.profile_snapshot import (
    WHOLE_LIST_SECTIONS, save_snapshot,
)

PROFILE_ID = "P-RESTORE"

#: A value that exists ONLY inside snapshot bodies. Control 8 asserts it never
#: appears in a listing - a leak here would print his profile into any
#: transcript that asked what could be restored.
CANARY = "CANARY-SUMMARY-DO-NOT-LEAK"


def _language(row_id, name, proficiency="Expert"):
    return {"languageId": row_id, "profileId": PROFILE_ID,
            "language": name, "proficiency": proficiency}


def _employment(row_id, org, designation="Engineer"):
    return {"employmentId": row_id, "profileId": PROFILE_ID,
            "organization": org, "designation": designation}


def _education(row_id, course, institute=None):
    """An education row. `entityInstitute` is a CONTROLLED-VOCABULARY field:
    the editor sends `{"id": .., "value": ..}` and the section layer refuses a
    bare string, because the API accepts one and then ignores it. Passing a
    string here is how a test produces a row that is refused BEFORE the wire.
    """
    row = {"educationId": row_id, "profileId": PROFILE_ID,
           "course": course, "yearOfCompletion": "2016"}
    if institute is not None:
        row["entityInstitute"] = institute
    return row


def _profile(summary=CANARY, headline="Old headline",
             languages=None, employments=None):
    """A profile in the READ shape the API actually returns.

    `profile` is an ARRAY OF ONE OBJECT, not an object - measured in
    `_audit/_slices/naukri-profile-read-shape.md`. Getting that wrong is how
    a scalar restore silently diffs nothing, so the fixture carries the real
    shape rather than a convenient one.
    """
    return {
        "profile": [{
            "profileId": PROFILE_ID,
            "summary": summary,
            "resumeHeadline": headline,
            "keySkills": "Node.js,TypeScript",
        }],
        "languages": [_language("L1", "English"), _language("L2", "Hindi")]
        if languages is None else languages,
        "employments": [_employment("E1", "OldCorp"),
                        _employment("E2", "OtherCorp")]
        if employments is None else employments,
    }


class FakeNaukri:
    """A profile the tests read and write, with the write route's semantics.

    `writes` is the point of the class: every "nothing was written" assertion
    is made against this list, not against a returned status.

    TWO FAILURE INJECTORS, because a row-by-row restore has to be shown
    surviving the two ways a write goes wrong, and they are NOT the same:

    `raise_on_write`  the transport throws. The server may or may not have
                      applied it, so the honest outcome is `unknown`.
    `swallow_write`   the write returns 200 and changes NOTHING. This is the
                      case the whole verify-by-re-read rail exists for, and it
                      must also come back `unknown` -- never a success.
    """

    def __init__(self, profile):
        self.profile = deepcopy(profile)
        self.writes = []
        #: 1-based index of the write that should throw. None = never.
        self.raise_on_write = None
        #: 1-based index of the write that is accepted and then discarded.
        self.swallow_write = None

    async def get(self, url, params=None):
        return deepcopy(self.profile)

    async def write(self, body, profile_id=None):
        attempt = len(self.writes) + 1
        if self.raise_on_write == attempt:
            self.writes.append(deepcopy(body))
            raise RuntimeError("transport exploded on write %d" % attempt)
        if self.swallow_write == attempt:
            self.writes.append(deepcopy(body))
            return {"status": "success"}  # accepted, applied to nothing
        self.writes.append(deepcopy(body))
        for key, value in body.items():
            if key == "profile":
                self.profile["profile"][0].update(value)
            elif key in WHOLE_LIST_SECTIONS:
                self.profile[key] = deepcopy(value)
            else:
                self._merge_rows(key, value)
        return {"status": "success"}

    def _merge_rows(self, key, rows):
        spec = psec.SECTION_SPECS.get(key)
        id_field = spec.id_field if spec else None
        current = self.profile.setdefault(key, [])
        for row in rows:
            target = row.get(id_field) if id_field else None
            for existing in current:
                if target is not None and existing.get(id_field) == target:
                    existing.update(deepcopy(row))
                    break
            else:
                current.append(deepcopy(row))


@pytest.fixture
def fake(monkeypatch):
    """Replace the profile READ and the write TRANSPORT. One patch point.

    `profile_restore` imports `_read_profile` from `profile_sections`, so
    patching `psec.api_client` covers both modules and there is no second
    seam a test could forget.
    """
    server = FakeNaukri(_profile())
    monkeypatch.setattr(psec, "api_client", SimpleNamespace(get=server.get))
    monkeypatch.setattr(psec, "fullprofiles_write", server.write)
    return server


async def _snapshot_of(profile, label="restore-test"):
    meta = await save_snapshot(profile, label)
    return meta["file"]


# ---------------------------------------------------------------------------
# CONTROL 1 - preview is read-only and actually reports the change
# ---------------------------------------------------------------------------

class TestPreviewIsReadOnly:
    async def test_it_reports_the_change_and_writes_nothing(self, fake):
        snap = await _snapshot_of(_profile(summary="ORIGINAL"))
        fake.profile["profile"][0]["summary"] = "OVERWRITTEN"

        out = await pr.preview_restore(snap, "summary")

        assert out["status"] == "preview"
        assert out["written"] is False
        changed = {e["path"]: (e["from"], e["to"])
                   for e in out["would_change"]["changed"]}
        assert changed == {"profile.summary": ("OVERWRITTEN", "ORIGINAL")}, out
        assert out["taken_at"], "the snapshot timestamp must come back"
        assert out["restorable"] is True
        assert fake.writes == [], "preview must never reach the transport"

    async def test_it_diffs_only_the_fields_that_section_owns(self, fake):
        """The four scalar sections share ONE `profile` object. A preview that
        diffed the whole object would report the neighbours as changes."""
        snap = await _snapshot_of(_profile(headline="ORIGINAL HEADLINE"))
        fake.profile["profile"][0]["resumeHeadline"] = "SOMETHING ELSE"

        out = await pr.preview_restore(snap, "summary")

        assert out["would_change"]["changed"] == []
        assert out["would_change"]["added"] == []
        assert out["would_change"]["removed"] == []
        assert fake.writes == []

    async def test_a_row_collection_preview_still_shows_the_row(self, fake):
        snap = await _snapshot_of(_profile())
        fake.profile["languages"][0]["proficiency"] = "Beginner"

        out = await pr.preview_restore(snap, "languages")

        assert out["status"] == "preview"
        assert [e["path"] for e in out["would_change"]["changed"]] == [
            "languages[languageId=L1].proficiency"]
        assert fake.writes == []


# ---------------------------------------------------------------------------
# CONTROL 2 - a scalar restore goes THROUGH write_section, confirmed
# ---------------------------------------------------------------------------

class TestScalarRestoreRoutesThroughTheWriteGate:
    async def test_it_calls_write_section_with_confirm_true(self, fake,
                                                            monkeypatch):
        snap = await _snapshot_of(_profile(summary="ORIGINAL"))
        fake.profile["profile"][0]["summary"] = "OVERWRITTEN"

        seen = []
        real = pr.write_section

        async def spy(**kw):
            seen.append(kw)
            return await real(**kw)

        monkeypatch.setattr(pr, "write_section", spy)
        out = await pr.restore_section(snap, "summary", confirm=True)

        assert len(seen) == 1, "the restore must issue exactly one write"
        assert seen[0]["section"] == "summary"
        assert seen[0]["confirm"] is True
        assert seen[0]["fields"] == {"summary": "ORIGINAL"}
        assert fake.writes == [{"profile": {"summary": "ORIGINAL"}}]
        assert out["status"] == "updated"
        assert out["verified"] is True
        assert out["collateral"] == []
        assert out["restored_from"] == snap
        assert fake.profile["profile"][0]["summary"] == "ORIGINAL"

    async def test_it_inherits_the_pre_write_snapshot(self, fake,
                                                       snapshot_home):
        """The restore is an ordinary write, so it takes its own snapshot
        first - restoring the wrong thing must itself be undoable.

        Asserted against the DIRECTORY, not against the `snapshot` field of
        the reply: as of this commit `write_section` reads that field with
        `snapshot.get("name")` while `save_snapshot` returns it under `file`,
        so the field is always None (profile_sections.py:347,360,370,381).
        Reported to the section-write owner. The snapshot itself is real, and
        this is the claim that matters."""
        snap = await _snapshot_of(_profile(summary="ORIGINAL"))
        fake.profile["profile"][0]["summary"] = "OVERWRITTEN"

        out = await pr.restore_section(snap, "summary", confirm=True)

        assert out["status"] == "updated"
        taken = [p.name for p in snapshot_home.glob("*.json")
                 if "pre-restore-summary" in p.name]
        assert len(taken) == 1, sorted(p.name for p in snapshot_home.glob("*"))


# ---------------------------------------------------------------------------
# CONTROL 3 - a one-row-per-call collection is RESTORED, row by row
#
# This class used to assert the opposite: that every row collection refused.
# That refusal was a transaction SHAPE ("write_section sends one row per
# call"), not a limit on what the write route could reach, and the ruling of
# 2026-08-25 is that a shape limit gets built, not declined. What survives
# from the old contract is the part that was never about shape: a row deleted
# on naukri.com still cannot come back with its identity, and that is still
# refused BY NAME.
# ---------------------------------------------------------------------------

class TestRowCollectionsAreRestoredRowByRow:
    async def test_one_differing_row_is_written_back(self, fake):
        snap = await _snapshot_of(_profile())
        fake.profile["employments"][0]["organization"] = "NewCorp"

        out = await pr.restore_section(snap, "employments", confirm=True)

        assert out["status"] == "updated", out
        assert out["restored_row"] == "E1"
        assert out["atomic"] is True, (
            "one differing row IS one write, and may say so")
        assert len(fake.writes) == 1
        assert fake.writes[0]["employments"] == [_employment("E1", "OldCorp")]
        assert fake.profile["employments"][0]["organization"] == "OldCorp"
        assert fake.profile["employments"][1]["organization"] == "OtherCorp", (
            "a single-row write must not disturb its neighbours")

    async def test_two_differing_rows_are_two_writes_and_both_land(self, fake):
        snap = await _snapshot_of(_profile())
        fake.profile["employments"][0]["organization"] = "NewCorp"
        fake.profile["employments"][1]["designation"] = "Something"

        out = await pr.restore_section(snap, "employments", confirm=True)

        assert out["status"] == "updated", out
        assert out["atomic"] is False, (
            "two writes are not atomic and the reply must never imply they are")
        assert out["rows"]["landed"] == ["E1", "E2"]
        assert out["rows"]["not_attempted"] == []
        assert out["rows"]["unknown"] == []
        assert out["rows"]["skipped"] == []
        assert out["restores_completely"] is True
        assert len(fake.writes) == 2, fake.writes
        assert fake.profile["employments"][0]["organization"] == "OldCorp"
        assert fake.profile["employments"][1]["designation"] == "Engineer"

    async def test_a_row_deleted_on_naukri_is_still_refused_by_name(self, fake):
        """The one refusal that is NOT a shape problem. A restore can return a
        row's content; it can never return the row's identity."""
        snap = await _snapshot_of(_profile())
        fake.profile["employments"] = [_employment("E2", "OtherCorp")]

        out = await pr.restore_section(snap, "employments", confirm=True)

        assert out["status"] == "unsupported"
        assert out["differing_rows"]["only_in_snapshot"] == ["E1"]
        assert "original id" in out["message"]
        assert fake.writes == [], (
            "nothing else differs, so there is nothing to write and nothing "
            "may be written")

    async def test_a_deleted_row_alongside_a_restorable_one_is_partial(self,
                                                                       fake):
        """The mixed case, and the one that must NEVER say `updated`: E2 goes
        back, E1 cannot, so the section does not end up matching the snapshot
        and the reply has to say so in both places."""
        snap = await _snapshot_of(_profile())
        fake.profile["employments"] = [
            _employment("E2", "ChangedCorp"), _employment("E3", "NewJoiner")]

        out = await pr.restore_section(snap, "employments", confirm=True)

        assert out["status"] == "partial", out
        assert out["restores_completely"] is False
        assert out["rows"]["landed"] == ["E2"]
        skipped = {s["row_id"]: s["reason"] for s in out["rows"]["skipped"]}
        assert skipped == {"E1": "deleted_on_naukri",
                           "E3": "added_since_snapshot"}, skipped
        assert len(fake.writes) == 1
        assert fake.profile["employments"][0]["organization"] == "OtherCorp"

    @pytest.mark.parametrize("section", [
        "employments", "educations", "schools", "itskills", "projects",
        "certifications",
    ])
    async def test_no_single_row_section_writes_when_nothing_differs(
            self, fake, section):
        """All six. `no rows differ` must never become `wrote nothing and
        called it updated` - a restore that reports success for a section it
        never touched is the same lie as a half-restore."""
        snap = await _snapshot_of(_profile())
        out = await pr.restore_section(snap, section, confirm=True)
        assert out["status"] in ("unsupported", "error"), out
        if out["status"] == "error":
            assert out["error_code"] == "SECTION_ABSENT"
        assert fake.writes == []


# ---------------------------------------------------------------------------
# CONTROL 4 - a refusal is TOTAL, and a PARTIAL never wears a success
#
# Two different promises now live here, and both are measured against
# `fake.writes` rather than a status string, because a status string is
# exactly what a broken version would still return.
# ---------------------------------------------------------------------------

class TestARefusalIsTotalAndAPartialIsNeverASuccess:
    async def test_a_whole_list_with_two_differing_rows_restores_both(self,
                                                                      fake):
        """A whole-list payload IS the whole list, so each call re-reads the
        live list and merges its one row into it. Two rows are two calls, and
        the second must not undo the first."""
        snap = await _snapshot_of(_profile())
        fake.profile["languages"][0]["proficiency"] = "Beginner"
        fake.profile["languages"][1]["proficiency"] = "Beginner"

        out = await pr.restore_section(snap, "languages", confirm=True)

        assert out["status"] == "updated", out
        assert out["atomic"] is False
        assert out["rows"]["landed"] == ["L1", "L2"]
        assert len(fake.writes) == 2, fake.writes
        assert [r["proficiency"] for r in fake.profile["languages"]] == [
            "Expert", "Expert"]

    async def test_a_row_that_fails_stops_the_run_and_the_reply_says_which(
            self, fake):
        """THE CASE THE RULING NAMED. Row 2 of 3 explodes: the caller must be
        able to read off exactly which row landed, which one did not, and
        which was never tried - and the whole thing must not say `updated`."""
        snap = await _snapshot_of(_profile(employments=[
            _employment("E1", "A"), _employment("E2", "B"),
            _employment("E3", "C")]))
        # The LIVE profile has to carry the same three rows, or E3 reads as
        # deleted-on-naukri and gets skipped instead of never-attempted.
        fake.profile["employments"] = [
            _employment("E1", "CLOBBERED"), _employment("E2", "CLOBBERED"),
            _employment("E3", "CLOBBERED")]
        fake.raise_on_write = 2

        out = await pr.restore_section(snap, "employments", confirm=True)

        assert out["status"] == "partial", out
        assert out["status"] != "updated"
        assert out["restores_completely"] is False
        assert out["rows"]["landed"] == ["E1"]
        assert out["rows"]["unknown"] == ["E2"], (
            "a transport exception cannot prove the server did not apply it")
        assert out["rows"]["not_attempted"] == ["E3"], (
            "a row never sent must be distinguishable from a row that failed")

        by_id = {u["row_id"]: u for u in out["row_results"]}
        assert by_id["E1"]["outcome"] == "landed"
        assert by_id["E1"]["snapshot"], "a taken write names its snapshot"
        assert by_id["E2"]["outcome"] == "unknown"
        assert by_id["E3"]["outcome"] == "not_attempted"
        for row_id in ("E1", "E2", "E3"):
            assert row_id in out["message"]

        # And the profile itself agrees with the report.
        live = {r["employmentId"]: r["organization"]
                for r in fake.profile["employments"]}
        assert live["E1"] == "A"
        assert live["E3"] == "CLOBBERED"

    @pytest.mark.parametrize("failure,bucket", [
        ("raise", "unknown"),
        ("swallow", "unknown"),
    ])
    async def test_every_planned_row_lands_in_exactly_one_bucket(
            self, fake, failure, bucket):
        """The summary must ACCOUNT for every planned row. A bucket set that
        can drop one lets a caller add up landed + unknown + not_attempted,
        get less than it asked for, and have no idea where the rest went."""
        snap = await _snapshot_of(_profile(employments=[
            _employment("E1", "A"), _employment("E2", "B"),
            _employment("E3", "C")]))
        fake.profile["employments"] = [
            _employment("E1", "X"), _employment("E2", "Y"),
            _employment("E3", "Z")]
        setattr(fake, "raise_on_write" if failure == "raise"
                else "swallow_write", 2)

        out = await pr.restore_section(snap, "employments", confirm=True)
        rows = out["rows"]

        accounted = []
        for key in ("landed", "landed_with_collateral", "refused", "unknown",
                    "not_attempted"):
            accounted += rows[key]
        assert sorted(accounted) == sorted(rows["planned"]), rows
        assert len(accounted) == len(set(accounted)), (
            "a row appeared in two buckets: %r" % accounted)
        assert "E2" in rows[bucket]

    async def test_a_row_refused_before_the_wire_gets_its_own_bucket(self,
                                                                     fake):
        """`refused` is NOT `unknown`. A row the section layer rejected never
        reached Naukri, and that is the ONE outcome that certifies nothing was
        written - folding it in with the dispatched-and-unconfirmed rows would
        throw away the only certainty in the report."""
        # ED1 carries the vocabulary field as a bare string, so write_section
        # refuses it. ED2 differs too and must therefore never be attempted.
        snap = await _snapshot_of(
            dict(_profile(), educations=[
                _education("ED1", "BTech", institute="MIT"),
                _education("ED2", "MTech")]),
            "edu")
        fake.profile["educations"] = [
            _education("ED1", "CHANGED", institute="MIT"),
            _education("ED2", "CHANGED")]

        out = await pr.restore_section(snap, "educations", confirm=True)
        rows = out["rows"]

        assert out["status"] == "error", out
        assert rows["refused"] == ["ED1"], rows
        assert rows["unknown"] == [], (
            "a refusal is not an unknown: it certifies nothing was sent")
        assert rows["not_attempted"] == ["ED2"]
        assert rows["landed"] == []
        assert fake.writes == [], (
            "a row refused before the transport must not reach it")

        accounted = []
        for key in ("landed", "landed_with_collateral", "refused", "unknown",
                    "not_attempted"):
            accounted += rows[key]
        assert sorted(accounted) == sorted(rows["planned"])

    async def test_an_accepted_but_unpersisted_write_is_unknown_not_success(
            self, fake):
        """A 200 that changed nothing is the exact failure the verify-by-
        re-read rail exists for. It must surface as `unknown`, which is
        neither 'done' nor 'nothing happened'."""
        snap = await _snapshot_of(_profile())
        fake.profile["employments"][0]["organization"] = "NewCorp"
        fake.profile["employments"][1]["designation"] = "Something"
        fake.swallow_write = 1

        out = await pr.restore_section(snap, "employments", confirm=True)

        assert out["status"] == "error", out
        assert out["error_code"] == "RESTORE_FAILED"
        assert out["written"] is False
        assert out["rows"]["landed"] == []
        assert out["rows"]["unknown"] == ["E1"]
        assert out["rows"]["not_attempted"] == ["E2"]
        assert len(fake.writes) == 1, "the run must STOP, not plough on"

    async def test_a_whole_list_missing_a_row_writes_nothing(self, fake):
        """`_merge_whole_list` builds its payload from the LIVE list, so a row
        deleted since the snapshot cannot come back through this path."""
        snap = await _snapshot_of(_profile())
        fake.profile["languages"] = [_language("L2", "Hindi")]

        out = await pr.restore_section(snap, "languages", confirm=True)

        assert out["status"] == "unsupported"
        assert out["differing_rows"]["only_in_snapshot"] == ["L1"]
        assert fake.writes == []

    async def test_the_one_row_whole_list_case_does_write(self, fake):
        """CONTROL on the control. The refusals above are only meaningful
        because the supported case genuinely writes - a path that refused
        everything would pass every assertion in this class."""
        snap = await _snapshot_of(_profile())
        fake.profile["languages"][0]["proficiency"] = "Beginner"

        out = await pr.restore_section(snap, "languages", confirm=True)

        assert out["status"] == "updated", out
        assert len(fake.writes) == 1
        assert fake.writes[0]["languages"] == [
            _language("L1", "English"), _language("L2", "Hindi")]
        assert out["rows_lost"] == []
        assert fake.profile["languages"][0]["proficiency"] == "Expert"


# ---------------------------------------------------------------------------
# CONTROL 5 - the confirm gate is inherited, not re-implemented
# ---------------------------------------------------------------------------

class TestConfirmFalseWritesNothing:
    async def test_a_scalar_restore_previews_the_exact_body(self, fake):
        snap = await _snapshot_of(_profile(summary="ORIGINAL"))
        fake.profile["profile"][0]["summary"] = "OVERWRITTEN"

        out = await pr.restore_section(snap, "summary")

        assert out["status"] == "preview"
        assert out["would_send"] == {"profile": {"summary": "ORIGINAL"}}
        assert out["restored_from"] == snap
        assert fake.writes == []
        assert fake.profile["profile"][0]["summary"] == "OVERWRITTEN"

    async def test_a_whole_list_restore_previews_the_whole_list(self, fake):
        snap = await _snapshot_of(_profile())
        fake.profile["languages"][0]["proficiency"] = "Beginner"

        out = await pr.restore_section(snap, "languages")

        assert out["status"] == "preview"
        assert out["rows_in_payload"] == 2, (
            "the payload for a whole-list section is the WHOLE list")
        assert fake.writes == []
        assert fake.profile["languages"][0]["proficiency"] == "Beginner"

    async def test_a_MULTI_ROW_restore_writes_nothing_without_confirm(self,
                                                                      fake):
        """The gate on the path this slice BUILT. The row-by-row restore is
        new, so its confirm gate is new, and a new write path that fires
        without consent is exactly what the gate exists to stop."""
        snap = await _snapshot_of(_profile())
        fake.profile["employments"][0]["organization"] = "NewCorp"
        fake.profile["employments"][1]["designation"] = "Something"

        out = await pr.restore_section(snap, "employments")

        assert out["status"] == "preview"
        assert out["written"] is False
        assert out["atomic"] is False
        assert out["rows"]["planned"] == ["E1", "E2"]
        assert fake.writes == [], (
            "the gate is measured at the transport, not read off the status")
        assert fake.profile["employments"][0]["organization"] == "NewCorp"

        # The preview is the thing the operator approves against, so it has to
        # carry the bodies - and say the quiet part about the ones after the
        # first.
        bodies = [u["would_send"] for u in out["row_results"]]
        assert bodies == [
            {"employments": [_employment("E1", "OldCorp")]},
            {"employments": [_employment("E2", "OtherCorp")]},
        ], bodies
        assert "NOT atomic" in out["message"]
        assert "confirm=True" in out["message"]

    async def test_a_preview_with_skipped_rows_says_it_will_not_complete(
            self, fake):
        snap = await _snapshot_of(_profile())
        fake.profile["employments"] = [
            _employment("E2", "ChangedCorp"), _employment("E3", "NewJoiner")]

        out = await pr.restore_section(snap, "employments")

        assert out["status"] == "preview"
        assert out["restores_completely"] is False, (
            "consent must be given against the fact that the section will "
            "STILL not match the snapshot afterwards")
        assert "SKIPPED" in out["message"]
        assert fake.writes == []


# ---------------------------------------------------------------------------
# CONTROL 5b - a restore cannot destroy the snapshot it may need to retry from
# ---------------------------------------------------------------------------

class TestTheSourceSnapshotSurvivesTheRestore:
    async def test_a_failed_multi_row_restore_leaves_the_source_intact(
            self, fake, snapshot_home):
        """A row-by-row restore takes a NEW pre-write snapshot per row. If any
        of those could land on the source filename, the file needed to retry
        would be gone at exactly the moment it was needed."""
        original = _profile(employments=[
            _employment("E1", "A"), _employment("E2", "B")])
        snap = await _snapshot_of(original, "the-source")
        source = snapshot_home / snap
        before = source.read_bytes()

        fake.profile["employments"] = [
            _employment("E1", "CLOBBERED"), _employment("E2", "CLOBBERED")]
        fake.raise_on_write = 2

        out = await pr.restore_section(snap, "employments", confirm=True)
        assert out["status"] == "partial"

        assert source.exists(), "the source snapshot was deleted"
        assert source.read_bytes() == before, (
            "the source snapshot was rewritten by the restore that reads it")

        # The per-row snapshots are additional files, not replacements: one
        # per ATTEMPTED write, including the one that blew up (write_section
        # snapshots before it dispatches, so the row that failed is the row
        # whose pre-state is most worth having), and all under distinct names.
        taken = sorted(p.name for p in snapshot_home.glob("*.json")
                       if "pre-restore-employments" in p.name)
        assert len(taken) == 2, taken
        assert len(set(taken)) == 2, "two snapshots collided on one filename"
        assert snap not in taken

        # And the retry works off the untouched source.
        fake.raise_on_write = None
        again = await pr.restore_section(snap, "employments", confirm=True)
        assert again["status"] == "updated", again
        assert [r["organization"] for r in fake.profile["employments"]] == [
            "A", "B"]


# ---------------------------------------------------------------------------
# CONTROL 6 - the traversal guard is inherited from load_snapshot
# ---------------------------------------------------------------------------

class TestASnapshotNameCannotEscapeTheSnapshotDirectory:
    @pytest.mark.parametrize("name", [
        "../secrets.json",
        "../../naukri_server/config.py",
        "sub/dir.json",
        r"..\windows.json",
        r"C:\Windows\System32\drivers\etc\hosts",
        "/etc/passwd",
        "..",
    ])
    async def test_restore_refuses_it(self, fake, name):
        out = await pr.restore_section(name, "summary", confirm=True)

        assert out["status"] == "error", out
        assert out["error_code"] == "VALIDATION_ERROR"
        assert "snapshot name must" in out["message"], out["message"]
        assert out["written"] is False
        assert fake.writes == []

    @pytest.mark.parametrize("name", ["../secrets.json", "sub/dir.json"])
    async def test_preview_refuses_it_too(self, fake, name):
        out = await pr.preview_restore(name, "summary")
        assert out["status"] == "error"
        assert out["error_code"] == "VALIDATION_ERROR"
        assert fake.writes == []

    async def test_CONTROL_a_legitimate_bare_name_is_accepted(self, fake):
        """The guard is only meaningful because a real name goes through."""
        snap = await _snapshot_of(_profile(summary="ORIGINAL"))
        out = await pr.restore_section(snap, "summary")
        assert out["status"] == "preview", out

    async def test_a_confined_but_missing_name_is_a_different_error(self,
                                                                    fake):
        out = await pr.restore_section("profile-nope.json", "summary")
        assert out["status"] == "error"
        assert out["error_code"] == "NOT_FOUND"
        assert fake.writes == []


# ---------------------------------------------------------------------------
# CONTROL 7 - a section the snapshot never held is an error, not a crash
# ---------------------------------------------------------------------------

class TestASectionAbsentFromTheSnapshot:
    async def test_a_missing_collection_reports_it_clearly(self, fake):
        thin = {"profile": [{"profileId": PROFILE_ID, "summary": "S"}]}
        snap = await _snapshot_of(thin, "thin")

        out = await pr.restore_section(snap, "languages", confirm=True)

        assert out["status"] == "error"
        assert out["error_code"] == "SECTION_ABSENT"
        assert "languages" in out["message"]
        assert out["written"] is False
        assert fake.writes == []

    async def test_a_missing_scalar_field_set_reports_it_clearly(self, fake):
        thin = {"profile": [{"profileId": PROFILE_ID, "summary": "S"}]}
        snap = await _snapshot_of(thin, "thin")

        out = await pr.restore_section(snap, "careerPreferences", confirm=True)

        assert out["status"] == "error"
        assert out["error_code"] == "SECTION_ABSENT"
        assert "careerPreferences" in out["message"]
        assert fake.writes == []

    async def test_preview_says_so_too_without_crashing(self, fake):
        thin = {"profile": [{"profileId": PROFILE_ID, "summary": "S"}]}
        snap = await _snapshot_of(thin, "thin")

        out = await pr.preview_restore(snap, "languages")

        assert out["status"] == "error"
        assert out["error_code"] == "SECTION_ABSENT"
        assert fake.writes == []

    async def test_an_unknown_section_name_lists_the_real_ones(self, fake):
        snap = await _snapshot_of(_profile())
        out = await pr.restore_section(snap, "nosuchsection", confirm=True)
        assert out["status"] == "error"
        assert out["error_code"] == "VALIDATION_ERROR"
        assert "summary" in out["message"] and "languages" in out["message"]
        assert fake.writes == []

    async def test_a_snapshot_without_a_profile_body_is_refused(self, fake,
                                                               snapshot_home):
        snapshot_home.mkdir(parents=True, exist_ok=True)
        bad = snapshot_home / "profile-20260101T000000000000Z__junk__deadbeef.json"
        bad.write_text(json.dumps({"taken_at": "x"}), encoding="utf-8")

        out = await pr.restore_section(bad.name, "summary", confirm=True)

        assert out["status"] == "error"
        assert out["error_code"] == "SNAPSHOT_UNREADABLE"
        assert fake.writes == []


# ---------------------------------------------------------------------------
# CONTROL 8 - the listing tool returns metadata, never profile content
# ---------------------------------------------------------------------------

#: Everything `list_snapshots` is allowed to publish about a snapshot.
LISTING_KEYS = {"file", "taken_at", "label", "bytes", "section_count",
                "readable", "snapshot_version"}


class TestListingLeaksNothing:
    async def test_no_profile_value_appears_in_the_listing(self, fake):
        await _snapshot_of(_profile(summary=CANARY), "leak-probe")

        from naukri_server.tools.profile import naukri_list_profile_snapshots
        out = await naukri_list_profile_snapshots()

        blob = json.dumps(out)
        assert out["snapshots"], out
        for secret in (CANARY, PROFILE_ID, "OldCorp", "Node.js", "English",
                       "Old headline"):
            assert secret not in blob, (
                "%r reached a snapshot LISTING - the listing is metadata "
                "only, and anything else prints his profile into whatever "
                "transcript asked what could be restored" % secret)

    async def test_the_listing_publishes_only_the_metadata_keys(self, fake):
        await _snapshot_of(_profile(), "keys-probe")

        from naukri_server.tools.profile import naukri_list_profile_snapshots
        out = await naukri_list_profile_snapshots()

        assert out["status"] == "success"
        for entry in out["snapshots"]:
            assert set(entry) <= LISTING_KEYS, set(entry) - LISTING_KEYS
        assert "sections" not in blob_keys(out), (
            "even the section NAME list stays out of a listing")

    async def test_the_limit_is_applied_and_validated(self, fake):
        for i in range(3):
            await _snapshot_of(_profile(), "limit-probe-%d" % i)

        from naukri_server.tools.profile import naukri_list_profile_snapshots
        out = await naukri_list_profile_snapshots(limit=2)
        assert out["total"] == 3
        assert out["returned"] == 2
        assert len(out["snapshots"]) == 2

        bad = await naukri_list_profile_snapshots(limit=0)
        assert bad["status"] == "error"
        assert bad["error_code"] == "VALIDATION_ERROR"


def blob_keys(payload) -> set:
    """Every key name anywhere in a nested payload."""
    found = set()
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found |= set(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found
