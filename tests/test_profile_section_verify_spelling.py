"""The VERIFY-side row-id spelling, pinned. Fixtures only -- no network.

`write_section` builds two things out of one caller `fields` dict, and they are
NOT in the same vocabulary:

  * `section_body` goes ON THE WIRE and must use the WRITE spelling. Naukri's
    editor renames each row's `id` to `<section>Id` before sending
    (`er=function(a,b){return a.forEach(function(a){a.onlineProfileId=a.id,
    delete a.profileId, delete a.id})...` at mnj_v320 offset 640662, and the
    same shape at 656875 / 668250 / 681299 / 696760).
  * `expected` goes to `verify_write`, which checks CONTAINMENT against the
    AFTER READ -- and the read spells that same row id plainly `id`
    (`_audit/_slices/naukri-profile-read-shape.md`: `onlineProfile` is the one
    section whose row id is named `id`).

`_build_body` used to hand `dict(fields)` to both. For the five id-mapped
whole-list collections an UPDATE therefore looked for `onlineProfileId` in a
payload that only ever carries `id`, so `verified` could never be True and a
perfectly landed write came back `status: "error", error_code: "NOT_PERSISTED"`.
That is a CHECK THAT CANNOT PASS -- the mirror of a check that cannot fail, and
worse for the reader, because it teaches distrust of a write that worked.
`_to_expected(fields, spec)` closes it: rename `spec.id_field` to
`spec.read_id_field` when they differ, identity when they agree.

THE CONTROL THAT MATTERS MOST IS 5. A fix that makes verification pass is one
edit away from making verification pass ALWAYS. Control 5 pins the other side:
an update that lands but silently drops an unrelated row must still come back
`COLLATERAL_CHANGE` with the lost row named, and control 6 pins that a write
which genuinely did not persist still comes back `NOT_PERSISTED`. Without those
two, this repair would just move the un-failable check to the other end.

ZERO NETWORK, ZERO WRITES AGAINST naukri.com. Three seams are mocked and
nothing else -- the profile READ (`api_client` in the module namespace), the
TRANSPORT (`fullprofiles_write`), and the event-bus emit -- the same rig
tests/test_profile_sections.py uses. The snapshot layer runs for real against a
tmp_path directory.

FIXTURE HONESTY. Only `onlineProfile` was captured non-empty in the live read;
`patent`, `presentation`, `publication` and `workSample` were all empty arrays,
so their row SHAPE is unmeasured. The five rows here are therefore one
synthetic shape reused, and what the table-driven controls exercise is the spec
table plus `_to_expected` -- the id rename, which the bundle documents for all
five -- not four guessed field lists.

EVERY CONTROL HERE WAS SHOWN FAILING FIRST. `profile_sections.py` was broken
deliberately one mutation at a time -- `_to_expected` made the identity
function, made to rename unconditionally against a hardcoded `id`, made to
expect only the row id, `rows_lost` handed the write spelling, and the read
spelling pushed into the wire body -- the single target test was run, the
verbatim RED was recorded, and the source was restored byte-for-byte and
re-hashed. The RED table and the before/after SHA-256 are in
`_audit/_slices/naukri-verify-spelling-slice.md`. A check never observed
failing certifies nothing.
"""

import copy

import pytest
from unittest.mock import AsyncMock

from naukri_server.tools import profile_sections as psec
from naukri_server.tools import profile_snapshot as ps

# Stand-in for the 64-character live profileId. The real one is
# account-identifying and never appears in a test file; the real online-profile
# ids are 48 characters and are likewise replaced by short readable tags. The
# rename under test is length-agnostic, so a legible tag makes a RED readable.
PROFILE_ID = "0" * 64

