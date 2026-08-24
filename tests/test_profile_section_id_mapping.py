"""The READ/WRITE row-id spelling gap, pinned. Fixtures only -- no network.

Naukri's profile READ and the profile WRITE do not spell whole-list row ids the
same way. Measured, both ends:

* `_audit/_slices/naukri-profile-read-shape.md` -- the live read returns section
  `onlineProfile` whose row id field is plainly `id` (a 48-character string),
  NOT `onlineProfileId`. It is the only section whose row id is named `id`.
* `_audit/_slices/naukri-profile-write-contract.md` -- the editor's builder at
  bundle offset 640662 is
  `er=function(a,b){return a.forEach(function(a){a.onlineProfileId=a.id,
  delete a.profileId, delete a.id}), {onlineProfiles:a, profileId:b}}`.
  The same `row.id -> <section>Id` rename is documented for `workSamples`
  (656875), `presentations` (668250), `publications` (681299) and `patents`
  (696760).

Without that mapping `_merge_whole_list` matched the caller's `onlineProfileId`
against live rows that carry only `id`, so NO ROW EVER MATCHED and updating any
of those five collections was impossible: every attempt raised
"No row in ... carries ...". `SectionSpec.read_id_field` plus
`_to_write_row(read_row, spec)` close that gap.

THE MAPPING IS CONFINED ON PURPOSE. When `read_id_field == id_field` the row is
returned unchanged. `languages` reads and writes `languageId` alike and its read
rows carry no `id` key at all, so its rows must pass through untouched --
generalising the strip would invent a contract the evidence does not carry.
Control 2 is that confinement guard.

THE CONTROL THAT MATTERS MOST IS 7. `rows_lost` compares two READ payloads, so
it must be handed the READ spelling. Handed the write spelling it finds no ids
at all, returns an empty list, and a whole-list write that silently deleted rows
reports none lost. That is the exact silent wipe this rails layer exists to
catch, and an empty list is indistinguishable from "nothing was lost".

ZERO NETWORK, ZERO WRITES AGAINST naukri.com. Three seams are mocked and
nothing else -- the profile READ (`api_client` in the module namespace), the
TRANSPORT (`fullprofiles_write`), and the event bus emit -- exactly as
tests/test_profile_sections.py does it. The snapshot layer runs for real
against a tmp_path directory.

EVERY CONTROL HERE WAS SHOWN FAILING FIRST. For each one `profile_sections.py`
was broken deliberately, the single target test was run, the verbatim RED was
recorded, and the source was restored byte-for-byte and re-hashed. The RED table
and the before/after SHA-256 are in
`_audit/_slices/naukri-id-mapping-slice.md`. A check never observed failing
certifies nothing.
"""

import copy
import json

import pytest
from unittest.mock import AsyncMock

from naukri_server.tools import profile_sections as psec
from naukri_server.tools import profile_snapshot as ps

# Stand-ins for live ids. The real profileId is 64 characters and the real
# online-profile id is 48; neither ever appears in a test file, and nothing
# here reaches the wire anyway. The mapping is length-agnostic, so short
# readable tags make a failure message legible.
PROFILE_ID = "0" * 64


# ---------------------------------------------------------------------------
# Fixtures in the READ spelling the API actually returns.
#
# `onlineProfile` is SINGULAR and its rows carry `id`, not `onlineProfileId`.
# Both facts are measured in the read-shape slice, and both are exactly what
# this file exists to pin -- a fixture that spelled them the write way would
# test the mapping against a payload Naukri never sends.
# ---------------------------------------------------------------------------

def _online(row_id, url):
    """One `onlineProfile` row as READ: 5 keys, row id named `id`."""
    return {
        "id": row_id,
        "profileId": PROFILE_ID,
        "profile": "Portfolio",
        "description": "desc-%s" % row_id,
        "url": url,
    }


def _language(row_id, name, proficiency="Beginner"):
    """One `languages` row as READ: row id named `languageId`, no `id` key."""
    return {
        "languageId": row_id,
        "entityLanguageId": "1",
        "profileId": PROFILE_ID,
        "lang": name,
        "proficiency": {"id": 1, "value": proficiency},
        "ability": ["Read", "Write", "Speak"],
    }


