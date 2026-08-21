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


def _is_read(path: str) -> bool:
    """Does the package name this key, relative to its own section?"""
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


class TestTheAgentSubtreeStaysUnread:
    """The exemption, asserted rather than assumed.

    These keys are tier C. naukri must NOT grow a reader for them: reading them
    from the file is precisely the escalation the invariant test runs.
    """

    @pytest.mark.parametrize("path", [
        "servers.naukri.agent.enabled",
        "servers.naukri.agent.mode",
        "servers.naukri.agent.min_fit_score",
        "servers.naukri.agent.searches",
        "servers.naukri.agent.per_search_limit",
        "servers.naukri.agent.blocklist.enabled",
    ])
    def test_key_is_tier_c_and_declares_no_reader(self, path):
        spec = SCHEMA[path]
        assert spec.tier == TIER_C, (path, spec.tier)
        assert spec.readers == (), (path, spec.readers)
        assert not spec.loadable

    def test_naukris_policy_module_lists_them_as_not_loadable(self):
        from naukri_server.policy import NOT_LOADABLE

        for tail in ("agent.enabled", "agent.mode", "agent.min_fit_score",
                     "agent.searches", "agent.per_search_limit",
                     "agent.blocklist.enabled"):
            assert tail in NOT_LOADABLE, tail

    def test_the_policy_module_never_reaches_into_the_agent_block(self):
        """`setting()` reads the loader's output, which by construction never
        contains a tier-C value from the file — but a future helper could reach
        past it. This pins that nothing in the package asks the CONFIG for the
        agent's mode."""
        src = (PKG / "policy.py").read_text(encoding="utf-8")
        assert 'setting("agent.' not in src
        assert "setting('agent." not in src