#: The five collections whose READ id (`id`) and WRITE id (`<section>Id`)
#: disagree: write envelope, read section key, write id field, bundle offset.
ID_MAPPED = (
    ("onlineProfiles", "onlineProfile", "onlineProfileId", "640662 (er)"),
    ("workSamples", "workSample", "workSampleId", "656875 (pr)"),
    ("presentations", "presentation", "presentationId", "668250 (Ar)"),
    ("publications", "publication", "publicationId", "681299 (Lr)"),
    ("patents", "patent", "patentId", "696760 (Wr)"),
)

#: Derived from the spec table, not typed out: a scalar section added later is
#: covered by control 3 the day it lands.
SCALAR_SECTIONS = tuple(sorted(
    name for name, spec in psec.SECTION_SPECS.items()
    if spec.kind == psec.SCALAR_BLOCK))


# ---------------------------------------------------------------------------
# Fixtures in the READ spelling the API actually returns: the section key is
# SINGULAR and the row id is `id`, sitting next to a `profileId` the editor
# strips before sending.
# ---------------------------------------------------------------------------

def _row(row_id, description):
    """One id-mapped whole-list row as READ: row id named `id`."""
    return {
        "id": row_id,
        "profileId": PROFILE_ID,
        "profile": "Portfolio",
        "description": description,
        "url": "url-%s" % row_id.lower(),
    }


def _language(row_id, name, level_id, level):
    """One `languages` row as READ: id named `languageId`, and no `id` key.

    `entityLanguageId` is the same on every row on purpose -- it is a
    vocabulary anchor, not a row id, and holding it constant keeps
    `detect_id_field` selecting `languageId` deterministically.
    """
    return {
        "languageId": row_id,
        "entityLanguageId": "1",
        "profileId": PROFILE_ID,
        "lang": name,
        "proficiency": {"id": level_id, "value": level},
        "ability": ["Read", "Write", "Speak"],
    }


def base_profile(read_key_name="onlineProfile"):
    """Four rows in the id-mapped collection, three languages, one scalar row.

    Four is the floor that makes control 5 mean something: the write touches
    one row and drops one more, so two must still survive untouched, and an
    off-by-one merge cannot pass at that width by accident.
    """
    return {
        "profile": [{
            "profileId": PROFILE_ID,
            "resumeHeadline": "Backend engineer",
            "summary": "Builds services.",
        }],
        read_key_name: [
            _row("ROW1", "desc-one"),
            _row("ROW2", "desc-two"),
            _row("ROW3", "desc-three"),
            _row("ROW4", "desc-four"),
        ],
        "languages": [
            _language("LANG1", "Lang-One", 3, "Expert"),
            _language("LANG2", "Lang-Two", 1, "Beginner"),
            _language("LANG3", "Lang-Three", 2, "Proficient"),
        ],
    }


def with_row_updated(profile, read_key_name, row_id, **changes):
    """An AFTER read in which *row_id* really did change. Deep-copied."""
    after = copy.deepcopy(profile)
    hits = 0
    for row in after[read_key_name]:
        if row.get("id") == row_id:
            row.update(changes)
            hits += 1
    assert hits == 1, (
        "fixture bug: %d row(s) matched id %r in %r; an after-read that edits "
        "zero rows would make the test pass for the wrong reason"
        % (hits, row_id, read_key_name))
    return after


def with_language_updated(profile, row_id, **changes):
    """The same, for the collection whose read and write ids AGREE."""
    after = copy.deepcopy(profile)
    hits = 0
    for row in after["languages"]:
        if row.get("languageId") == row_id:
            row.update(changes)
            hits += 1
    assert hits == 1, "fixture bug: %d language row(s) matched %r" % (hits, row_id)
    return after


def without_row(profile, read_key_name, row_id):
    """An AFTER read from which *row_id* has silently vanished."""
    after = copy.deepcopy(profile)
    kept = [r for r in after[read_key_name] if r.get("id") != row_id]
    assert len(kept) == len(after[read_key_name]) - 1, (
        "fixture bug: removing %r did not drop exactly one row" % row_id)
    after[read_key_name] = kept
    return after


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
        """Queue the reads this call may make, in order. Deep-copied."""
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
    """Redirect the snapshot directory off the repo for every test here."""
    target = tmp_path / "profile_snapshots"
    monkeypatch.setattr(ps, "_SNAPSHOT_DIR", target)
    return target


