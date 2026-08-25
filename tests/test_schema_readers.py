"""jobcore's schema declares WHO reads each key. This checks naukri actually does.

M3 in the review: the anti-decoy rule is "a key enters only when it encodes a
judgement AND at least one live consumer reads it", and a shared schema makes
that easy to violate from a distance — jobcore can declare `readers=("naukri",)`
on a key naukri has never heard of, and nothing notices.

Two directions, both mechanical:

  * every `servers.naukri.*` key the schema declares with naukri as a reader is
    either READ here, or listed in NOT_YET_WIRED with a reason. The list is the
    honest form of "not done"; silence is the dishonest one.

  * the agent subtree is EXEMPT and must stay exempt — those keys are tier C,
    naukri deliberately does NOT read them from the file, and a census that
    demanded a reader for them would push someone to add one.

All PURE: schema introspection + a text scan over this package.
"""

import re
from pathlib import Path

import pytest

from jobcore.policy import SCHEMA, TIER_C

PKG = Path(__file__).resolve().parent.parent / "naukri_server"

#: Declared for naukri, not yet read here, each with the reason. A key may sit
#: in this list; it may not sit nowhere.
NOT_YET_WIRED = {
    "servers.naukri.daily_apply_quota":
        "read from config.DAILY_APPLY_QUOTA today; moving the daily cap onto "
        "the file is an apply-side change and belongs with a review of the "
        "quota logic, not inside the mechanism commit",
    "servers.naukri.follow_up.auto_draft_at":
        "follow-up drafting thresholds are computed inside "
        "compute_follow_up_priority; wiring them needs that function's scoring "
        "reviewed first",
    "servers.naukri.follow_up.notify_at": "see follow_up.auto_draft_at",
    "servers.naukri.follow_up.template_id": "see follow_up.auto_draft_at",
    "servers.naukri.saved_jobs.expiry_days":
        "saved-job expiry is derived from Naukri's own returned dates today, "
        "not from a local constant; there is nothing to override yet",
    "servers.naukri.saved_jobs.warn_days": "see saved_jobs.expiry_days",
    "servers.naukri.daily_brief.hour":
        "the brief's schedule is owned by @scheduled_task(run_at_hour=8), "
        "which is evaluated at import; making it dynamic means touching the "
        "scheduler registry",
    "servers.naukri.boost_profile.hour":
        "same as daily_brief.hour — owned by the @scheduled_task decorator's "
        "run_at_hour, which is evaluated at import time",
    "servers.naukri.agent.blocklist.companies":
        "blocklist lives in agent_config.json beside the rest of the agent "
        "block; splitting it across two files would be worse than either file",
    "servers.naukri.agent.blocklist.title_keywords": "see blocklist.companies",
    "servers.naukri.agent.max_daily_applications": "see blocklist.companies",
    "servers.naukri.agent.cycle_interval_hours":
        "owned by @scheduled_task(interval_seconds=...), evaluated at import",
    "servers.naukri.agent.quiet_hours.enabled": "see blocklist.companies",
    "servers.naukri.agent.quiet_hours.start_hour": "see blocklist.companies",
    "servers.naukri.agent.quiet_hours.end_hour": "see blocklist.companies",
    "servers.naukri.agent.quiet_hours.tz": "see blocklist.companies",
}


def _naukri_keys():
    out = []
    for path, spec in SCHEMA.items():
        if spec.is_pattern or not path.startswith("servers.naukri."):
            continue
        if "naukri" not in spec.readers:
            continue
        out.append(path)
    return sorted(out)


