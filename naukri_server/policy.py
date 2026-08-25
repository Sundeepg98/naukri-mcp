"""naukri's binding to the central config file — read here, injected everywhere.

This module is the ONLY place in the package that touches ``jobcore.config``.
Everything else takes a policy object as an argument, exactly as
``domain/salary.py`` takes ``SalaryConfig``. That is not aesthetics: jobcore's
``test_independence.py`` runs a clean interpreter with cwd elsewhere and asserts
a score of exactly ``100``, and a scoring path that discovered a file would make
that number machine-dependent.

WHAT IT READS, AND WHAT IT REFUSES TO READ
------------------------------------------

The file (``config/jobhunt.json``, discovered by walking up from THIS file)
supplies the numbers behind a score, the shared ``candidate`` block, naukri's
display-side settings, and -- since 2026-08-25 -- six keys that ARM AND AIM THE
AUTONOMOUS APPLY LOOP.

    THIS FILE MAY ARM THE AGENT. It could not until 2026-08-25, and a great
    deal of prose in this repo still says it cannot. That prose is stale
    wherever it survives; this docstring is the current statement.

    What the file may decide: ``agent.enabled``, ``agent.mode``,
    ``agent.min_fit_score``, ``agent.searches``, ``agent.per_search_limit``,
    ``agent.blocklist.enabled``. What it may NOT: anything else under
    ``agent`` -- the subtree still denies by default -- and specifically
    ``agent.max_daily_applications``, which this server does not take from
    the shared file at any tier.

The traced escalation that once justified refusing all six was five writes long
-- ``agent.enabled: true`` -> ``agent.mode: "auto"`` -> ``min_fit_score: 0`` ->
``blocklist.enabled: false`` -> arbitrary ``searches[]`` -- and ended at fifteen
real applications a day with no human approval. That trace was correct. The
operator overruled the conclusion, not the trace, and NOT the protections. The
escalation now LANDS at the config layer and is NEUTRALISED in Python:

1. :data:`MIN_AGENT_FIT_FLOOR` (=60) floors the selector in
   ``agent._decide`` from ANY source, and again per search, so
   ``min_fit_score: 0`` selects nothing under 60.
2. The kill switch is re-checked inside the auto-apply loop itself.
3. The daily quota caps candidates before ``_act`` ever runs, and
   ``validate_agent_config`` bounds ``max_daily_applications`` to 1-100.
4. Everything the file offers is re-validated through
   ``validate_agent_config`` on load; an overlay that would break the agent is
   dropped whole rather than half-applied.

A sixth path ran through a sibling server entirely and never touched the agent
block: writing every canonical skill into ``candidate.skills`` drives
``|matched|/|job_skills|`` to 100 for every job in existence, which drives this
server's agent. ``candidate.skills`` and ``scoring`` CANNOT be locked away --
they are the feature he asked for by name -- so they are bounded instead:
jobcore caps them, and :func:`requires_approval_cycle` forces one approval
cycle after any change to the {scoring, candidate} fingerprint, so "policy was
quietly widened" becomes "he sees the list".

Note precisely what that last guard does and does not cover: the fingerprint is
``policy_hash``, over ``scoring`` and ``candidate``. It does NOT include
``servers.*``, so writing ``agent.mode: "auto"`` here does not itself force an
approval cycle. Guards 1-4 above are what bound a file-armed agent.

``naukri_server.agent.load_agent_config`` is where the six are merged, and it
documents its own precedence: the shared file wins over ``agent_config.json``
for those six, and for nothing else.

ONE CALL, ONE POLICY
--------------------

:func:`bind` sets a :class:`contextvars.ContextVar` at tool entry and every
``snapshot()`` inside that call returns the same object. A change landing
mid-call is not seen by that call: half a ranking scored with old weights and
half with new is worse than either. Contextvars are the right primitive because
naukri is ONE process running HTTP + stdio + nine scheduled tasks on ONE event
loop, and every ``await`` inside a ranking loop is a yield point.

Unbound, ``snapshot()`` reads through jobcore's content-addressed cache: the
bytes are read and hashed on every call and re-parsed only on change. Never
``mtime`` — twelve back-to-back atomic replaces on this volume produced eight
distinct ``(mtime_ns, size)`` pairs, four of them with a delta of exactly zero.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from jobcore import config as _jobcore_config
from jobcore.config import Loaded, MIN_AGENT_FIT_FLOOR
from jobcore.paths import display_path as _jobcore_display_path
from jobcore.paths import relativise_known as _jobcore_relativise_known
from jobcore.policy import (
    DEFAULT_POLICY,
    CandidatePolicy,
    ScoringPolicy,
    requires_approval_cycle,
)

logger = logging.getLogger("naukri.policy")

SERVER_NAME = "naukri"

#: Sections `naukri_set_config` may write. `servers.uplers` and friends are
#: refused by name — no server widens a sibling's section.
WRITABLE_SECTIONS = ("candidate", "scoring", f"servers.{SERVER_NAME}")

#: Keys under this server's section that the FILE does not decide, whatever it
#: contains. Listed here as well as in jobcore's schema so a reader of this
#: package can see the boundary without leaving it.
#:
#: The six agent keys that used to head this tuple were REMOVED on 2026-08-25:
#: `agent.enabled`, `agent.mode`, `agent.min_fit_score`, `agent.searches`,
#: `agent.per_search_limit` and `agent.blocklist.enabled` are now tier B and
#: loadable. See this module's docstring for what bounds them instead, and
#: `agent.FILE_DECIDABLE_KEYS` for the merge.
#:
#: What remains is the deny-by-default subtree, which did NOT change. Anything
#: under `agent` that jobcore's schema does not explicitly name is still tier C
#: and still refused -- omission is how the escalation opened. The one key named
#: below is called out because it is the daily quota, which is one of the four
#: Python protections: a guard whose value the same file can raise is worth
#: less than one it cannot.
NOT_LOADABLE = (
    "agent.max_daily_applications",
)

#: The subtree rule, as prose for the readout. Not a key name, so deliberately
#: not in :data:`NOT_LOADABLE` -- that tuple holds paths a caller can match.
SUBTREE_DENY = (
    "Anything under servers.naukri.agent that jobcore's schema does not "
    "explicitly name is tier C and refused on both load and write."
)

__all__ = [
    "MIN_AGENT_FIT_FLOOR",
    "NOT_LOADABLE",
    "SERVER_NAME",
    "SUBTREE_DENY",
    "WRITABLE_SECTIONS",
    "bind",
    "invalidate",
    "requires_approval_cycle",
    "display_min_score",
    "scoring_policy",
    "server_settings",
    "setting",
    "snapshot",
]

_BOUND: contextvars.ContextVar[Optional[Loaded]] = contextvars.ContextVar(
    "naukri_policy_snapshot", default=None
)

# Walk up from THIS file, not from jobcore's. H5: jobcore cannot know who
# imported it, and a non-editable `pip install jobcore` puts its __file__ in
# site-packages where the walk finds nothing — silently, which is the worst
# possible failure for a config system.
_START = Path(__file__).resolve()


def snapshot() -> Loaded:
    """The effective policy for this call. Never raises.

    A malformed or out-of-range file yields built-in defaults plus a prominent
    ``config_error``; half-applying a policy is worse than ignoring one, and
    dying at import is worse than both.
    """
    bound = _BOUND.get()
    if bound is not None:
        return bound
    try:
        return _jobcore_config.current(_START)
    except Exception as exc:  # pragma: no cover - jobcore already swallows
        logger.warning("config load failed, using built-in defaults: %s", exc)
        return _defaults()


def _defaults() -> Loaded:
    return Loaded(
        policy=DEFAULT_POLICY,
        source=None,
        revision=0,
        policy_rev=0,
        policy_hash=DEFAULT_POLICY.policy_hash,
        content_hash=None,
        config_error="loader raised; built-in defaults in use",
    )


@contextmanager
def bind(loaded: Optional[Loaded] = None):
    """Pin one policy snapshot for the duration of a tool call.

    Wrap a tool body in this and every score it produces carries one
    ``scoring_hash``. Without it the granularity is per-scoring-call, and a
    concurrent write can swap the policy between two rows of one ranking.
    """
    token = _BOUND.set(loaded if loaded is not None else snapshot())
    try:
        yield _BOUND.get()
    finally:
        _BOUND.reset(token)


def invalidate() -> None:
    """Drop every cached snapshot. Tests and an explicit reload use this."""
    _jobcore_config.invalidate_cache()


# ── Convenience accessors ──────────────────────────────────────────────────

def scoring_policy() -> ScoringPolicy:
    return snapshot().policy.scoring


def candidate() -> CandidatePolicy:
    return snapshot().policy.candidate


def server_settings() -> dict:
    """naukri's own section of the file, defaults filled in.

    Display-side only. Anything the agent's apply decision depends on is read
    from :func:`naukri_server.agent.load_agent_config` and from Python.
    """
    return snapshot().policy.server(SERVER_NAME)


def setting(path: str, default):
    """One naukri setting by dotted path, e.g. ``"daily_brief.sections.inbox"``.

    The DEFAULT is passed by the caller and is today's literal, so a missing
    file, a malformed file or a key nobody wrote all yield exactly today's
    behaviour. The type of the default is enforced: a string where a number
    belongs falls back rather than propagating into arithmetic.

    NOT the way to read the agent's apply settings, even though six of them
    ARE loadable now. This function returns a bare value with no floor, no
    clamp and no validation; the agent's settings must come from
    :func:`naukri_server.agent.load_agent_config`, which applies the
    precedence, routes the file's values through ``validate_agent_config``,
    and drops an overlay that would break the agent. Asking this function for
    the agent's selector threshold would hand a caller the raw file value and
    bypass the Python floor entirely, so nothing in this package does.
    """
    node = server_settings()
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    if node is None:
        return default
    if isinstance(default, bool):
        return node if isinstance(node, bool) else default
    if isinstance(default, int) and not isinstance(node, bool):
        try:
            return int(node)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(node)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        return node if isinstance(node, str) else default
    return node


def display_min_score() -> int:
    """The DISPLAY filter. Deliberately not named ``min_fit_score``: that leaf
    name is the agent's apply selector and is tier C wherever it appears."""
    from naukri_server.config import DISPLAY_MIN_FIT_SCORE

    value = server_settings().get("display_min_score")
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DISPLAY_MIN_FIT_SCORE
    return value if 0 <= value <= 100 else DISPLAY_MIN_FIT_SCORE