def base_profile():
    """Four online-profile rows and three language rows.

    Four is the floor that makes control 5 meaningful: an update touches one
    row, so three others must survive, and an off-by-one merge cannot pass by
    accident at that width.
    """
    return {
        "profile": [{
            "profileId": PROFILE_ID,
            "resumeHeadline": "Backend engineer",
            "summary": "Builds services.",
        }],
        "onlineProfile": [
            _online("OP1", "url-one"),
            _online("OP2", "url-two"),
            _online("OP3", "url-three"),
            _online("OP4", "url-four"),
        ],
        "languages": [
            _language("LANG1", "Lang-One", "Expert"),
            _language("LANG2", "Lang-Two", "Proficient"),
            _language("LANG3", "Lang-Three", "Beginner"),
        ],
    }


def mapped(read_row):
    """What the editor puts on the wire for one read row.

    An INDEPENDENT re-implementation of `er` at bundle offset 640662 -- rename
    `id` to `onlineProfileId`, then delete `id` and `profileId`. Deliberately
    not a call to `_to_write_row`: a test that asserted the code against itself
    would pass for any mapping at all.
    """
    out = {k: v for k, v in read_row.items() if k not in ("id", "profileId")}
    out["onlineProfileId"] = read_row["id"]
    return out


def canonical(value):
    """Byte-exact canonical form, for 'this row is exactly that row' asserts."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def by_write_id(rows):
    return {row.get("onlineProfileId"): row for row in rows}


# ---------------------------------------------------------------------------
# The rig. Same three seams as tests/test_profile_sections.py, same refusal to
# serve a read the test did not arm.
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
        """Queue the reads this call may make, in order.

        Deep-copied, so the profile the module sees is a different object from
        the one the test compares against.
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
    """Redirect the snapshot directory off the repo.

    DATA_DIR is the repo root, so an un-isolated snapshot dirties the checkout
    with a file full of live personal data.
    """
    target = tmp_path / "profile_snapshots"
    monkeypatch.setattr(ps, "_SNAPSHOT_DIR", target)
    return target


@pytest.fixture(autouse=True)
def no_event_fanout(monkeypatch):
    """Record the ProfileUpdated emit; never dispatch it.

    The live subscriber writes a notification row into naukri.db, and a unit
    test must not.
    """
    import naukri_server.events as events
    emit = AsyncMock(return_value=None)
    monkeypatch.setattr(events.event_bus, "emit", emit)
    return emit


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


# ===========================================================================
# CONTROL 1 -- the spec table declares the read spelling of every row id
#
# Table-driven, so a whole-list collection added later cannot reach production
# without someone deciding how its read payload spells the row id.
# ===========================================================================

#: (write key, id_field, read_id_field). The five with a read spelling of `id`
#: are the five builders quoted in the write-contract slice; `languages` is the
#: whole-list collection that spells it the same on both sides.
ID_SPELLINGS = [
    ("onlineProfiles", "onlineProfileId", "id"),
    ("workSamples", "workSampleId", "id"),
    ("presentations", "presentationId", "id"),
    ("publications", "publicationId", "id"),
    ("patents", "patentId", "id"),
    ("languages", "languageId", "languageId"),
]


class TestTheSpecTableDeclaresBothSpellings:

    @pytest.mark.parametrize("section,id_field,read_id_field", ID_SPELLINGS)
    def test_the_declared_spellings_match_the_measured_payloads(
            self, section, id_field, read_id_field):
        spec = psec.SECTION_SPECS[section]
        assert spec.id_field == id_field, (
            "section %r writes its row id as %r; the spec says %r"
            % (section, id_field, spec.id_field))
        assert spec.read_id_field == read_id_field, (
            "section %r is READ with its row id spelled %r; the spec says %r. "
            "Get this wrong and no live row matches a caller's id, so the "
            "collection cannot be updated at all."
            % (section, read_id_field, spec.read_id_field))

    def test_every_whole_list_section_is_in_this_table(self):
        """A new whole-list collection must declare its read spelling here."""
        whole_list = {name for name, spec in psec.SECTION_SPECS.items()
                      if spec.kind == psec.WHOLE_LIST}
        pinned = {name for name, _, _ in ID_SPELLINGS}
        assert whole_list == pinned, (
            "the whole-list sections in the spec table and the sections this "
            "file pins disagree. Only in the module: %r. Only here: %r."
            % (sorted(whole_list - pinned), sorted(pinned - whole_list)))

    def test_no_other_section_declares_a_divergent_read_spelling(self):
        """Confinement at the table level: the asymmetry is whole-list only.

        Every non-whole-list section is a SCALAR_BLOCK or a SINGLE_ROW write,
        and the read-shape capture measured each of those carrying its write
        spelling verbatim. A divergence declared on one of them would send
        `_to_write_row` stripping keys off rows nothing in the bundle strips.
        """
        for section, spec in sorted(psec.SECTION_SPECS.items()):
            if spec.kind == psec.WHOLE_LIST:
                continue
            assert spec.read_id_field == spec.id_field, (
                "section %r is a %s write but declares read_id_field=%r "
                "against id_field=%r" % (section, spec.kind,
                                         spec.read_id_field, spec.id_field))