@pytest.fixture(autouse=True)
def no_event_fanout(monkeypatch):
    """Record the ProfileUpdated emit; never dispatch it.

    The live subscriber writes a notification row into naukri.db. A unit test
    must not, so the bus is stubbed and the emit is merely observable.
    """
    import naukri_server.events as events
    emit = AsyncMock(return_value=None)
    monkeypatch.setattr(events.event_bus, "emit", emit)
    return emit


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


def _verdict(result):
    """Everything a failure message needs, in one line."""
    return ("status=%r error_code=%r verified=%r rows_lost=%r\nreason: %s"
            % (result.get("status"), result.get("error_code"),
               result.get("verified"), result.get("rows_lost"),
               result.get("reason")))


# ===========================================================================
# CONTROL 1 -- the headline. A clean onlineProfiles UPDATE verifies.
#
# Before the fix this exact call returned NOT_PERSISTED: `expected` carried
# `onlineProfileId`, the after read carries `id`, and containment could not
# match. The write had landed.
# ===========================================================================

class TestACleanUpdateIsReportedAsUpdated:

    async def test_onlineprofiles_update_that_landed_reports_updated(self, rig):
        before = base_profile()
        after = with_row_updated(before, "onlineProfile", "ROW2",
                                 description="desc-two-updated")
        rig.arm(before, after)

        result = await psec.write_section(
            "onlineProfiles",
            {"onlineProfileId": "ROW2", "description": "desc-two-updated"},
            confirm=True)

        assert result["verified"] is True, (
            "the after read genuinely contains the updated row, so this write "
            "DID land -- reporting it unverified is the check-that-cannot-pass "
            "this control exists for.\n%s" % _verdict(result))
        assert result["status"] == "updated", (
            "a landed, collateral-free write must report status 'updated'.\n%s"
            % _verdict(result))
        assert "error_code" not in result, (
            "a landed write carries no error_code.\n%s" % _verdict(result))

    async def test_the_expected_handed_to_the_verifier_is_in_the_read_spelling(self):
        """The unit-level statement of the same fact, with no rig in the way."""
        spec = psec.SECTION_SPECS["onlineProfiles"]
        expected = psec._to_expected(
            {"onlineProfileId": "ROW2", "description": "desc-two-updated"}, spec)

        assert expected == {"id": "ROW2", "description": "desc-two-updated"}, (
            "verify_write compares this against the AFTER READ, which spells "
            "the row id 'id'. Got %r" % expected)


# ===========================================================================
# CONTROL 2 -- the same, across all five id-mapped collections
# ===========================================================================

class TestEveryIdMappedCollectionVerifies:

    @pytest.mark.parametrize("envelope,read_key_name,id_field,origin", ID_MAPPED)
    async def test_an_update_that_landed_reports_updated(
            self, rig, envelope, read_key_name, id_field, origin):
        before = base_profile(read_key_name)
        after = with_row_updated(before, read_key_name, "ROW2",
                                 description="desc-two-updated")
        rig.arm(before, after)

        result = await psec.write_section(
            envelope, {id_field: "ROW2", "description": "desc-two-updated"},
            confirm=True)

        assert result["status"] == "updated" and result["verified"] is True, (
            "%r (bundle %s) writes %r and reads 'id'. The after read contains "
            "the updated row, so this must verify.\n%s"
            % (envelope, origin, id_field, _verdict(result)))

    def test_the_table_covers_every_id_mapped_section_in_the_spec(self):
        """A sixth id-mapped collection must not slip past this file."""
        from_spec = sorted(
            name for name, spec in psec.SECTION_SPECS.items()
            if spec.read_id_field != spec.id_field)

        assert from_spec == sorted(entry[0] for entry in ID_MAPPED), (
            "SECTION_SPECS declares a read/write id gap for %r, but this "
            "file's ID_MAPPED table lists %r. Untested spellings are exactly "
            "how the first one got through."
            % (from_spec, sorted(entry[0] for entry in ID_MAPPED)))