def policy_stamp() -> dict:
    """``{policy_rev, policy_hash, scoring_hash}`` -- both fingerprints, named.

    They are NOT interchangeable, which is why both are here:

    - ``scoring_hash`` covers ``{scoring}`` only. This is what a STORED SCORE
      needs: two scores are comparable exactly when it matches, because it
      fingerprints the arithmetic and nothing else.
    - ``policy_hash`` covers ``{scoring, candidate}``. This is what a CONFIG
      READOUT needs, and what the agent's approval gate compares -- inflating
      ``candidate.skills`` moves every score without touching the arithmetic,
      so only the full hash sees it.
    """
    snap = snapshot()
    return {"policy_rev": snap.policy_rev, "policy_hash": snap.policy_hash,
            "scoring_hash": snap.scoring_hash}


#: The naukri checkout root — the anchor config paths are displayed against.
#: Read at CALL time by `display_path`, not captured at import: the
#: `TestDisplayPathUnderNeitherAnchor` fixture monkeypatches this name to force
#: both anchors to miss, and a captured value would make that fixture inert.
_REPO_ROOT = _START.parent.parent


def display_path(raw: Optional[str]) -> Optional[str]:
    """A path a human can act on, that is not an absolute local path.

    CANONICAL IMPLEMENTATION: :func:`jobcore.paths.display_path`. This is a
    delegation, not a copy -- the same rendering now serves naukri, uplers and
    every other jobcore consumer, so a fix lands once. All this wrapper supplies
    is the ANCHOR, which jobcore cannot know: a path rendered relative to the
    *library* would be meaningless to a reader of naukri's output.

    WHY IT EXISTS AT ALL (the naukri-specific rationale, kept because it is the
    reason the field is relativised rather than deleted). `utils.scrub_result`
    runs over every tool result and reduces anything still absolute to its
    BASENAME -- deliberately, after a census found this machine's layout inside
    tool output. That guarantee must not be weakened, but on its own it turns
    every entry of `searched` into the identical string "jobhunt.json", which is
    worse than saying nothing. So paths are rendered relative to the checkout,
    or to home as ``~/...``, or as a ``.../a/b/c`` tail. All three survive the
    scrubber (it matches drive-letter absolutes only), all three stay
    distinguishable, and none publishes the machine's layout.

    The tail form is the one CI found missing (2026-08-21, red on the Linux
    runner and green on every Windows box, because a Windows temp dir lives
    under ``C:\\Users\\Dell`` and so the home anchor always caught it). jobcore
    carries that fix and its ``DISPLAY_TAIL_PARTS`` / ``MAX_PARENT_HOPS``
    constants; see ``jobcore/paths.py`` for the full account.
    """
    return _jobcore_display_path(raw, anchor=_REPO_ROOT)