# ===========================================================================
# CONTROL 2 -- _to_write_row is IDENTITY for languages
#
# THE CONFINEMENT GUARD. `languages` is a whole-list collection like the other
# five, so a refactor that made the strip unconditional would look tidier and
# would start deleting `profileId` off rows the bundle never strips it from.
# ===========================================================================

class TestToWriteRowIsIdentityForLanguages:

    SECTION = "languages"

    def test_a_language_row_passes_through_byte_identical(self):
        spec = psec.SECTION_SPECS[self.SECTION]
        row = _language("LANG1", "Lang-One", "Expert")

        out = psec._to_write_row(row, spec)

        assert canonical(out) == canonical(row), (
            "a languages row was transformed on its way to the wire. It reads "
            "and writes `languageId` alike and the bundle documents no strip "
            "for it, so it must pass through untouched.\nRead: %s\nSent: %s"
            % (canonical(row), canonical(out)))

    def test_a_language_row_keeps_its_profile_id(self):
        """The strip that removes `profileId` is for the other five only."""
        spec = psec.SECTION_SPECS[self.SECTION]
        row = _language("LANG2", "Lang-Two")

        out = psec._to_write_row(row, spec)

        assert "profileId" in out, (
            "`profileId` was stripped off a languages row. Only the five "
            "collections whose read and write spellings differ are stripped "
            "(bundle offsets 640662, 656875, 668250, 681299, 696760); "
            "generalising it invents a contract the evidence does not carry.")

    def test_the_identity_path_still_returns_a_copy(self):
        """Identity must not mean 'hand back the caller's dict'."""
        spec = psec.SECTION_SPECS[self.SECTION]
        row = _language("LANG3", "Lang-Three")

        out = psec._to_write_row(row, spec)
        out["lang"] = "mutated"

        assert row["lang"] == "Lang-Three", (
            "_to_write_row returned the live read row itself, so editing the "
            "payload edits the profile the caller is comparing against")


# ===========================================================================
# CONTROL 3 -- _to_write_row renames and strips for onlineProfiles
#
# Verbatim from the bundle at 640662:
#   a.onlineProfileId = a.id, delete a.profileId, delete a.id
# ===========================================================================

class TestToWriteRowMapsAnOnlineProfileRow:

    SECTION = "onlineProfiles"

    def _mapped(self):
        return psec._to_write_row(_online("OP1", "url-one"),
                                  psec.SECTION_SPECS[self.SECTION])

    def test_the_write_id_is_taken_from_the_read_id(self):
        out = self._mapped()
        assert out.get("onlineProfileId") == "OP1", (
            "the row went to the wire without `onlineProfileId` set from the "
            "read row's `id`: %s" % canonical(out))

    def test_the_read_id_is_removed(self):
        out = self._mapped()
        assert "id" not in out, (
            "`id` survived the mapping; the editor deletes it before sending: "
            "%s" % canonical(out))

    def test_the_profile_id_is_removed(self):
        out = self._mapped()
        assert "profileId" not in out, (
            "`profileId` survived the mapping; the editor deletes it off "
            "every row before sending: %s" % canonical(out))

    def test_every_other_field_survives_untouched(self):
        read_row = _online("OP1", "url-one")

        out = psec._to_write_row(read_row, psec.SECTION_SPECS[self.SECTION])

        for field in ("profile", "description", "url"):
            assert out.get(field) == read_row[field], (
                "field %r was altered by the id mapping: %r -> %r"
                % (field, read_row[field], out.get(field)))
        assert canonical(out) == canonical(mapped(read_row)), (
            "the mapped row does not match the bundle's transform.\n"
            "Contract: %s\nActual:   %s"
            % (canonical(mapped(read_row)), canonical(out)))


