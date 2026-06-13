"""Pragmatic Auto-Fix healing system.

Subscribes to EndpointDriftDetected (emitted by api_validator) and dispatches
by tier (see router.py):

  T1  → t1_autofix.py    (auto-edit parser/config + git commit, then a
                           post-commit ast.parse+import smoke check that
                           AUTO-REVERTS the commit if the edit broke the file)
  T2  → t2_autofix.py    (same edit as T1 + insert auto_fix_pending row;
                           the t2_verify_pending scheduler task verifies after
                           ~10 min and reverts if validator still reports drift)
  T3  → t3_notify.py     (build unified diff + store_notification, never
                           auto-applies)

Wiring: importing this package pulls in router.py, whose @subscriber decorator
registers on_endpoint_drift in the unified registry. naukri_server.subscribers
imports this package before calling wire_subscribers(event_bus), so the drift
subscriber is live at startup.

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

# Import the router so its @subscriber(EndpointDriftDetected) decorator runs and
# registers on_endpoint_drift in the framework registry. Placed AFTER the
# tier_registry import so router's `from naukri_server.healing import ...` sees a
# partially-initialized but sufficient package (tier_registry symbols are set).
from naukri_server.healing.router import on_endpoint_drift  # noqa: E402

__all__ = [
    "TIER_T1", "TIER_T2", "TIER_T3", "ALL_TIERS",
    "TierEntry", "tier_for", "has_tier", "all_constants",
    "constants_by_tier", "tier_counts",
    "on_endpoint_drift",
]