# ===========================================================================
# CONTROL 3 -- the rename is CONFINED
#
# `_to_expected` renames only where the two spellings actually differ. Applied
# to `languages`, whose read rows carry `languageId` and no `id` at all, a
# rename would break a section that worked -- the repair eating its neighbour.
# ===========================================================================

class TestTheRenameIsConfinedToSectionsThatNeedIt:

    @pytest.mark.parametrize("envelope,read_key_name,id_field,origin", ID_MAPPED)
    def test_it_renames_the_write_id_to_the_read_id(
            self, envelope, read_key_name, id_field, origin):
        spec = psec.SECTION_SPECS[envelope]
        expected = psec._to_expected({id_field: "ROW2", "description": "d"}, spec)

        assert expected == {"id": "ROW2", "description": "d"}, (
            "%r must be restated as 'id' for the verifier (bundle %s). Got %r"
            % (id_field, origin, expected))
        assert id_field not in expected, (
            "the write spelling %r survived into `expected`; the after read "
            "never carries it, so containment cannot match." % id_field)

    def test_it_is_identity_for_languages(self):
        """languages reads and writes `languageId` alike. Nothing to rename."""
        spec = psec.SECTION_SPECS["languages"]
        fields = {"languageId": "LANG2",
                  "proficiency": {"id": 3, "value": "Expert"}}

        assert psec._to_expected(fields, spec) == fields, (
            "languages is a whole-list section whose read and write spellings "
            "AGREE. Renaming here would break a section that worked.")

    @pytest.mark.parametrize("section", SCALAR_SECTIONS)
    def test_it_is_identity_for_every_scalar_block_section(self, section):
        spec = psec.SECTION_SPECS[section]
        field = sorted(psec.SCALAR_ALLOWED_FIELDS[section])[0]
        fields = {field: "a value"}

        assert psec._to_expected(fields, spec) == fields, (
            "%r is a SCALAR_BLOCK: it has no row id at all (id_field=%r, "
            "read_id_field=%r), so `expected` is the caller's fields verbatim."
            % (section, spec.id_field, spec.read_id_field))

    def test_the_scalar_table_is_not_empty(self):
        """A table-driven control over an empty table certifies nothing."""
        assert len(SCALAR_SECTIONS) >= 4, (
            "expected at least the four scalar sections, got %r"
            % (SCALAR_SECTIONS,))

    async def test_a_languages_update_still_verifies_end_to_end(self, rig):
        before = base_profile()
        after = with_language_updated(
            before, "LANG2", proficiency={"id": 3, "value": "Expert"})
        rig.arm(before, after)

        result = await psec.write_section(
            "languages",
            {"languageId": "LANG2", "proficiency": {"id": 3, "value": "Expert"}},
            confirm=True)

        assert result["status"] == "updated" and result["verified"] is True, (
            "languages has no spelling gap. A rename applied to it would send "
            "the verifier looking for an 'id' key its rows do not have.\n%s"
            % _verdict(result))


# ===========================================================================
# CONTROL 4 -- a CREATE is untouched
#
# A create carries no id field. `_to_expected` must not invent one: an
# `expected` containing `{"id": None}` matches no row ever returned, so a
# perfectly good create would report NOT_PERSISTED.
# ===========================================================================