# ===========================================================================
# CONTROL 4 -- an update by onlineProfileId MATCHES a row carrying only `id`
#
# This is the whole point of the fix. Before it, this call raised
# "No row in 'onlineProfiles' carries onlineProfileId == 'OP2'" and updating
# any of the five collections was impossible.
#
# These asserts read the SENT BODY, not the returned status: what the mapping
# fixes is what goes on the wire.
# ===========================================================================

class TestAnUpdateMatchesARowThatCarriesOnlyId:

    FIELDS = {"onlineProfileId": "OP2", "url": "url-new"}

    async def _update(self, rig):
        before = base_profile()
        after = copy.deepcopy(before)
        after["onlineProfile"][1]["url"] = "url-new"
        rig.arm(before, after)
        await psec.write_section(
            "onlineProfiles", dict(self.FIELDS), confirm=True)
        return before

    async def test_the_update_reaches_the_transport_at_all(self, rig):
        await self._update(rig)

        assert rig.transport.await_count == 1, (
            "the update never reached the transport. The caller's "
            "`onlineProfileId` matched no live row, because the live rows "
            "carry `id` -- which is the defect the read/write id mapping "
            "exists to remove.")

    async def test_the_targeted_row_is_updated_in_place(self, rig):
        await self._update(rig)

        rows = rig.sent["onlineProfiles"]
        assert [r.get("onlineProfileId") for r in rows] == [
            "OP1", "OP2", "OP3", "OP4"], (
            "the matched row was appended or reordered instead of merged in "
            "place: %s" % canonical([r.get("onlineProfileId") for r in rows]))
        target = by_write_id(rows)["OP2"]
        assert target["url"] == "url-new"
        assert target["description"] == "desc-OP2", (
            "the merge dropped a field the caller did not name; an update is "
            "a merge over the live row, not a replacement of it: %s"
            % canonical(target))

    async def test_the_row_count_is_unchanged(self, rig):
        await self._update(rig)

        rows = rig.sent["onlineProfiles"]
        assert len(rows) == 4, (
            "an update put %d row(s) on the wire; the live list has 4. Naukri "
            "replaces the whole collection with what it receives, so this "
            "deletes %d row(s)." % (len(rows), 4 - len(rows)))


# ===========================================================================
# CONTROL 5 -- the other rows survive that same update, and are mapped
#
# Four live rows in, four on the wire, and the three the caller never named
# arrive in the editor's spelling rather than the reader's.
# ===========================================================================

class TestTheOtherRowsSurviveAndAreMapped:

    FIELDS = {"onlineProfileId": "OP2", "url": "url-new"}
    UNTOUCHED = ("OP1", "OP3", "OP4")

    async def _update(self, rig):
        before = base_profile()
        after = copy.deepcopy(before)
        after["onlineProfile"][1]["url"] = "url-new"
        rig.arm(before, after)
        await psec.write_section(
            "onlineProfiles", dict(self.FIELDS), confirm=True)
        return before

    async def test_four_live_rows_produce_four_rows_on_the_wire(self, rig):
        before = await self._update(rig)

        assert len(rig.sent["onlineProfiles"]) == len(before["onlineProfile"])

    async def test_the_untouched_rows_are_exactly_the_bundles_transform(
            self, rig):
        before = await self._update(rig)

        sent = by_write_id(rig.sent["onlineProfiles"])
        live = {row["id"]: row for row in before["onlineProfile"]}
        for row_id in self.UNTOUCHED:
            assert row_id in sent, (
                "untouched row %s is not on the wire under a WRITE id at all. "
                "Ids sent: %s. A row that kept its READ spelling is a row the "
                "server cannot match."
                % (row_id, sorted(str(k) for k in sent)))
            assert canonical(sent[row_id]) == canonical(mapped(live[row_id])), (
                "untouched row %s did not arrive in the editor's spelling.\n"
                "Contract: %s\nSent:     %s"
                % (row_id, canonical(mapped(live[row_id])),
                   canonical(sent[row_id])))

    async def test_no_untouched_row_still_carries_the_read_only_keys(self, rig):
        await self._update(rig)

        for row in rig.sent["onlineProfiles"]:
            assert "id" not in row and "profileId" not in row, (
                "a row went to the wire still carrying keys the editor "
                "deletes off every row: %s" % canonical(row))

    async def test_the_untouched_rows_keep_their_own_values(self, rig):
        before = await self._update(rig)

        sent = by_write_id(rig.sent["onlineProfiles"])
        live = {row["id"]: row for row in before["onlineProfile"]}
        for row_id in self.UNTOUCHED:
            assert row_id in sent, (
                "untouched row %s is not on the wire under a WRITE id at all. "
                "Ids sent: %s" % (row_id, sorted(str(k) for k in sent)))
            assert sent[row_id]["url"] == live[row_id]["url"], (
                "row %s left with the wrong url: %r, live value %r"
                % (row_id, sent[row_id]["url"], live[row_id]["url"]))