def relativise_known_paths(text, loaded: Loaded):
    """Render any path jobcore already baked into a composed message.

    A BINDING of :func:`jobcore.paths.relativise_known`, not a second copy of
    it: this supplies the two arguments naukri knows -- the snapshot's own path
    list, and this module's `display_path` -- and the upstream owns the
    algorithm, including the fact that a Windows path has TWO spellings
    (`OSError.__str__` renders its filename through `repr()`, which doubles
    every separator).

    `Loaded.known_paths` rather than a hand-rolled `[source]`, and the
    difference is not cosmetic: it includes the PARENT DIRECTORY of the config
    and of every searched path, which is the only way to reach the two files
    jobcore names FROM that directory -- the history ledger (`could not append
    to {ledger}`) and the write lock (`config file locked by live PID ...
    (lock: {lock_file})`). Neither equals `source`, both reach a caller through
    `apply_patch`, and uplers measured the lock message publishing the full
    temp path with a source-only list and clean with this one.

    Substitution stays EXACT rather than heuristic -- only strings the snapshot
    already knows are paths -- which is why a Naukri API route or a
    `https://www.naukri.com/...` job URL in the same sentence is left alone.
    """
    return _jobcore_relativise_known(
        text, known=loaded.known_paths, render=display_path
    )


def _relativise_node(node, loaded: Loaded):
    """One node of :func:`relativise_mapping`'s walk.

    Split out so `relativise_mapping` can keep its "a mapping, or nothing"
    contract at the top while the walk below it is uniform. Container TYPES are
    rebuilt as themselves because callers compare this dict against the one
    they passed in, and jobcore's payload is JSON-shaped -- dict, list, str,
    number, None -- so the recursion is finite: a JSON document cannot express
    a cycle.
    """
    if isinstance(node, dict):
        return {key: _relativise_node(value, loaded)
                for key, value in node.items()}
    if isinstance(node, list):
        return [_relativise_node(item, loaded) for item in node]
    if isinstance(node, tuple):
        return tuple(_relativise_node(item, loaded) for item in node)
    return relativise_known_paths(node, loaded)


