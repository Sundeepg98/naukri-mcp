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
:func:`agent_settings` reads ``agent_config.json`` and Python, and deliberately
does not consult the config file at all — the file may DISPLAY what the agent is
set to (:func:`report` shows it), it may not decide it. jobcore refuses them on
its side too; two independent refusals, because the file is the surface a text
editor reaches and a write-path guard alone is not a guard.

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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from jobcore import config as _jobcore_config
from jobcore.config import Loaded, MIN_AGENT_FIT_FLOOR
from jobcore.policy import (
    DEFAULT_POLICY,
    CandidatePolicy,
    Policy,
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
    "scoring_policy",
    "server_settings",
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
    ``policy_hash``. Without it the granularity is per-scoring-call, and a
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
    """``{policy_rev, policy_hash}`` — what a stored score needs to stay
    interpretable after the policy moves."""
    snap = snapshot()
    return {"policy_rev": snap.policy_rev, "policy_hash": snap.policy_hash}


def report(section: Optional[str] = None) -> dict:
    """The payload ``naukri_config()`` returns: effective values + provenance.

    Includes what the file is NOT allowed to decide, by name, so "why did my
    edit do nothing?" is answerable without reading source.
    """
    snap = snapshot()
    out = snap.report(SERVER_NAME)
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
                    "source": out["source"],
                }
    return out


def apply_patch(patch: dict, *, base_revision: Optional[int] = None,
                actor: str = "naukri", confirm_widen: bool = False) -> dict:
    """Write into the config file, scoped to what this server may write."""
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
    return result
