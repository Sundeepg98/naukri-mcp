"""The write-safety rails, pinned. Fixtures only - no network, no live profile.

Eight controls, one per rail the profile-write layer depends on. Every one of
them was SHOWN FAILING before it was allowed to stay: the source was broken
deliberately (collateral forced empty, the traversal guard turned into a
no-op, the plural mapping dropped, prune's floor removed, and so on), the test
run went red, and the observed message was recorded. The full RED table is in
`_audit/_slices/naukri-snapshot-rails-slice.md`. A check that has never been
seen failing certifies nothing, and this repo has already produced more than
seven of those.

The control that matters most is #5. A silent deletion is INVISIBLE from the
write response: the six whole-list sections are sent as complete arrays, so a
payload carrying one row deletes the others and the API returns success. Only
a before/after comparison can say so, and `collateral` is that comparison.

PURE: dict arithmetic plus a tmp_path directory. The snapshot directory is
redirected off the repo for every test in this module.
"""

import json
from pathlib import Path

import pytest

from naukri_server.config import DATA_DIR
from naukri_server.tools import profile_snapshot as ps

REPO = Path(__file__).resolve().parent.parent
GITIGNORE = REPO / ".gitignore"


def _ignored_names() -> set:
    lines = GITIGNORE.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


@pytest.fixture(autouse=True)
def snapshot_home(monkeypatch, tmp_path):
    """Redirect the snapshot directory off the repo for every test here.

    Same treatment conftest gives `agent.POLICY_STATE_PATH`, and for the same
    reason: DATA_DIR is the repo root, so an un-isolated write dirties the
    checkout. `test_the_suite_never_writes_into_the_real_snapshot_dir` below is
    what notices if this fixture is ever removed.
    """
    target = tmp_path / "profile_snapshots"
    monkeypatch.setattr(ps, "_SNAPSHOT_DIR", target)
    return target


# ---------------------------------------------------------------------------
# Fixtures - synthetic profiles in the READ spelling the API actually returns.
# `profileId` is stamped on every row on purpose: the live payload does that,
# and it is why a row-id detector cannot just take the first *Id key it sees.
# ---------------------------------------------------------------------------

PROFILE_ID = "P-ANCHOR"


def _online_row(row_id, kind, url):
    return {"id": row_id, "profileId": PROFILE_ID,
            "profileType": kind, "url": url}


def _language_row(row_id, name, proficiency="Expert"):
    return {"languageId": row_id, "profileId": PROFILE_ID,
            "language": name, "proficiency": proficiency}


def _base_profile():
    """Three online profiles, five languages, one `profile` row."""
    return {
        "profile": [{
            "profileId": PROFILE_ID,
            "resumeHeadline": "Backend engineer",
            "summary": "Builds services.",
        }],
        "onlineProfile": [
            _online_row("A1", "Portfolio", "url-a"),
            _online_row("B2", "Repo", "url-b"),
            _online_row("C3", "Blog", "url-c"),
        ],
        "languages": [_language_row("L%d" % n, "Lang%d" % n) for n in range(1, 6)],
        "employments": [{
            "employmentId": "E1", "profileId": PROFILE_ID,
            "organization": "OldCo", "designation": "Engineer",
        }],
    }


def _plant(directory: Path, stamp: str, label: str) -> str:
    """Write a well-formed snapshot with a CHOSEN timestamp.

    Ordering assertions must not depend on how fast the clock ticks between two
    `save_snapshot` calls, so the ordering tests plant names instead.
    """
    directory.mkdir(parents=True, exist_ok=True)
    name = "profile-%s__%s__deadbeef.json" % (stamp, label)
    (directory / name).write_text(json.dumps({
        "snapshot_version": ps.SNAPSHOT_VERSION,
        "taken_at": "2026-08-24T00:00:00+00:00",
        "label": label,
        "sections": ["profile"],
        "profile": {"profile": []},
    }), encoding="utf-8")
    return name


# ---------------------------------------------------------------------------
# CONTROL 1 - a snapshot round-trips
# ---------------------------------------------------------------------------