# ===========================================================================
# CONTROL 6 -- a CREATE appends and leaves the existing rows intact and mapped
#
# A create carries no id, so it never exercises the MATCH half of the mapping.
# The rename still has to run over every live row, or the append rebuilds the
# list in the read spelling and the server sees four unrecognised rows.
# ===========================================================================

class TestACreateAppendsAndLeavesTheRestMapped:

    NEW_ROW = {"profile": "Repo", "url": "url-five", "description": "desc-new"}

    async def _create(self, rig):
        before = base_profile()
        after = copy.deepcopy(before)
        after["onlineProfile"].append(_online("OP5", "url-five"))
        rig.arm(before, after)
        await psec.write_section(
            "onlineProfiles", dict(self.NEW_ROW), confirm=True)
        return before

    async def test_the_new_row_makes_five(self, rig):
        await self._create(rig)

        rows = rig.sent["onlineProfiles"]
        assert len(rows) == 5, (
            "creating a whole-list row sent %d row(s); 4 live + 1 new = 5"
            % len(rows))

    async def test_the_new_row_is_last_and_carries_no_id(self, rig):
        await self._create(rig)

        new = rig.sent["onlineProfiles"][-1]
        assert "onlineProfileId" not in new and "id" not in new, (
            "the created row went out carrying an id, which makes it an "
            "update against a row that does not exist: %s" % canonical(new))
        assert new["url"] == "url-five"

    async def test_the_four_existing_rows_are_still_mapped(self, rig):
        before = await self._create(rig)

        live = {row["id"]: row for row in before["onlineProfile"]}
        sent = by_write_id(rig.sent["onlineProfiles"][:4])
        for row_id in sorted(live):
            assert row_id in sent, (
                "existing row %s is not on the wire under a WRITE id at all. "
                "Ids sent: %s. Appending must not skip the rename over the "
                "rows it keeps." % (row_id, sorted(str(k) for k in sent)))
            assert canonical(sent[row_id]) == canonical(mapped(live[row_id])), (
                "appending a row left existing row %s in the READ spelling.\n"
                "Contract: %s\nSent:     %s"
                % (row_id, canonical(mapped(live[row_id])),
                   canonical(sent[row_id])))


# ===========================================================================
# CONTROL 7 -- rows_lost is handed the READ spelling
#
# THE MOST IMPORTANT CONTROL IN THIS FILE. `rows_lost` compares the before-read
# with the after-read, both of which spell the row id `id`. Handed
# `onlineProfileId` it finds that key on no row at all, skips every row as
# unidentified, and returns [] -- which is exactly what "nothing was lost"
# looks like. A whole-list write that deleted three rows would report none.
# ===========================================================================

