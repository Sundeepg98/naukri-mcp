"""Autonomous job-hunting agent — observe→decide→act→learn loop.

Runs as a scheduled task (via Option B scheduler). Searches for jobs,
filters by fit score and blocklist, and applies based on configured mode:
  - dry_run: log decisions only, never apply
  - approval: queue jobs for human review via notifications
  - auto: apply automatically within daily budget

All building blocks (search, scoring, apply, events, DB) already exist.
This file is ~250 lines of orchestration glue.
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from mcp.server.fastmcp import Context

from naukri_server.config import DATA_DIR
from naukri_server.domain.agent import (
    AgentBlocklist, QuietHours, AgentCandidate, validate_agent_config,
)

logger = logging.getLogger(__name__)

# IST offset (UTC+5:30) — matches scheduler.py
IST = timezone(timedelta(hours=5, minutes=30))

# Default config — used when agent_config.json doesn't exist
DEFAULT_CONFIG = {
    "enabled": False,  # Must be explicitly enabled
    "mode": "approval",  # dry_run | approval | auto
    "max_daily_applications": 15,
    "min_fit_score": 70,
    "quiet_hours": {
        "enabled": True,
        "start_hour": 20,  # 8 PM IST
        "end_hour": 8,     # 8 AM IST
    },
    "searches": [
        {
            "name": "Node.js Bangalore",
            "keywords": "Node.js developer",
            "location": "Bangalore",
            "freshness": 7,
            "min_fit_score": 70,
            "enabled": True,
        },
    ],
    "blocklist": {
        "companies": [],
        "title_keywords": [],
        "enabled": True,
    },
    "reminder_days": 7,
}

CONFIG_PATH = DATA_DIR / "agent_config.json"


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------

def load_agent_config() -> dict:
    """Load agent config from file, falling back to defaults.

    Sync — for use from sync contexts. Async callers should use
    `await asyncio.to_thread(load_agent_config)` to avoid blocking the event loop.
    Config file is small (~500 bytes), so blocking is microseconds, but consistent
    use of to_thread() keeps the pattern correct.
    """
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
            # Merge with defaults for any missing keys
            merged = {**DEFAULT_CONFIG, **config}
            return merged
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load agent config: %s, using defaults", e,
                           extra={"step": "config"})
    return DEFAULT_CONFIG.copy()


def save_agent_config(config: dict):
    """Save agent config to file.

    Sync — async callers should use `await asyncio.to_thread(save_agent_config, config)`.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def config_hash(config: dict) -> str:
    """Short hash of config for change detection."""
    s = json.dumps(config, sort_keys=True)
    return hashlib.sha256(s.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Saga steps: observe → decide → act → learn
# ---------------------------------------------------------------------------

async def _observe(cycle_id: str, config: dict) -> dict:
    """Step 1: Gather context — applied IDs, daily count, config."""
    from naukri_server.database import get_applied_job_ids, count_daily_applied

    applied_ids = await get_applied_job_ids()
    today = datetime.now(IST).strftime("%Y-%m-%d")
    daily_applied = await count_daily_applied(today)
    daily_remaining = max(0, config["max_daily_applications"] - daily_applied)

    logger.info("Agent %s: observe — %d applied IDs, %d daily applied, %d remaining",
                cycle_id, len(applied_ids), daily_applied, daily_remaining,
                extra={"cycle_id": cycle_id, "step": "observe"})

    try:
        from naukri_server.events import event_bus, AgentObserveCompleted
        await event_bus.emit(AgentObserveCompleted(
            cycle_id=cycle_id, applied_ids_count=len(applied_ids),
            daily_remaining=daily_remaining,
        ))
    except Exception as e:
        logger.warning("Failed to emit AgentObserveCompleted: %s", e,
                       extra={"cycle_id": cycle_id, "step": "observe"})

    return {
        "cycle_id": cycle_id,
        "config": config,
        "applied_ids": applied_ids,
        "daily_applied": daily_applied,
        "daily_remaining": daily_remaining,
    }


async def _decide(observe_result: dict) -> dict:
    """Step 2: Search jobs, filter by score/blocklist/dedup, cap to budget."""
    from naukri_server.tools.auto_hunt import naukri_auto_hunt
    from naukri_server.database import insert_agent_decision
    from naukri_server.events import event_bus, AgentJobSkipped

    config = observe_result["config"]
    applied_ids = observe_result["applied_ids"]
    cycle_id = observe_result["cycle_id"]
    daily_remaining = observe_result["daily_remaining"]
    min_fit = config.get("min_fit_score", 70)
    blocklist_obj = AgentBlocklist.from_config(config)

    candidates: list[AgentCandidate] = []
    total_found = 0
    total_matched = 0
    searches_run = 0

    for search in config.get("searches", []):
        if not search.get("enabled", True):
            continue
        searches_run += 1

        result = await naukri_auto_hunt(
            keywords=search["keywords"],
            location=search.get("location"),
            min_fit_score=search.get("min_fit_score", min_fit),
            limit=20,
            freshness=search.get("freshness", 7),
            work_mode=search.get("work_mode"),
        )

        if result.get("status") != "success":
            logger.warning("Auto hunt failed for '%s': %s", search["name"], result.get("message"),
                           extra={"cycle_id": cycle_id, "step": "decide", "search": search["name"]})
            continue

        total_found += result.get("jobs_found", 0)
        total_matched += result.get("jobs_matched", 0)
        logger.info("Agent %s: search '%s' — found %d, matched %d",
                    cycle_id, search["name"], result.get("jobs_found", 0), result.get("jobs_matched", 0),
                    extra={"cycle_id": cycle_id, "step": "decide", "search": search["name"]})

        for job in result.get("ranked_jobs", []):
            job_id = str(job.get("job_id", ""))

            # Dedup
            if job_id in applied_ids or any(c.job_id == job_id for c in candidates):
                await insert_agent_decision(
                    cycle_id, job_id, "skip",
                    company=job.get("company", ""), title=job.get("title", ""),
                    search_query=search["name"], fit_score=job.get("fit_score", 0),
                    skip_reason="already_applied",
                )
                logger.info("Agent %s: skip %s at %s — %s (fit: %d)",
                            cycle_id, job_id, job.get("company", ""), "already_applied", job.get("fit_score", 0),
                            extra={"cycle_id": cycle_id, "step": "decide", "search": search["name"]})
                try:
                    await event_bus.emit(AgentJobSkipped(
                        cycle_id=cycle_id, job_id=job_id,
                        company=job.get("company", ""), reason="already_applied",
                        fit_score=job.get("fit_score", 0),
                    ))
                except Exception as e:
                    logger.warning("Failed to emit AgentJobSkipped: %s", e,
                                   extra={"cycle_id": cycle_id, "step": "decide"})
                continue

            # Blocklist
            block_reason = blocklist_obj.check(job.get("company", ""), job.get("title", ""))
            if block_reason:
                await insert_agent_decision(
                    cycle_id, job_id, "skip",
                    company=job.get("company", ""), title=job.get("title", ""),
                    search_query=search["name"], fit_score=job.get("fit_score", 0),
                    skip_reason=block_reason,
                )
                logger.info("Agent %s: skip %s at %s — %s (fit: %d)",
                            cycle_id, job_id, job.get("company", ""), block_reason, job.get("fit_score", 0),
                            extra={"cycle_id": cycle_id, "step": "decide", "search": search["name"]})
                try:
                    await event_bus.emit(AgentJobSkipped(
                        cycle_id=cycle_id, job_id=job_id,
                        company=job.get("company", ""), reason=block_reason,
                        fit_score=job.get("fit_score", 0),
                    ))
                except Exception as e:
                    logger.warning("Failed to emit %s: %s", "AgentJobSkipped", e,
                                   extra={"cycle_id": cycle_id, "step": "decide"})
                continue

            # Daily cap
            if len(candidates) >= daily_remaining:
                await insert_agent_decision(
                    cycle_id, job_id, "skip",
                    company=job.get("company", ""), title=job.get("title", ""),
                    search_query=search["name"], fit_score=job.get("fit_score", 0),
                    skip_reason="daily_cap",
                )
                logger.info("Agent %s: skip %s at %s — %s (fit: %d)",
                            cycle_id, job_id, job.get("company", ""), "daily_cap", job.get("fit_score", 0),
                            extra={"cycle_id": cycle_id, "step": "decide", "search": search["name"]})
                try:
                    await event_bus.emit(AgentJobSkipped(
                        cycle_id=cycle_id, job_id=job_id,
                        company=job.get("company", ""), reason="daily_cap",
                        fit_score=job.get("fit_score", 0),
                    ))
                except Exception as e:
                    logger.warning("Failed to emit AgentJobSkipped: %s", e,
                                   extra={"cycle_id": cycle_id, "step": "decide"})
                continue

            # Accept candidate
            candidate = AgentCandidate.from_hunt_result(job, search["name"])
            logger.info("Agent %s: candidate %s at %s (fit: %d)",
                        cycle_id, candidate.job_id, candidate.company, candidate.fit_score,
                        extra={"cycle_id": cycle_id, "step": "decide", "search": search["name"]})
            candidates.append(candidate)
            await insert_agent_decision(
                cycle_id, candidate.job_id, "apply",
                company=candidate.company, title=candidate.title,
                search_query=candidate.search_name, fit_score=candidate.fit_score,
                apply_status="pending",
            )

    candidates.sort(key=lambda c: c.fit_score, reverse=True)
    logger.info("Agent %s: decide — %d candidates from %d searches, %d total found, %d matched",
                cycle_id, len(candidates), searches_run, total_found, total_matched,
                extra={"cycle_id": cycle_id, "step": "decide"})

    try:
        from naukri_server.events import AgentDecideCompleted
        await event_bus.emit(AgentDecideCompleted(
            cycle_id=cycle_id, searches_run=searches_run,
            total_found=total_found, total_matched=total_matched,
            candidates_count=len(candidates),
        ))
    except Exception as e:
        logger.warning("Failed to emit AgentDecideCompleted: %s", e,
                       extra={"cycle_id": cycle_id, "step": "decide"})

    return {
        **observe_result,
        "candidates": candidates,
        "searches_run": searches_run,
        "total_found": total_found,
        "total_matched": total_matched,
    }


async def _act(decide_result: dict) -> dict:
    """Step 3: Apply based on mode — dry_run, approval, or auto."""
    from naukri_server.database import update_agent_decision, store_notification
    from naukri_server.events import event_bus, AgentJobApplied

    config = decide_result["config"]
    candidates: list[AgentCandidate] = decide_result["candidates"]
    cycle_id = decide_result["cycle_id"]
    mode = config.get("mode", "approval")

    logger.info("Agent %s: act — mode=%s, %d candidates", cycle_id, mode, len(candidates),
                extra={"cycle_id": cycle_id, "step": "act"})

    act_result: dict

    if not candidates:
        act_result = {**decide_result, "applied": 0, "pending_approval": 0, "mode": mode, "note": "No candidates to act on"}
    elif mode == "dry_run":
        logger.info("Agent %s: dry_run — %d candidates logged, none applied", cycle_id, len(candidates),
                    extra={"cycle_id": cycle_id, "step": "act"})
        act_result = {
            **decide_result,
            "applied": 0,
            "pending_approval": 0,
            "mode": "dry_run",
            "note": f"Dry run — {len(candidates)} candidates logged, none applied",
        }
    elif mode == "approval":
        # Queue notification for user review
        job_list = "\n".join(
            f"- {c.title} at {c.company} (fit: {c.fit_score})"
            for c in candidates[:10]
        )
        try:
            await store_notification({
                "event_type": "AgentApprovalNeeded",
                "title": f"Agent found {len(candidates)} jobs to apply",
                "body": job_list,
                "priority": "high",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "metadata": json.dumps({
                    "cycle_id": cycle_id,
                    "job_ids": [c.job_id for c in candidates],
                }),
            })
            logger.info("Agent %s: approval notification stored for %d jobs", cycle_id, len(candidates),
                        extra={"cycle_id": cycle_id, "step": "act"})
        except Exception as e:
            logger.error("Agent %s: CRITICAL — failed to store approval notification: %s", cycle_id, e,
                         extra={"cycle_id": cycle_id, "step": "act"})
            act_result = {
                **decide_result,
                "applied": 0,
                "pending_approval": 0,
                "mode": "approval",
                "error": f"Notification storage failed: {e}",
            }
            # Emit act completed even on error
            try:
                from naukri_server.events import AgentActCompleted
                await event_bus.emit(AgentActCompleted(
                    cycle_id=cycle_id, mode=mode,
                    applied_count=0, pending_approval=0,
                ))
            except Exception as emit_err:
                logger.warning("Failed to emit AgentActCompleted: %s", emit_err,
                               extra={"cycle_id": cycle_id, "step": "act"})
            return act_result
        act_result = {
            **decide_result,
            "applied": 0,
            "pending_approval": len(candidates),
            "mode": "approval",
        }
    else:
        # mode == "auto"
        from naukri_server.tools.apply import _apply_single
        from naukri_server import kill_switch
        applied = 0
        errors = []
        halted = False
        for candidate in candidates:
            # FAIL-CLOSED: stop the auto-apply loop the moment the kill-switch
            # trips (e.g. a prior apply in this loop hit a block and tripped it).
            if kill_switch.is_tripped():
                halted = True
                logger.warning("Agent %s: auto-apply HALTED by kill-switch after %d applies",
                               cycle_id, applied, extra={"cycle_id": cycle_id, "step": "act"})
                break
            logger.info("Agent %s: auto-applying to %s at %s (fit: %d)",
                        cycle_id, candidate.job_id, candidate.company, candidate.fit_score,
                        extra={"cycle_id": cycle_id, "step": "act"})
            try:
                result = await _apply_single(
                    job_id=candidate.job_id,
                    title=candidate.title,
                    company=candidate.company,
                    tracking_extra={
                        "source": "agent",
                        "cycle_id": cycle_id,
                        "fit_score": candidate.fit_score,
                    },
                )
                status = result.get("status", "error")
                await update_agent_decision(cycle_id, candidate.job_id, status)
                if status == "applied":
                    applied += 1
                    try:
                        await event_bus.emit(AgentJobApplied(
                            cycle_id=cycle_id, job_id=candidate.job_id,
                            company=candidate.company, title=candidate.title,
                            fit_score=candidate.fit_score,
                        ))
                    except Exception as e:
                        logger.warning("Failed to emit %s: %s", "AgentJobApplied", e,
                                       extra={"cycle_id": cycle_id, "step": "act"})
                elif status == "halted":
                    # This apply hit a block and tripped the kill-switch — stop
                    # the loop now rather than firing more traffic into a block.
                    halted = True
                    logger.warning("Agent %s: auto-apply HALTED by kill-switch (apply returned halted)",
                                   cycle_id, extra={"cycle_id": cycle_id, "step": "act"})
                    break
            except Exception as e:
                errors.append(f"{candidate.job_id}: {e}")
                await update_agent_decision(cycle_id, candidate.job_id, "error")

        act_result = {
            **decide_result,
            "applied": applied,
            "pending_approval": 0,
            "failed": len(errors),
            "errors": errors[:5],
            "mode": "auto",
        }
        if halted:
            act_result["halted"] = True
            act_result["note"] = "Auto-apply halted by kill-switch (block detected)"

    try:
        from naukri_server.events import AgentActCompleted
        await event_bus.emit(AgentActCompleted(
            cycle_id=cycle_id, mode=mode,
            applied_count=act_result.get("applied", 0),
            pending_approval=act_result.get("pending_approval", 0),
        ))
    except Exception as e:
        logger.warning("Failed to emit AgentActCompleted: %s", e,
                       extra={"cycle_id": cycle_id, "step": "act"})

    return act_result


async def _learn(act_result: dict) -> dict:
    """Step 4: Log cycle results to DB and emit completion event."""
    from naukri_server.database import update_agent_run
    from naukri_server.events import event_bus, AgentCycleCompleted

    cycle_id = act_result["cycle_id"]
    applied = act_result.get("applied", 0)
    skipped = len(act_result.get("candidates", [])) - applied
    duration_ms = act_result.get("_duration_ms", 0)

    logger.info("Agent %s: learn — applied=%d, skipped=%d, duration=%.0fms",
                cycle_id, applied, skipped, duration_ms,
                extra={"cycle_id": cycle_id, "step": "learn"})

    await update_agent_run(
        cycle_id,
        finished_at=datetime.now(timezone.utc).isoformat(),
        status="completed",
        searches_run=act_result.get("searches_run", 0),
        jobs_found=act_result.get("total_found", 0),
        jobs_matched=act_result.get("total_matched", 0),
        applied_count=applied,
        skipped_count=skipped,
        duration_ms=duration_ms,
    )

    try:
        await event_bus.emit(AgentCycleCompleted(
            cycle_id=cycle_id, applied_count=applied,
            skipped_count=skipped, duration_ms=duration_ms,
            status="completed",
        ))
    except Exception as e:
        logger.warning("Failed to emit %s: %s", "AgentCycleCompleted", e,
                       extra={"cycle_id": cycle_id, "step": "learn"})

    return {
        "cycle_id": cycle_id,
        "applied": applied,
        "skipped": skipped,
        "mode": act_result.get("mode", "unknown"),
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

class _DailyLimitReached(Exception):
    """Raised by observe step when daily application limit is exhausted."""
    def __init__(self, daily_applied: int):
        self.daily_applied = daily_applied
        super().__init__(f"Daily application limit reached ({daily_applied} applied)")


async def run_agent_cycle(ctx: Context | None = None) -> dict:
    """Execute one observe->decide->act->learn cycle using SagaExecutor.

    Each step is a saga step with per-step timeout, timing, and error aggregation.
    On failure, completed steps are reported with their individual timings.

    Called by the scheduler task or manually via naukri_agent(action="run_now").
    """
    from naukri_server.database import insert_agent_run, update_agent_run
    from naukri_server.events import event_bus, AgentCycleStarted
    from naukri_server.sagas import SagaExecutor

    config = await asyncio.to_thread(load_agent_config)

    # Validate config
    errors = validate_agent_config(config)
    if errors:
        logger.error("Invalid agent config: %s", "; ".join(errors),
                     extra={"step": "config"})
        return {"status": "error", "message": "Config validation failed", "errors": errors}

    # Guard: not enabled
    if not config.get("enabled"):
        return {"status": "skipped", "reason": "Agent is disabled in config"}

    # Guard: quiet hours
    quiet = QuietHours.from_config(config)
    if quiet.is_active:
        return {"status": "skipped", "reason": "Quiet hours active"}

    cycle_id = f"cycle_{uuid4().hex[:12]}"
    started_at = datetime.now(timezone.utc).isoformat()

    # Insert run record
    await insert_agent_run({
        "cycle_id": cycle_id,
        "started_at": started_at,
        "mode": config.get("mode", "approval"),
        "metadata": json.dumps({"config_hash": config_hash(config)}),
    })

    enabled_searches = [s for s in config.get("searches", []) if s.get("enabled", True)]

    logger.info("Agent cycle %s starting — mode=%s, %d searches, config_hash=%s",
                cycle_id, config.get("mode"), len(enabled_searches), config_hash(config),
                extra={"cycle_id": cycle_id, "step": "start"})

    try:
        await event_bus.emit(AgentCycleStarted(
            cycle_id=cycle_id,
            mode=config.get("mode", "approval"),
            search_count=len(enabled_searches),
        ))
    except Exception as e:
        logger.warning("Failed to emit %s: %s", "AgentCycleStarted", e,
                       extra={"cycle_id": cycle_id, "step": "start"})

    # Build saga with shared mutable state for inter-step data flow
    saga = SagaExecutor("agent_cycle")
    cycle_state = {"cycle_id": cycle_id, "config": config}

    async def _step_observe():
        if ctx:
            try:
                await ctx.info(f"[{cycle_id}] observe — gathering applied IDs + daily quota")
            except Exception:
                pass
        result = await _observe(cycle_id, config)
        cycle_state.update(result)
        if ctx:
            try:
                await ctx.report_progress(
                    1, 4,
                    message=f"observe done ({result['daily_remaining']} slots)",
                )
            except Exception:
                pass
        # Early termination: raise if daily limit exhausted (except dry_run)
        if result["daily_remaining"] <= 0 and config.get("mode") != "dry_run":
            raise _DailyLimitReached(result["daily_applied"])
        return result

    async def _step_decide():
        if ctx:
            try:
                await ctx.info(f"[{cycle_id}] decide — running searches and scoring")
            except Exception:
                pass
        result = await _decide(cycle_state)
        cycle_state.update(result)
        if ctx:
            try:
                await ctx.report_progress(
                    2, 4,
                    message=f"decide done ({len(result.get('candidates', []))} candidates)",
                )
            except Exception:
                pass
        return result

    async def _step_act():
        if ctx:
            try:
                await ctx.info(
                    f"[{cycle_id}] act — mode={config.get('mode', 'approval')}, "
                    f"{len(cycle_state.get('candidates', []))} candidates"
                )
            except Exception:
                pass
        result = await _act(cycle_state)
        cycle_state.update(result)
        if ctx:
            try:
                await ctx.report_progress(
                    3, 4,
                    message=f"act done (applied={result.get('applied', 0)}, "
                            f"pending={result.get('pending_approval', 0)})",
                )
            except Exception:
                pass
        return result

    async def _step_learn():
        if ctx:
            try:
                await ctx.info(f"[{cycle_id}] learn — recording cycle results")
            except Exception:
                pass
        # Compute total duration from saga step timings so far
        cycle_state["_duration_ms"] = sum(
            ms for _, _, ms in saga.completed
        )
        result = await _learn(cycle_state)
        if ctx:
            try:
                await ctx.report_progress(
                    4, 4,
                    message=f"learn done (applied={result.get('applied', 0)}, "
                            f"skipped={result.get('skipped', 0)})",
                )
            except Exception:
                pass
        return result

    async def _compensate_observe():
        """Compensate observe: mark cycle as failed."""
        try:
            from naukri_server.database import update_agent_run as _update_run
            await _update_run(cycle_id, status="failed",
                              finished_at=datetime.now(timezone.utc).isoformat(),
                              error="saga_compensation_observe")
        except Exception as e:
            logger.warning("Observe compensation failed: %s", e,
                           extra={"cycle_id": cycle_id, "step": "compensate_observe"})

    async def _compensate_decide():
        """Compensate decide: clear candidate state."""
        cycle_state["candidates"] = []
        logger.info("Decide compensation: cleared candidates for %s", cycle_id,
                    extra={"cycle_id": cycle_id, "step": "compensate_decide"})

    saga.add_step("observe", _step_observe, compensate=_compensate_observe, timeout=60)
    saga.add_step("decide", _step_decide, compensate=_compensate_decide, timeout=180)
    saga.add_step("act", _step_act, timeout=300)
    saga.add_step("learn", _step_learn, timeout=30)

    saga_result = await saga.run()

    if saga_result["status"] == "success":
        # Attach step timings to the run metadata
        try:
            await update_agent_run(cycle_id,
                metadata=json.dumps({
                    "config_hash": config_hash(config),
                    "step_timings": saga_result["step_timings"],
                }))
        except Exception:
            pass

        # Return the learn step's result plus saga metadata
        learn_result = saga_result.get("results", {}).get("learn", {})

        if ctx:
            try:
                applied = learn_result.get("applied", 0)
                skipped = learn_result.get("skipped", 0)
                await ctx.info(
                    f"Cycle {cycle_id} complete: {applied} applied, {skipped} skipped"
                )
            except Exception:
                pass

        return {
            **learn_result,
            "status": "success",
            "step_timings": saga_result["step_timings"],
        }

    # Saga failed — check for daily-limit early termination
    if (saga_result.get("failed_step") == "observe"
            and any("_DailyLimitReached" in e for e in saga_result.get("errors", []))):
        await update_agent_run(cycle_id, status="skipped",
                               finished_at=datetime.now(timezone.utc).isoformat())
        return {
            "status": "skipped",
            "reason": "Daily application limit reached",
            "daily_applied": cycle_state.get("daily_applied", 0),
            "step_timings": saga_result.get("step_timings", {}),
        }

    # Generic saga failure
    total_duration_ms = sum(saga_result.get("step_timings", {}).values())
    error_msg = "; ".join(str(e) for e in saga_result.get("errors", []))

    await update_agent_run(
        cycle_id, status="error",
        finished_at=datetime.now(timezone.utc).isoformat(),
        duration_ms=total_duration_ms,
        error=error_msg[:500],
    )
    logger.error("Agent cycle %s saga failed: %s", cycle_id, error_msg,
                 extra={"cycle_id": cycle_id, "step": "saga_failed"})
    return {
        "status": "error",
        "cycle_id": cycle_id,
        "message": error_msg,
        "completed_steps": saga_result.get("completed_steps", []),
        "step_timings": saga_result.get("step_timings", {}),
    }