class TestSnapshotRoundTrip:
    async def test_save_then_load_returns_the_same_content(self, snapshot_home):
        profile = _base_profile()

        meta = await ps.save_snapshot(profile, "before-employment-write")

        loaded = ps.load_snapshot(meta["file"])
        assert loaded["profile"] == profile
        assert loaded["snapshot_version"] == 1
        assert loaded["label"] == "before-employment-write"
        assert loaded["sections"] == sorted(profile.keys())
        assert loaded["taken_at"].endswith("+00:00")
        assert meta["section_count"] == 4
        assert (snapshot_home / meta["file"]).exists()

    async def test_the_metadata_never_carries_the_profile_body(self, snapshot_home):
        meta = await ps.save_snapshot(_base_profile(), "no-body")
        assert "profile" not in meta
        listed = ps.list_snapshots()
        assert len(listed) == 1
        assert "profile" not in listed[0]
        assert listed[0]["readable"] is True
        assert listed[0]["bytes"] > 0

    async def test_two_saves_in_the_same_label_do_not_collide(self, snapshot_home):
        first = await ps.save_snapshot(_base_profile(), "same-label")
        second = await ps.save_snapshot(_base_profile(), "same-label")
        assert first["file"] != second["file"]
        assert len(list(snapshot_home.glob("profile-*.json"))) == 2

    def test_listing_orders_newest_first(self, snapshot_home):
        old = _plant(snapshot_home, "20260101T000000000000Z", "old")
        new = _plant(snapshot_home, "20260824T000000000000Z", "new")
        mid = _plant(snapshot_home, "20260601T000000000000Z", "mid")
        assert [e["file"] for e in ps.list_snapshots()] == [new, mid, old]

    def test_a_corrupt_snapshot_is_listed_not_crashed_on(self, snapshot_home):
        _plant(snapshot_home, "20260101T000000000000Z", "fine")
        (snapshot_home / "profile-20260102T000000000000Z__broken__ffffffff.json"
         ).write_text("{not json", encoding="utf-8")
        listed = ps.list_snapshots()
        assert [e["readable"] for e in listed] == [False, True]


# ---------------------------------------------------------------------------
# CONTROL 2 - load_snapshot refuses to escape its directory
# ---------------------------------------------------------------------------

class TestSnapshotNameIsConfined:
    @pytest.mark.parametrize("name", [
        "../secrets.json",              # parent reference, posix separator
        "..\\secrets.json",             # parent reference, windows separator
        "..",                           # the parent itself
        "/etc/passwd",                  # absolute, posix
        "C:\\Windows\\win.ini",         # drive letter, windows separator
        "C:/Windows/win.ini",           # drive letter, posix separator
        "sub/dir/file.json",            # any nesting at all
        "",                             # empty
    ])
    def test_load_snapshot_refuses(self, name):
        with pytest.raises(ValueError):
            ps.load_snapshot(name)

    def test_CONTROL_a_legitimate_bare_name_is_accepted(self, snapshot_home):
        """The other direction. A guard that rejects everything is not a guard
        - it is a broken feature that happens to look safe."""
        planted = _plant(snapshot_home, "20260824T000000000000Z", "ok")
        assert ps.load_snapshot(planted)["label"] == "ok"

    def test_a_confined_but_missing_name_is_a_different_error(self, snapshot_home):
        """Not-found must not be reported as a traversal attempt, or the real
        traversal message stops meaning anything."""
        snapshot_home.mkdir(parents=True, exist_ok=True)
        with pytest.raises(FileNotFoundError):
            ps.load_snapshot("profile-20260824T000000000000Z__gone__aaaaaaaa.json")


# ---------------------------------------------------------------------------
# CONTROL 3 - prune keeps exactly N newest, and refuses to keep none
# ---------------------------------------------------------------------------

class TestPrune:
    def test_prune_keeps_exactly_the_n_newest(self, snapshot_home):
        planted = [
            _plant(snapshot_home, "2026010%dT000000000000Z" % n, "s%d" % n)
            for n in range(1, 6)
        ]
        assert len(list(snapshot_home.glob("profile-*.json"))) == 5

        deleted = ps.prune_snapshots(2)

        assert deleted == 3
        survivors = sorted(p.name for p in snapshot_home.glob("profile-*.json"))
        assert survivors == sorted(planted[-2:])

    @pytest.mark.parametrize("keep", [0, -1, -99])
    def test_prune_raises_when_keep_is_below_one(self, snapshot_home, keep):
        _plant(snapshot_home, "20260101T000000000000Z", "only")
        with pytest.raises(ValueError):
            ps.prune_snapshots(keep)
        assert len(list(snapshot_home.glob("profile-*.json"))) == 1

    def test_prune_on_a_missing_directory_deletes_nothing(self, snapshot_home):
        assert not snapshot_home.exists()
        assert ps.prune_snapshots(3) == 0

    def test_prune_keeps_everything_when_keep_exceeds_the_count(self, snapshot_home):
        _plant(snapshot_home, "20260101T000000000000Z", "a")
        _plant(snapshot_home, "20260102T000000000000Z", "b")
        assert ps.prune_snapshots(10) == 0
        assert len(list(snapshot_home.glob("profile-*.json"))) == 2