def _package_text() -> str:
    parts = []
    for p in sorted(PKG.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


PACKAGE_TEXT = _package_text()
NAUKRI_KEYS = _naukri_keys()


AGENT_PREFIX = "servers.naukri.agent."


def _agent_keys_read_from_the_file() -> frozenset:
    """The agent keys naukri takes from the shared file, from the horse's mouth.

    `agent.FILE_DECIDABLE_KEYS` IS the merge -- `load_agent_config` iterates it
    -- so this is the authoritative answer, not an inference about one.
    """
    from naukri_server.agent import FILE_DECIDABLE_KEYS

    return frozenset(FILE_DECIDABLE_KEYS)


def _is_read(path: str) -> bool:
    """Does naukri read this key from the config file?

    Two different questions, deliberately answered two different ways.

    Under the agent subtree the answer is AUTHORITATIVE, never textual. The
    text scan is a heuristic and it lied here the moment the six became
    loadable: `naukri_server/policy.py` names
    "agent.max_daily_applications" in NOT_LOADABLE -- a statement that the file
    does NOT decide it -- and a substring search read that as evidence that it
    DOES. A census that counts a refusal as a read is worse than no census, so
    for this subtree the check asks `FILE_DECIDABLE_KEYS` instead.

    Everywhere else the text scan stands: those keys are read through
    `setting("<tail>", default)`, so the quoted tail really is the call site.
    """
    if path.startswith(AGENT_PREFIX):
        return path[len(AGENT_PREFIX):] in _agent_keys_read_from_the_file()
    tail = path[len("servers.naukri."):]
    return bool(re.search(rf'["\']{re.escape(tail)}["\']', PACKAGE_TEXT))


class TestEveryDeclaredReaderIsReal:
    def test_the_census_found_keys_to_check(self):
        assert len(NAUKRI_KEYS) > 15, NAUKRI_KEYS

    @pytest.mark.parametrize("path", NAUKRI_KEYS)
    def test_key_is_read_or_explicitly_deferred(self, path):
        if _is_read(path):
            return
        assert path in NOT_YET_WIRED, (
            f"{path} declares naukri as a reader and naukri does not read it. "
            f"Either wire it, or add it to NOT_YET_WIRED with the reason — a "
            f"declared-and-unread key is the decoy class in a new coat."
        )
        assert len(NOT_YET_WIRED[path]) > 20, "give a real reason, not a shrug"

    def test_the_deferral_list_has_no_stale_entries(self):
        """CONTROL in the other direction: once a key IS wired it must leave
        the list, or the list becomes a place where excuses go to retire."""
        stale = [k for k in NOT_YET_WIRED if _is_read(k)]
        assert not stale, f"now read, remove from NOT_YET_WIRED: {stale}"

    def test_the_deferral_list_names_only_real_schema_keys(self):
        unknown = [k for k in NOT_YET_WIRED if k not in SCHEMA]
        assert not unknown, unknown

    def test_the_read_check_can_fail(self):
        """CONTROL for `_is_read` — it must not match everything."""
        assert not _is_read("servers.naukri.a_key_that_does_not_exist")
        assert _is_read("servers.naukri.staleness.days")

    def test_the_AGENT_branch_of_the_read_check_can_fail_too(self):
        """CONTROL for the authoritative branch, which has its own way to lie.

        It must say YES to a key in FILE_DECIDABLE_KEYS and NO to a key that
        merely appears in the package text under the agent prefix. The second
        half is the regression that prompted the branch:
        `agent.max_daily_applications` is named in `policy.NOT_LOADABLE` and
        the old substring scan called that a read.
        """
        assert _is_read("servers.naukri.agent.mode")
        assert not _is_read("servers.naukri.agent.max_daily_applications")
        assert not _is_read("servers.naukri.agent.invented_key")
        assert re.search(r'["\']agent\.max_daily_applications["\']',
                         PACKAGE_TEXT), (
            "the false positive this branch exists to kill must still be "
            "present in the package text, or this control proves nothing"
        )


class TestTheAgentSubtreeIsNamedNotOpen:
    """The exemption INVERTED by the 2026-08-25 ruling, asserted not assumed.

    Six keys are loadable, by name, and naukri really does read them. The
    subtree around them is not open: everything else under `agent` is still
    tier C with no reader, and that is the half the escalation opened through.
    """

    SIX = [
        "servers.naukri.agent.enabled",
        "servers.naukri.agent.mode",
        "servers.naukri.agent.min_fit_score",
        "servers.naukri.agent.searches",
        "servers.naukri.agent.per_search_limit",
        "servers.naukri.agent.blocklist.enabled",
    ]

    @pytest.mark.parametrize("path", SIX)
    def test_key_is_loadable_declares_naukri_and_is_really_read(self, path):
        spec = SCHEMA[path]
        assert spec.tier != TIER_C, (path, spec.tier)
        assert spec.loadable, path
        assert spec.readers == ("naukri",), (path, spec.readers)
        assert _is_read(path), (
            f"{path} is loadable and declares naukri as its reader; "
            f"agent.FILE_DECIDABLE_KEYS must actually name it, or it is a "
            f"decoy with a tier"
        )

    def test_every_key_in_the_merge_is_loadable_and_declares_naukri(self):
        """Direction 1: naukri's merge cannot name a key the schema strips.

        `load_agent_config` iterates FILE_DECIDABLE_KEYS and reads each one out
        of the loaded policy. A key the loader refuses would simply never
        appear there, and the merge would silently do nothing for it.
        """
        from naukri_server.agent import FILE_DECIDABLE_KEYS

        for tail in FILE_DECIDABLE_KEYS:
            path = AGENT_PREFIX + tail
            assert path in SCHEMA, path
            assert SCHEMA[path].loadable, path
            assert "naukri" in SCHEMA[path].readers, path

    def test_no_agent_key_is_both_merged_and_deferred(self):
        """Direction 2: "read" and "not yet wired" are exclusive claims.

        NOT_YET_WIRED is the honest form of "declared but unread". A key in
        both lists means one of them is lying, and the deferral list is where
        excuses would go to retire.
        """
        from naukri_server.agent import FILE_DECIDABLE_KEYS

        both = [AGENT_PREFIX + t for t in FILE_DECIDABLE_KEYS
                if AGENT_PREFIX + t in NOT_YET_WIRED]
        assert not both, both

    def test_every_declared_agent_key_is_merged_or_deferred_by_name(self):
        """Direction 3: no silent gap under the agent subtree.

        Every agent key that declares naukri as a reader is either in the
        merge or on the deferral list with a reason. This is the same census
        the class above runs, restricted to the subtree the ruling touched, so
        the NEXT agent key added cannot arrive unaccounted for.
        """
        from naukri_server.agent import FILE_DECIDABLE_KEYS

        declared = [p for p in _naukri_keys() if p.startswith(AGENT_PREFIX)]
        assert declared, "census found no agent keys at all"
        unaccounted = [
            p for p in declared
            if p[len(AGENT_PREFIX):] not in FILE_DECIDABLE_KEYS
            and p not in NOT_YET_WIRED
        ]
        assert not unaccounted, unaccounted

    @pytest.mark.parametrize("path", [
        "servers.naukri.agent.newly_invented_switch",
        "servers.naukri.agent.deeply.nested.thing",
        "servers.uplers.agent.enabled",
    ])
    def test_everything_else_under_agent_is_still_tier_c(self, path):
        from jobcore.policy import tier_for

        assert tier_for(path) == TIER_C, path

    def test_naukris_policy_module_still_names_the_daily_quota(self):
        """The six left NOT_LOADABLE; the quota did not.

        It is one of the four Python guards that replaced the tier-C boundary,
        and a guard whose value the same file can raise is worth less than one
        it cannot.
        """
        from naukri_server.agent import FILE_DECIDABLE_KEYS
        from naukri_server.policy import NOT_LOADABLE, SUBTREE_DENY

        assert "agent.max_daily_applications" in NOT_LOADABLE
        assert "max_daily_applications" not in FILE_DECIDABLE_KEYS
        for gone in ("agent.enabled", "agent.mode", "agent.min_fit_score",
                     "agent.searches", "agent.per_search_limit",
                     "agent.blocklist.enabled"):
            assert gone not in NOT_LOADABLE, gone
        assert "deny" in SUBTREE_DENY.lower() or "refused" in SUBTREE_DENY.lower()

    def test_the_policy_module_never_reaches_into_the_agent_block(self):
        """UNCHANGED, and more load-bearing now, not less.

        `setting()` returns a bare value with no floor and no validation. Six
        agent keys are loadable today, so a helper reaching for one THROUGH
        this module would bypass `load_agent_config` -- and with it the Python
        floor, the precedence and `validate_agent_config`. The one legitimate
        reader is `agent.load_agent_config`; this pins that policy.py is not a
        second one.
        """
        src = (PKG / "policy.py").read_text(encoding="utf-8")
        assert 'setting("agent.' not in src
        assert "setting('agent." not in src