class TestACreateIsUnaffected:

    @pytest.mark.parametrize("envelope,read_key_name,id_field,origin", ID_MAPPED)
    def test_no_id_key_is_invented(
            self, envelope, read_key_name, id_field, origin):
        spec = psec.SECTION_SPECS[envelope]
        fields = {"profile": "Portfolio", "description": "desc-new",
                  "url": "url-new"}

        expected = psec._to_expected(fields, spec)

        assert expected == fields, (
            "a create has no %r to rename; `expected` must be the caller's "
            "fields verbatim. Got %r" % (id_field, expected))
        assert "id" not in expected, (
            "`expected` grew an 'id' key out of a create. No row in any read "
            "will match it, so the create would report NOT_PERSISTED.")

    async def test_a_create_that_landed_reports_updated(self, rig):
        before = base_profile()
        after = copy.deepcopy(before)
        after["onlineProfile"].append({
            "id": "ROW5", "profileId": PROFILE_ID, "profile": "Portfolio",
            "description": "desc-new", "url": "url-new",
        })
        rig.arm(before, after)

        result = await psec.write_section(
            "onlineProfiles",
            {"profile": "Portfolio", "description": "desc-new",
             "url": "url-new"},
            confirm=True)

        assert result["action"] == "create", (
            "fields carry no onlineProfileId, so this is a create.\n%s"
            % _verdict(result))
        assert result["status"] == "updated" and result["verified"] is True, (
            "the after read carries the new row; the create landed.\n%s"
            % _verdict(result))


# ===========================================================================
# CONTROL 5 -- the fix must not blind the collateral detector
#
# THE ONE THAT KEEPS THIS REPAIR HONEST. Making `verified` reachable is one
# edit away from making it unconditional. Here the intended change DOES land
# AND an unrelated row disappears: the call must still refuse, name the lost
# row, and point at the snapshot.
# ===========================================================================

class TestCollateralIsStillDetectedWhenTheWriteVerifies:

    async def test_a_landed_update_that_dropped_a_row_is_still_an_error(self, rig):
        before = base_profile()
        landed = with_row_updated(before, "onlineProfile", "ROW2",
                                  description="desc-two-updated")
        after = without_row(landed, "onlineProfile", "ROW4")
        rig.arm(before, after)

        result = await psec.write_section(
            "onlineProfiles",
            {"onlineProfileId": "ROW2", "description": "desc-two-updated"},
            confirm=True)

        assert result["verified"] is True, (
            "the intended change is in the after read -- this control is only "
            "meaningful while verification PASSES, because it exists to prove "
            "a passing verification does not silence collateral.\n%s"
            % _verdict(result))
        assert result["status"] == "error", (
            "a row vanished. Reporting success here is the silent wipe the "
            "whole rails layer exists to catch.\n%s" % _verdict(result))
        assert result["error_code"] == "COLLATERAL_CHANGE", (
            "expected COLLATERAL_CHANGE.\n%s" % _verdict(result))
        assert result["rows_lost"] == ["ROW4"], (
            "the lost row must be NAMED, not merely counted -- an empty list "
            "is indistinguishable from 'nothing was lost'. rows_lost compares "
            "two READ payloads, so it takes the read spelling 'id'.\n%s"
            % _verdict(result))
        assert result["collateral"], (
            "the removed row must also show up as an unaccounted change.\n%s"
            % _verdict(result))
        assert result["snapshot"], (
            "an error that says 'restore from the snapshot' must carry one.\n%s"
            % _verdict(result))

    async def test_the_two_untouched_rows_still_went_out_on_the_wire(self, rig):
        """The write itself was innocent: the loss happened server-side."""
        before = base_profile()
        landed = with_row_updated(before, "onlineProfile", "ROW2",
                                  description="desc-two-updated")
        after = without_row(landed, "onlineProfile", "ROW4")
        rig.arm(before, after)

        await psec.write_section(
            "onlineProfiles",
            {"onlineProfileId": "ROW2", "description": "desc-two-updated"},
            confirm=True)

        sent_ids = [r.get("onlineProfileId") for r in rig.sent["onlineProfiles"]]
        assert sent_ids == ["ROW1", "ROW2", "ROW3", "ROW4"], (
            "all four live rows must go on the wire; a whole-list payload "
            "replaces the collection. Sent %r" % (sent_ids,))


# ===========================================================================
# CONTROL 6 -- a genuine non-persistence still fails
#
# The mirror of control 1. The fix removed a check that could not pass; it must
# not have installed one that cannot fail.
# ===========================================================================