# ---------------------------------------------------------------------------
# CONTROL 4 - verify_write is False when the expected change did not land
# ---------------------------------------------------------------------------

class TestVerifyWrite:
    def test_verified_is_false_when_the_expected_change_is_absent(self):
        before = _base_profile()
        after = _base_profile()  # the write did nothing

        result = ps.verify_write(before, after, "profile",
                                 {"resumeHeadline": "Staff engineer"})

        assert result["verified"] is False
        assert "NOT all present" in result["reason"]
        assert "resumeHeadline" in result["reason"]
        assert result["collateral"] == []

    def test_verified_is_true_when_it_did_land(self):
        before = _base_profile()
        after = _base_profile()
        after["profile"][0]["resumeHeadline"] = "Staff engineer"

        result = ps.verify_write(before, after, "profile",
                                 {"resumeHeadline": "Staff engineer"})

        assert result["verified"] is True
        assert result["collateral"] == []
        assert "all present" in result["reason"]

    def test_an_empty_expected_never_certifies_anything(self):
        before = _base_profile()
        result = ps.verify_write(before, _base_profile(), "profile", {})
        assert result["verified"] is False
        assert "empty" in result["reason"]

    def test_a_section_missing_after_the_write_is_not_verified(self):
        before = _base_profile()
        after = _base_profile()
        del after["employments"]

        result = ps.verify_write(before, after, "employments",
                                 {"organization": "NewCo"})

        assert result["verified"] is False
        assert "ABSENT" in result["reason"]
        assert result["collateral"], "losing the whole section is collateral"

    def test_the_reason_names_the_whole_list_hazard(self):
        before = _base_profile()
        result = ps.verify_write(before, _base_profile(), "onlineProfiles",
                                 {"url": "url-a"})
        assert "WHOLE LIST" in result["reason"]


# ---------------------------------------------------------------------------
# CONTROL 5 - collateral catches a silent deletion. THE ONE THAT MATTERS.
# ---------------------------------------------------------------------------

class TestCollateralCatchesSilentDeletion:
    def test_expected_change_lands_but_a_row_vanished(self):
        """The exact shipping accident: a whole-list section is written with
        the row the caller cared about, the intended field really does change,
        the API says success -- and an unrelated row is gone. `verified` is
        True on the expected half and MUST NOT be the whole answer."""
        before = _base_profile()
        after = _base_profile()
        after["onlineProfile"][0]["url"] = "url-a-new"   # the intended change
        after["onlineProfile"] = [r for r in after["onlineProfile"]
                                  if r["id"] != "C3"]    # the silent casualty

        result = ps.verify_write(before, after, "onlineProfiles",
                                 {"url": "url-a-new"})

        assert result["verified"] is True, "the expected half did land"
        assert result["collateral"], "and the vanished row must still be caught"

        paths = [c["path"] for c in result["collateral"]]
        assert any("C3" in p for p in paths), (
            "the vanished row is not NAMED in collateral: %s" % paths)
        assert all(c["kind"] == "removed" for c in result["collateral"])
        assert any(c["value"].get("url") == "url-c"
                   for c in result["collateral"]), "the lost row is returned whole"

    def test_the_intended_change_alone_produces_no_collateral(self):
        """The other direction: a clean write must come back with an EMPTY
        collateral, or the detector is just noise nobody will read."""
        before = _base_profile()
        after = _base_profile()
        after["onlineProfile"][0]["url"] = "url-a-new"

        result = ps.verify_write(before, after, "onlineProfiles",
                                 {"url": "url-a-new"})

        assert result["verified"] is True
        assert result["collateral"] == []

    def test_an_unrelated_field_edit_is_collateral_too(self):
        """Deletion is the worst case, not the only one."""
        before = _base_profile()
        after = _base_profile()
        after["onlineProfile"][0]["url"] = "url-a-new"
        after["onlineProfile"][1]["profileType"] = "Clobbered"

        result = ps.verify_write(before, after, "onlineProfiles",
                                 {"url": "url-a-new"})

        assert result["verified"] is True
        assert [c["kind"] for c in result["collateral"]] == ["changed"]
        assert "B2" in result["collateral"][0]["path"]


# ---------------------------------------------------------------------------
# CONTROL 6 - rows_lost on a whole-list overwrite that kept 1 of 5
# ---------------------------------------------------------------------------

