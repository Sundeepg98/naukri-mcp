"""Pragmatic Auto-Fix healing system.

Subscribes to EndpointDriftDetected (emitted by api_validator) and dispatches
by tier:

  T1  → t1_autofix.py    (auto-edit parser/config + git commit, no verify)
  T2  → t2_autofix.py    (same as T1 + insert auto_fix_pending row, scheduler
                           verifies after ~10 min and reverts if validator
                           still reports drift)
  T3  → t3_notify.py     (build unified diff + store_notification, never
                           auto-applies)

Public API exported here is intentionally minimal — most callers should
interact via events (emit drift, subscribe to AutoFixApplied/Reverted) and
the daily-brief notifier integration.

Healing is DISABLED by default (see circuit.py); explicit user enable is
required before any auto-fix runs. This is a guard against the healer
modifying its own code mid-session.
"""

from naukri_server.healing.tier_registry import (
    TIER_T1, TIER_T2, TIER_T3, ALL_TIERS,
    TierEntry, tier_for, has_tier, all_constants,
    constants_by_tier, tier_counts,
)

__all__ = [
    "TIER_T1", "TIER_T2", "TIER_T3", "ALL_TIERS",
    "TierEntry", "tier_for", "has_tier", "all_constants",
    "constants_by_tier", "tier_counts",
]
