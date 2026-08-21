"""Health probe that exercises 5 critical Naukri endpoints + checks for drift.

Distinct from naukri_server/health/probes/api.py:
  - api.py probes test LIVENESS only (does the endpoint return 200?).
  - api_validator probes test CORRECTNESS — does the response match the
    schema we recorded last time? Drift is reported but never fixed.

The probe runs every 15 minutes (longer than liveness probes — schema drift
is rare; we don't need 1-min granularity). It uses the module-level
DriftDetector singleton from naukri_server.probes.drift_detector so successive
runs accumulate snapshots.

First run for an endpoint: snapshot only (no drift to compare against).
Subsequent runs: compare to snapshot, report any drift via probe metadata.

Status policy:
  - All endpoints 200 + no drift → healthy
  - Any endpoint non-2xx → unhealthy (the api.py liveness probes already
    cover this, so we degrade rather than panic)
  - Any endpoint shows drift → degraded (with severity in metadata)
"""

from __future__ import annotations

from typing import Any

from naukri_server.health import health_probe, ProbeResult
from naukri_server.probes.drift_detector import detector as _drift_detector


# The 5 critical endpoints. Each tuple is (label, path, optional params).
# We intentionally never send a MUTATING request from a probe. Two of these were
# permanently failing until 2026-08-21 for call-shape reasons, not health ones:
#   - profile needed expand_level; without it the endpoint is a hard HTTP 400.
#   - recom-jobs is a POST-only SEARCH; GET is a hard HTTP 405. Reading it needs
#     POST, which is still side-effect-free (see _POST_ONLY_PATHS below).
# A probe whose call shape is wrong reports the endpoint as broken forever,
# which is indistinguishable from a check that cannot pass.
_CRITICAL_ENDPOINTS: list[tuple[str, str, dict[str, str] | None]] = [
    ("profile", "PROFILE_API", {"expand_level": "4"}),
    ("dashboard", "DASHBOARD_API", None),
    ("applications", "APPLIED_JOBS_API", {"pageNo": "1"}),
    ("recommendations", "RECOMMENDED_JOBS_API", {"pageNo": "1"}),  # search alternative
    ("settings", "FORMATTED_SETTINGS_API", None),
]

# Paths that only answer to POST. The verb is decided from the path rather than
# added to _fetch_one's signature on purpose: every existing test mocks
# _fetch_one(path, params), so widening the signature would silently break them
# and force the real client into unit tests.
_POST_ONLY_PATHS = frozenset({"/jobapi/v2/search/recom-jobs"})


async def _fetch_one(path: str, params: dict[str, str] | None) -> Any:
    """Read helper -- isolated for easy mocking in tests.

    Sends GET, except for the POST-only read endpoints above, which are
    searches: they return data and change nothing.
    """
    from naukri_server.interfaces import api_client
    if path in _POST_ONLY_PATHS:
        return await api_client.post(path, body=dict(params or {}))
    if params:
        return await api_client.get(path, params=params)
    return await api_client.get(path)


def _resolve_path(const_name: str) -> str:
    """Look up the URL constant from naukri_server.config by name."""
    from naukri_server import config as _cfg
    return getattr(_cfg, const_name)


@health_probe(
    name="probes.api_validator",
    interval=900,  # 15 minutes — drift is rare, no need for 1-min cadence
    criticality="informational",
    description="Validate 5 critical endpoints respond + detect schema drift",
)
async def api_validator_probe() -> ProbeResult:
    """Hit 5 critical endpoints, snapshot/compare schemas, report status.

    Returns ProbeResult.metadata fields:
      endpoints_total       — total endpoints checked (5)
      endpoints_ok          — count that returned a parseable response
      endpoints_failed      — count that raised
      drift_detected        — count that showed schema drift
      drift_severity        — worst severity across all drift reports
      drift_details         — {label: {url, severity, changed_field_count}}
      failures              — {label: error message}
    """
    endpoints_total = len(_CRITICAL_ENDPOINTS)
    endpoints_ok = 0
    failures: dict[str, str] = {}
    drift_details: dict[str, dict[str, Any]] = {}

    severity_rank = {"added": 0, "type_change": 1, "removed": 2}
    worst_severity: str | None = None
    worst_rank = -1

    for label, const_name, params in _CRITICAL_ENDPOINTS:
        try:
            path = _resolve_path(const_name)
        except AttributeError as exc:
            failures[label] = f"config constant {const_name} missing: {exc}"
            continue

        try:
            response = await _fetch_one(path, params)
        except Exception as exc:
            failures[label] = f"{type(exc).__name__}: {exc}"
            continue

        endpoints_ok += 1

        if not _drift_detector.has_snapshot(path):
            # First time we've seen this endpoint — record baseline only.
            try:
                _drift_detector.snapshot_endpoint(path, response)
            except ValueError:
                # url empty (shouldn't happen given _resolve_path) — ignore
                pass
            continue

        report = _drift_detector.check_drift(path, response)
        if report is not None:
            drift_details[label] = {
                "url": report.url,
                "severity": report.severity,
                "changed_field_count": len(report.changed_fields),
                "snapshot_age_days": round(report.snapshot_age_days, 1),
            }
            rank = severity_rank.get(report.severity, 0)
            if rank > worst_rank:
                worst_rank = rank
                worst_severity = report.severity

            # Emit the drift event so the healing router can react. We emit the
            # RAW detector output (changed_fields as {dotted_path: change_desc})
            # and intentionally do NOT synthesise fix parameters here — the
            # detector knows *what* changed, not the correct remediation. The
            # router treats a param-less event as detect→notify (the safe
            # default); it will only auto-fix if an enrichment layer supplies
            # explicit, validated fix parameters. Wrapped so a bus failure never
            # downgrades the probe's own status.
            try:
                from naukri_server.events import event_bus, EndpointDriftDetected
                await event_bus.emit(EndpointDriftDetected(
                    constant_name=const_name,
                    url=report.url,
                    severity=report.severity,
                    drift_type="field",  # schema/field drift; url moves arrive via discovery
                    changed_fields=dict(report.changed_fields),
                    snapshot_age_days=report.snapshot_age_days,
                ))
            except Exception:  # noqa: BLE001 — never let emit break the probe
                pass

    metadata: dict[str, Any] = {
        "endpoints_total": endpoints_total,
        "endpoints_ok": endpoints_ok,
        "endpoints_failed": len(failures),
        "drift_detected": len(drift_details),
    }
    if drift_details:
        metadata["drift_severity"] = worst_severity
        metadata["drift_details"] = drift_details
    if failures:
        metadata["failures"] = failures

    # Status policy
    if failures and endpoints_ok == 0:
        status = "unhealthy"
        message = f"All {endpoints_total} endpoints failed"
    elif failures:
        status = "degraded"
        message = f"{len(failures)} of {endpoints_total} endpoints failed"
    elif drift_details:
        status = "degraded"
        message = (
            f"Schema drift on {len(drift_details)} of {endpoints_total} endpoints "
            f"(worst: {worst_severity})"
        )
    else:
        status = "healthy"
        message = f"{endpoints_ok}/{endpoints_total} endpoints OK, no drift"

    return ProbeResult(status=status, message=message, metadata=metadata)