class TestRowsLost:
    def test_returns_exactly_the_dropped_ids(self):
        before = _base_profile()
        after = _base_profile()
        after["languages"] = [r for r in after["languages"]
                              if r["languageId"] == "L3"]

        lost = ps.rows_lost(before, after, "languages", "languageId")

        assert lost == ["L1", "L2", "L4", "L5"]

    def test_returns_nothing_when_the_list_survived(self):
        assert ps.rows_lost(_base_profile(), _base_profile(),
                            "languages", "languageId") == []

    def test_a_whole_section_wiped_loses_every_row(self):
        after = _base_profile()
        after["languages"] = []
        assert ps.rows_lost(_base_profile(), after, "languages",
                            "languageId") == ["L1", "L2", "L3", "L4", "L5"]

    def test_it_resolves_the_write_spelling_too(self):
        """The caller holds a WRITE payload, so it names the section the write
        way; the profile it compares was READ, so it holds the other."""
        before = _base_profile()
        after = _base_profile()
        after["onlineProfile"] = [after["onlineProfile"][0]]
        assert ps.rows_lost(before, after, "onlineProfiles", "id") == ["B2", "C3"]

    def test_asking_a_non_collection_raises_rather_than_reassures(self):
        before = _base_profile()
        before["employments"] = {"organization": "OldCo"}
        with pytest.raises(ValueError):
            ps.rows_lost(before, _base_profile(), "employments", "employmentId")

    def test_an_empty_id_field_raises(self):
        with pytest.raises(ValueError):
            ps.rows_lost(_base_profile(), _base_profile(), "languages", "")


# ---------------------------------------------------------------------------
# CONTROL 7 - the singular/plural mapping, both directions plus identity
# ---------------------------------------------------------------------------

#: The five measured asymmetric collections. read-shape doc lists the singular
#: spellings among its 21 top-level sections; write-contract rows 13-17 carry
#: the plural ones.
ASYMMETRIC = [
    ("onlineProfiles", "onlineProfile"),
    ("patents", "patent"),
    ("presentations", "presentation"),
    ("publications", "publication"),
    ("workSamples", "workSample"),
]

#: Sections spelled identically on both sides. `languages` is here AND is a
#: whole-list section - the combination is exactly why it gets forgotten.
SAME_NAME = ["employments", "educations", "schools", "itskills", "projects",
             "certifications", "languages", "profile"]


class TestReadWriteKeyMapping:
    @pytest.mark.parametrize("write,read", ASYMMETRIC)
    def test_the_mapping_holds_both_ways(self, write, read):
        assert ps.READ_KEY_FOR_WRITE_KEY[write] == read
        assert ps.WRITE_KEY_FOR_READ_KEY[read] == write

    @pytest.mark.parametrize("write,read", ASYMMETRIC)
    def test_either_spelling_resolves_to_either_key(self, write, read):
        assert ps.read_key(write) == read
        assert ps.read_key(read) == read
        assert ps.write_key(read) == write
        assert ps.write_key(write) == write

    @pytest.mark.parametrize("section", SAME_NAME)
    def test_same_name_sections_pass_through_unchanged(self, section):
        assert ps.read_key(section) == section
        assert ps.write_key(section) == section

    def test_the_mapping_covers_exactly_the_five_measured_collections(self):
        assert set(ps.READ_KEY_FOR_WRITE_KEY) == {w for w, _ in ASYMMETRIC}
        assert len(ps.WRITE_KEY_FOR_READ_KEY) == 5

    @pytest.mark.parametrize("spelling", ["onlineProfile", "onlineProfiles"])
    def test_every_rail_accepts_either_spelling(self, spelling):
        """The mapping is worthless if only one function consults it."""
        before = _base_profile()
        after = _base_profile()
        after["onlineProfile"] = [r for r in after["onlineProfile"]
                                  if r["id"] != "C3"]

        assert ps.rows_lost(before, after, spelling, "id") == ["C3"]
        assert ps.diff_section(before, after, spelling)["removed"]
        assert ps.verify_write(before, after, spelling,
                               {"url": "url-a"})["collateral"]

    def test_the_six_whole_list_sections_are_the_measured_six(self):
        assert ps.WHOLE_LIST_SECTIONS == frozenset((
            "onlineProfiles", "workSamples", "presentations",
            "publications", "patents", "languages"))


# ---------------------------------------------------------------------------
# CONTROL 8 - nothing is written inside the source tree
# ---------------------------------------------------------------------------

