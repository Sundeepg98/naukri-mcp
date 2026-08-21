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
import os
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from jobcore import config as _jobcore_config
from jobcore.config import Loaded, MIN_AGENT_FIT_FLOOR
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
_REPO_ROOT = _START.parent.parent

#: Components kept by `display_path`'s last-resort tail form. Three is enough to
#: tell two pytest tmp dirs apart, which is the case that exposed the old
#: basename fallback, and short enough that it publishes no useful layout.
_DISPLAY_TAIL_PARTS = 3


def display_path(raw: Optional[str]) -> Optional[str]:
    """A path a human can act on, that is not an absolute local path.

    `utils.scrub_result` runs over every tool result and reduces anything still
    absolute to its BASENAME — deliberately, after a census found this machine's
    layout inside tool output. That guarantee must not be weakened, but it turns
    every entry of `searched` into the identical string "jobhunt.json", which is
    worse than saying nothing.

    So paths are rendered relative to the checkout, or to home as ``~/…``. Both
    forms survive the scrubber (it matches drive-letter absolutes only), both
    stay distinguishable, and neither publishes the machine's layout.

    THIRD FORM, and why it exists (2026-08-21, found by CI, not locally). A path
    under NEITHER anchor used to fall through to the bare basename -- which is
    the very collapse the paragraph above says is worse than saying nothing.
    On this box it never fired, because a Windows temp dir lives under
    ``C:\\Users\\Dell`` and so the home anchor always caught it. On the Linux
    runner ``/tmp`` is under neither ``/home/runner/work/...`` nor
    ``/home/runner``, so every path fell straight to "jobhunt.json" and
    ``test_the_reported_path_is_not_an_absolute_local_path`` failed there and
    only there. That test was right and the function was wrong.

    The last resort is now the TAIL: the final few components with a leading
    ``.../``. It keeps the file distinguishable, publishes no drive letter, no
    home directory and no layout above those components, and it carries no
    leading slash -- so the scrubber leaves it alone exactly as the other two
    forms are left alone.
    """
    if not raw:
        return raw
    p = Path(raw)
    try:
        rel = os.path.relpath(p, _REPO_ROOT)
        # Count PARTS equal to "..", not occurrences of the substring: a
        # directory legitimately named "a..b" is not four levels up.
        if sum(1 for part in rel.split(os.sep) if part == os.pardir) <= 4:
            return rel.replace(os.sep, "/")
    except (ValueError, OSError):
        pass
    try:
        return "~/" + p.relative_to(Path.home()).as_posix()
    except (ValueError, OSError):
        pass
    # Drop the anchor ("C:\\", "/") so no drive letter or root can survive,
    # then keep the tail. One component means there is nothing to elide and
    # the marker would be a lie, so it is omitted in that case.
    parts = p.parts[1:] if p.anchor else p.parts
    if len(parts) <= 1:
        return p.name
    tail = parts[-_DISPLAY_TAIL_PARTS:]
    prefix = ".../" if len(tail) < len(parts) else ""
    return prefix + "/".join(tail)


def report(section: Optional[str] = None) -> dict:
    """The payload ``naukri_config()`` returns: effective values + provenance.

    Includes what the file is NOT allowed to decide, by name, so "why did my
    edit do nothing?" is answerable without reading source.
    """
    snap = snapshot()
    out = snap.report(SERVER_NAME)
    out["unknown_keys"] = [k for k in out.get("unknown_keys", ())
                           if k not in ENVELOPE_KEYS]
    out["source"] = display_path(out.get("source"))
    out["searched"] = [display_path(s) for s in out.get("searched", ())]
    if out.get("source") is None:
        out["config_status"] = (
            "no file found; built-in defaults in use. searched: "
            + (", ".join(out["searched"]) if out["searched"] else "(nothing)")
        )
    else:
        out["config_status"] = f"loaded from {out['source']}"
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
