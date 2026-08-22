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
supplies the numbers behind a score, the shared ``candidate`` block, and
naukri's display-side settings. It supplies **nothing that decides whether an
application is submitted**.

    THE INVARIANT: no sequence of config writes, from any server, may grant
    autonomous apply authority.

The traced escalation was five writes long — ``agent.enabled: true`` ->
``agent.mode: "auto"`` -> ``min_fit_score: 0`` -> ``blocklist.enabled: false``
-> arbitrary ``searches[]`` — and ended at fifteen real applications a day with
no human approval. A sixth path ran through a sibling server entirely: writing
every canonical skill into ``candidate.skills`` drives ``|matched|/|job_skills|``
to 100 for every job in existence, which drives this server's agent.

So those keys are **tier C: not loadable**, and this module never asks for them.
``naukri_server.agent.load_agent_config`` reads ``agent_config.json`` and Python
and deliberately does not consult the config file at all — the file may DISPLAY
what the agent is set to (:func:`report` shows it), it may not decide it.
jobcore refuses them on its side too; two independent refusals, because the file
is the surface a text editor reaches and a write-path guard alone is not a
guard.

The two levers that CANNOT be tier C — ``candidate.skills`` and ``scoring``,
which are the feature he asked for by name — are bounded instead: jobcore caps
them, :data:`MIN_AGENT_FIT_FLOOR` floors the selector in Python whatever the
file says, and :func:`requires_approval_cycle` forces one approval cycle after
any scoring-fingerprint change, so "policy was quietly widened" becomes "he sees
the list".

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

#: The document's own envelope. jobcore's `default_document()` writes all four,
#: and its unknown-key census then reports them as "keys nothing reads" — so
#: EVERY legitimate file, including the one jobcore generates itself, carries a
#: spurious decoy warning. Filtered here rather than surfaced, because a warning
#: that fires on correct input trains people to ignore warnings. The real fix is
#: one line upstream in jobcore; reported, not reached into from this repo.
ENVELOPE_KEYS = frozenset({"config_version", "revision", "updated_at", "updated_by"})

#: Keys under this server's section that the FILE does not decide, whatever it
#: contains. Listed here as well as in jobcore's schema so a reader of this
#: package can see the boundary without leaving it.
NOT_LOADABLE = (
    "agent.enabled",
    "agent.mode",
    "agent.min_fit_score",
    "agent.searches",
    "agent.per_search_limit",
    "agent.blocklist.enabled",
    # …and anything else under `agent` the schema does not explicitly name:
    # omission is how the escalation opened, so the subtree denies by default.
)

__all__ = [
    "MIN_AGENT_FIT_FLOOR",
    "NOT_LOADABLE",
    "SERVER_NAME",
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

    Deliberately NOT usable for anything the agent's apply decision depends on
    — those keys are not loadable at all (:data:`NOT_LOADABLE`) and this
    function reads only what the loader produced, which never contains them.
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
    out = snap.report(SERVER_NAME, display=display_path)
    out["unknown_keys"] = [k for k in out.get("unknown_keys", ())
                           if k not in ENVELOPE_KEYS]
    out["server_name"] = SERVER_NAME
    out["writable_sections"] = list(WRITABLE_SECTIONS)
    out["not_loadable_here"] = [f"servers.{SERVER_NAME}.{k}" for k in NOT_LOADABLE]
    out["min_agent_fit_floor"] = MIN_AGENT_FIT_FLOOR
    out["agent_authority"] = (
        "The autonomous apply loop is configured by agent_config.json and "
        "naukri_agent_update_config, never by this file. A tier-C key present "
        "in the file is refused and listed in tier_c_refusals; the Python "
        "value is used."
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