class TestNoTreeWrites:
    def test_the_snapshot_dir_is_data_dir_relative(self, monkeypatch):
        """Pins WHERE snapshots live and which config constant decided it.

        `DATA_DIR` is the repo root by default - the same fact that made
        `agent_policy_state.json` land in the checkout. That is why the
        gitignore entry below is load-bearing rather than tidy.
        """
        monkeypatch.undo()
        assert ps._SNAPSHOT_DIR == DATA_DIR / "profile_snapshots"
        assert DATA_DIR == REPO

    def test_the_snapshot_dir_is_gitignored(self):
        assert "profile_snapshots/" in _ignored_names(), (
            "profile_snapshots/ holds the FULL live profile read and is "
            "written into DATA_DIR, which is the repo root. Un-ignored, the "
            "next `git add -A` commits his employment history."
        )

    async def test_the_suite_never_writes_into_the_real_snapshot_dir(self):
        """Runs a whole save/list/prune cycle and proves the checkout is
        untouched. Fails the moment the autouse redirect is removed."""
        real = DATA_DIR / "profile_snapshots"
        before = sorted(p.name for p in real.iterdir()) if real.exists() else None

        assert ps.snapshot_dir() != real, "the autouse redirect is not in effect"
        await ps.save_snapshot(_base_profile(), "isolation-probe")
        ps.list_snapshots()
        ps.prune_snapshots(1)

        after = sorted(p.name for p in real.iterdir()) if real.exists() else None
        assert after == before, (
            "the test run created or changed files in %s" % real)

    def test_snapshot_dir_is_pure(self, snapshot_home):
        """Resolving the path must not create it. Import-time or list-time
        directory creation is how a runtime dir sneaks into a checkout."""
        assert not snapshot_home.exists()
        ps.snapshot_dir()
        ps.list_snapshots()
        assert not snapshot_home.exists()


# ---------------------------------------------------------------------------
# The diff itself - the generic machinery the four rails above stand on
# ---------------------------------------------------------------------------

class TestDiffSection:
    def test_it_reports_added_removed_changed_and_unchanged(self):
        before = _base_profile()
        after = _base_profile()
        after["onlineProfile"][0]["url"] = "url-a-new"
        after["onlineProfile"][1]["note"] = "added key"
        del after["onlineProfile"][2]["profileType"]

        d = ps.diff_section(before, after, "onlineProfile")

        assert [c["path"] for c in d["changed"]] == ["onlineProfile[id=A1].url"]
        assert [a["path"] for a in d["added"]] == ["onlineProfile[id=B2].note"]
        assert [r["path"] for r in d["removed"]] == [
            "onlineProfile[id=C3].profileType"]
        # 3 rows x 4 keys = 12 leaves; one changed and one removed leaves 10.
        assert d["unchanged_count"] == 10
        assert d["id_field"] == "id"

    def test_a_deleted_middle_row_is_one_removal_not_a_shift_cascade(self):
        """Positional matching would report the tail rows as changed and the
        last one as removed, naming the wrong row every time."""
        before = _base_profile()
        after = _base_profile()
        after["onlineProfile"] = [r for r in after["onlineProfile"]
                                  if r["id"] != "B2"]

        d = ps.diff_section(before, after, "onlineProfile")

        assert d["changed"] == []
        assert [r["path"] for r in d["removed"]] == ["onlineProfile[id=B2]"]

    def test_the_row_id_detector_ignores_the_profile_level_anchor(self):
        """`profileId` is stamped identically on every row of every section. A
        detector that took the first *Id key would match every row to every
        other and report a clean diff on a wiped collection."""
        rows = _base_profile()["onlineProfile"]
        assert ps.detect_id_field(rows, rows) == "id"

    def test_the_detector_declines_when_one_row_cannot_discriminate(self):
        rows = [_online_row("A1", "Portfolio", "url-a")]
        assert ps.detect_id_field(rows, rows) is None

    def test_it_knows_no_field_names(self):
        """Generic over shape: a section it has never seen diffs the same way."""
        before = {"invented": [{"widgetId": "W1", "size": 1},
                               {"widgetId": "W2", "size": 2}]}
        after = {"invented": [{"widgetId": "W1", "size": 9}]}

        d = ps.diff_section(before, after, "invented")

        assert [c["path"] for c in d["changed"]] == ["invented[widgetId=W1].size"]
        assert [r["path"] for r in d["removed"]] == ["invented[widgetId=W2]"]

    def test_a_dict_section_diffs_by_path(self):
        before = {"user": {"mobile": "1", "email": "a"}}
        after = {"user": {"mobile": "2", "email": "a"}}
        d = ps.diff_section(before, after, "user")
        assert d["changed"] == [{"path": "user.mobile", "from": "1", "to": "2"}]
        assert d["unchanged_count"] == 1

    def test_a_list_emptied_is_a_visible_change(self):
        before = _base_profile()
        after = _base_profile()
        after["languages"] = []
        d = ps.diff_section(before, after, "languages")
        assert d["removed"], "emptying a collection must not diff as silence"