def relativise_mapping(payload, loaded: Loaded):
    """:func:`relativise_known_paths` over EVERY string in a payload, at any depth.

    For jobcore's `apply_patch` return, which this server hands back verbatim
    and which carries a path in FOUR places: `path` on SUCCESS, `ledger_error`,
    `detail` on a lock conflict, and `searched` on `no_config_file` -- which is
    a LIST. Passing that dict through untouched is how a leak survived a sweep
    that had already cleaned every path FIELD on the report beside it -- error
    paths are where everyone looks, and the first of these is on the success
    payload.

    THE WALK IS FULL RECURSION -- dicts, lists and tuples at any depth, every
    string leaf rendered -- and NOT the "non-strings pass through" rule this
    used to carry. That rule was MEASURED leaking on 2026-08-22: `detail` and
    `searched` are composed from the SAME path on the `no_config_file` branch,
    the string was rendered and the list beside it was not, so one payload had
    one field right and its neighbour wrong. Full depth rather than one level
    because `changed` is `{key: [old, new]}` over arbitrary config values and a
    path can therefore sit two containers down; a depth limit would be an
    arbitrary line, and this leak is what crossing one looks like.

    DEPTH IS FREE BECAUSE THE SUBSTITUTION IS EXACT. `relativise_known`
    replaces only strings the snapshot ALREADY KNOWS are paths, never
    path-shaped text, so walking into a list cannot begin eating a Naukri API
    route or a `https://www.naukri.com/...` job URL: it can only replace a
    string equal to a known path, which is the correct rendering wherever that
    string appears. Dict KEYS are deliberately left alone -- a key here is a
    config key name, never a path, and rendering keys could only ever collide
    two of them into one.

    Deliberately identical to `uplers_server.policy.relativise_mapping`: the
    two servers hand back the same jobcore dict, and two renderers that
    disagree is the drift `report()` already had to be rescued from.
    """
    if not isinstance(payload, dict):
        return payload
    return _relativise_node(payload, loaded)