class TestRowsLostUsesTheReadSpelling:

    FIELDS = {"onlineProfileId": "OP2", "url": "url-new"}

    def _reads_with_op4_gone(self):
        before = base_profile()
        after = copy.deepcopy(before)
        after["onlineProfile"][1]["url"] = "url-new"     # the intended change
        after["onlineProfile"] = [r for r in after["onlineProfile"]
                                  if r["id"] != "OP4"]    # and a casualty
        return before, after

    async def test_a_row_that_vanished_between_the_two_reads_is_named(
            self, rig):
        before, after = self._reads_with_op4_gone()
        rig.arm(before, after)

        result = await psec.write_section(
            "onlineProfiles", dict(self.FIELDS), confirm=True)

        assert result["rows_lost"] == ["OP4"], (
            "a row present in the before-read and gone from the after-read "
            "was reported as %r. Handed the write spelling, rows_lost matches "
            "no row at all, and an empty list is indistinguishable from "
            "'nothing was lost' -- the silent wipe this layer exists to catch."
            % result["rows_lost"])

    async def test_a_vanished_row_never_reports_success(self, rig):
        before, after = self._reads_with_op4_gone()
        rig.arm(before, after)

        result = await psec.write_section(
            "onlineProfiles", dict(self.FIELDS), confirm=True)

        assert result["status"] == "error", (
            "a row disappeared and the write reported %r"
            % result.get("status"))

    async def test_the_spelling_handed_to_rows_lost_is_the_read_one(
            self, rig, monkeypatch):
        """Pin the call site itself, not just its result.

        The end-to-end assert above can only see an empty list; this one names
        the argument, so a failure says which spelling was passed.
        """
        seen = []
        real = ps.rows_lost

        def spy(before, after, section, id_field):
            seen.append((section, id_field))
            return real(before, after, section, id_field)

        monkeypatch.setattr(psec, "rows_lost", spy)
        before, after = self._reads_with_op4_gone()
        rig.arm(before, after)

        await psec.write_section(
            "onlineProfiles", dict(self.FIELDS), confirm=True)

        assert seen == [("onlineProfiles", "id")], (
            "rows_lost was called as %r. It compares two READ payloads, so it "
            "needs the READ spelling of the row id; the write spelling "
            "matches nothing and silently reports no losses." % (seen,))

    async def test_the_identity_collection_is_still_checked_correctly(
            self, rig):
        """languages reads and writes `languageId`, so its call is unchanged."""
        before = base_profile()
        after = copy.deepcopy(before)
        after["languages"][2]["proficiency"] = {"id": 1, "value": "Expert"}
        after["languages"] = [r for r in after["languages"]
                              if r["languageId"] != "LANG1"]
        rig.arm(before, after)

        result = await psec.write_section(
            "languages",
            {"languageId": "LANG3",
             "proficiency": {"id": 1, "value": "Expert"}},
            confirm=True)

        assert result["rows_lost"] == ["LANG1"], (
            "the identity collection lost a row and reported %r"
            % result["rows_lost"])


# ===========================================================================
# CONTROL 8 -- the refusal names the collection AND the read spelling
#
# An id that matches nothing is refused rather than appended, and the refusal
# has to be actionable: which collection, and what the read payload calls the
# id the caller is being asked for.
# ===========================================================================

class TestTheRefusalNamesTheCollectionAndTheReadSpelling:

    UNKNOWN = {"onlineProfileId": "OP9", "url": "url-new"}

    async def _refusal(self, rig):
        rig.arm(base_profile())
        return await psec.write_section("onlineProfiles", dict(self.UNKNOWN))

    async def test_an_unmatched_id_is_refused(self, rig):
        result = await self._refusal(rig)

        assert result["status"] == "error", (
            "an id matching no live row returned %r; silently appending "
            "orphans the edit the caller meant to make" % result.get("status"))
        assert result["error_code"] == "VALIDATION_ERROR"

    async def test_the_message_names_the_collection_and_the_id(self, rig):
        result = await self._refusal(rig)

        assert "onlineProfiles" in result["message"], (
            "the refusal does not name the collection: %r" % result["message"])
        assert "OP9" in result["message"], (
            "the refusal does not name the id that failed to match: %r"
            % result["message"])

    async def test_the_message_names_the_read_spelling_of_the_id(self, rig):
        """Without this, the caller is told an id does not exist while looking
        at a profile read where every row plainly has one -- under another
        name. The refusal has to bridge the two vocabularies."""
        result = await self._refusal(rig)

        assert "'id'" in result["message"], (
            "the refusal names the WRITE spelling only. The read payload "
            "spells this row id `id`, and a caller comparing the message "
            "against a profile read has no way to connect the two: %r"
            % result["message"])

    async def test_a_refused_write_never_reaches_the_transport(self, rig):
        rig.arm(base_profile(), base_profile())

        await psec.write_section(
            "onlineProfiles", dict(self.UNKNOWN), confirm=True)

        assert rig.transport.await_count == 0, (
            "a refused write still went to the wire")