class TestANonPersistedWriteStillFails:

    async def test_an_after_read_without_the_change_reports_not_persisted(self, rig):
        before = base_profile()
        after = copy.deepcopy(before)          # the write did NOT land
        rig.arm(before, after)

        result = await psec.write_section(
            "onlineProfiles",
            {"onlineProfileId": "ROW2", "description": "desc-two-updated"},
            confirm=True)

        assert result["verified"] is False, (
            "the after read still shows the OLD description, so nothing "
            "landed. Reporting verified here would make the check "
            "unfailable.\n%s" % _verdict(result))
        assert result["status"] == "error", (
            "an unconfirmed write is an error, not a success.\n%s"
            % _verdict(result))
        assert result["error_code"] == "NOT_PERSISTED", (
            "expected NOT_PERSISTED.\n%s" % _verdict(result))

    async def test_a_write_that_landed_on_the_wrong_row_reports_not_persisted(
            self, rig):
        """Containment must be per-row, not per-collection."""
        before = base_profile()
        after = with_row_updated(before, "onlineProfile", "ROW3",
                                 description="desc-two-updated")
        rig.arm(before, after)

        result = await psec.write_section(
            "onlineProfiles",
            {"onlineProfileId": "ROW2", "description": "desc-two-updated"},
            confirm=True)

        assert result["verified"] is False, (
            "the new description landed on ROW3, not the ROW2 the caller "
            "named. `expected` pins BOTH the id and the value, so this must "
            "not verify.\n%s" % _verdict(result))
        assert result["error_code"] == "NOT_PERSISTED", _verdict(result)


# ===========================================================================
# CONTROL 7 -- the wire keeps the WRITE vocabulary
#
# `expected` moved to the read spelling. `section_body` must not have followed
# it. Two vocabularies, two destinations.
# ===========================================================================

class TestTheWireBodyStaysInTheWriteSpelling:

    async def test_every_row_sent_carries_the_write_id_and_no_read_id(self, rig):
        before = base_profile()
        after = with_row_updated(before, "onlineProfile", "ROW2",
                                 description="desc-two-updated")
        rig.arm(before, after)

        await psec.write_section(
            "onlineProfiles",
            {"onlineProfileId": "ROW2", "description": "desc-two-updated"},
            confirm=True)

        body = rig.sent
        assert list(body) == ["onlineProfiles"], (
            "the body envelope is the WRITE spelling. Got %r" % (list(body),))
        for row in body["onlineProfiles"]:
            assert "onlineProfileId" in row, (
                "a row went out without onlineProfileId: %r. The editor "
                "renames id -> onlineProfileId before sending (bundle "
                "640662); a row without it is a row the server cannot "
                "match." % row)
            assert "id" not in row, (
                "the READ spelling 'id' reached the wire on %r. The editor "
                "deletes it; `expected` is the only place 'id' belongs." % row)
            assert "profileId" not in row, (
                "the editor deletes profileId off every row before sending; "
                "%r still carries it." % row)

    @pytest.mark.parametrize("envelope,read_key_name,id_field,origin", ID_MAPPED)
    async def test_the_same_for_every_id_mapped_collection(
            self, rig, envelope, read_key_name, id_field, origin):
        before = base_profile(read_key_name)
        after = with_row_updated(before, read_key_name, "ROW2",
                                 description="desc-two-updated")
        rig.arm(before, after)

        await psec.write_section(
            envelope, {id_field: "ROW2", "description": "desc-two-updated"},
            confirm=True)

        rows = rig.sent[envelope]
        assert [r.get(id_field) for r in rows] == ["ROW1", "ROW2", "ROW3", "ROW4"], (
            "%r must go out keyed %r, in order (bundle %s). Got %r"
            % (envelope, id_field, origin, rows))
        assert not any("id" in r for r in rows), (
            "a read-spelling 'id' reached the wire for %r: %r" % (envelope, rows))