def report(section: Optional[str] = None) -> dict:
    """The payload ``naukri_config()`` returns: effective values + provenance.

    Includes what the file is NOT allowed to decide, by name, so "why did my
    edit do nothing?" is answerable without reading source.
    """
    snap = snapshot()
    # ONE PLACE RENDERS A PATH, and it is jobcore's, driven by our anchor.
    #
    # This used to call `snap.report(SERVER_NAME)` and then post-process
    # `source` and `searched` by hand. Two defects came out of that on
    # 2026-08-22:
    #
    # 1. THE PROSE WAS NEVER RENDERED. jobcore composes its failure messages as
    #    f-strings with the path already baked in -- `f"{path} is not valid
    #    JSON: {exc}"` -- and stores them in `config_error`. Post-processing the
    #    path FIELDS left that string absolute. `utils.scrub_result` caught it
    #    at the tool boundary, but only by collapsing the path to its bare
    #    basename, which is the exact degradation `display_path` exists to
    #    prevent; and any caller of this function that is not a tool got the
    #    raw absolute path.
    # 2. `config_status` WAS OVERWRITTEN WITH A LIE. The hand-rolled version was
    #    composed from `source` alone and discarded `config_error` entirely, so
    #    an UNPARSEABLE file reported "loaded from <file>". It had not loaded.
    #    jobcore's own property says "error: ..." and is simply correct.
    #
    # Passing `display` down fixes both, and deleting the post-processing is
    # the load-bearing half: two renderers would drift apart, and the second
    # one is what discarded the error.
    # The envelope filter that used to sit here is GONE, not lost. jobcore's
    # `default_document()` stamps four envelope keys (config_version, revision,
    # updated_at, updated_by) and its unknown-key census used to report them as
    # "keys nothing reads", so every correctly generated file carried a
    # spurious warning and this module filtered them back out. jobcore fixed it
    # upstream in 97cfa79 with a read-side exemption in its own ENVELOPE_KEYS,
    # which is the one place it belongs; a second filter here would now be a
    # no-op that reads like a guard. Pinned by test_config_tool.py.
    out = snap.report(SERVER_NAME, display=display_path)
    out["server_name"] = SERVER_NAME
    out["writable_sections"] = list(WRITABLE_SECTIONS)
    out["not_loadable_here"] = [f"servers.{SERVER_NAME}.{k}" for k in NOT_LOADABLE]
    out["subtree_deny"] = SUBTREE_DENY
    out["min_agent_fit_floor"] = MIN_AGENT_FIT_FLOOR
    out["agent_authority"] = (
        "This file CAN arm the autonomous apply loop: agent.enabled, "
        "agent.mode, agent.min_fit_score, agent.searches, "
        "agent.per_search_limit and agent.blocklist.enabled are tier B and "
        "are merged by naukri_server.agent.load_agent_config, where the file "
        "wins over agent_config.json. What it cannot do is get past the four "
        f"Python guards: the selector floor of {MIN_AGENT_FIT_FLOOR} (applied "
        "per cycle AND per search), the kill switch checked inside the "
        "auto-apply loop, the daily quota that caps candidates before any "
        "apply runs, and validate_agent_config, which re-checks every "
        "file-sourced value and drops the whole overlay rather than "
        "half-apply it. agent.max_daily_applications is not taken from this "
        "file at all, and anything under agent the schema does not name is "
        "still tier C, refused, and listed in tier_c_refusals."
    )
    if section:
        return {"status": "success", "section": section,
                section: out.get(section, out.get("server"))} | {
                    "policy_rev": out["policy_rev"],
                    "policy_hash": out["policy_hash"],
                    # The narrowed readout carries the same bridge field as the
                    # full one, so a stored score's scoring_hash can be matched
                    # against a section read without a second call.
                    "scoring_hash": out["scoring_hash"],
                    "source": out["source"],
                }
    return out


def apply_patch(patch: dict, *, base_revision: Optional[int] = None,
                actor: str = "naukri", confirm_widen: bool = False) -> dict:
    """Write into the config file, scoped to what this server may write.

    EVERY PATH IN THE RETURN IS RENDERED, including the one on the SUCCESS
    payload. jobcore's dict carries `str(target)` as `path` when the write
    lands, plus `ledger_error` and `detail` on the failure branches; handing it
    back verbatim published this machine's absolute layout out of a WRITE tool.
    Measured at this layer on 2026-08-22, all three branches red.

    RELATIVISED, NOT DELETED, and that is the load-bearing half. `path` answers
    the one question a write tool's response has to answer -- WHICH file did
    you just change -- and `utils.scrub_result` "saved" it downstream only by
    collapsing it to the bare basename `jobhunt.json`, under which two
    candidate config files read identically. A `None` there would trade a leak
    for a field that answers a different question than it looks like.
    """
    result = _jobcore_config.apply_patch(
        patch,
        start=_START,
        base_revision=base_revision,
        actor=actor,
        allowed_sections=WRITABLE_SECTIONS,
        confirm_widen=confirm_widen,
    )
    if result.get("status") == "ok":
        invalidate()
    # AFTER the invalidate, so the snapshot whose `known_paths` drive the
    # substitution is the post-write one -- the same file, re-read, rather than
    # a cached pre-write object that a concurrent relocation could have staled.
    return relativise_mapping(result, snapshot())
