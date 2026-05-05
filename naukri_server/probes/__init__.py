"""Probing layer — drift detection + endpoint validators.

Distinct from naukri_server/health/probes/ (which monitors *liveness* — is the
service up, is the browser responsive). This package monitors *correctness* —
has the upstream API schema changed in a way that would break parsing?

NO auto-fix. This layer only DETECTS drift and reports it. Any healing layer
(future work) consumes the DriftReport objects this package emits.

Modules:
  drift_detector  — DriftDetector class + DriftReport dataclass
  api_validator   — @health_probe that exercises 5 critical endpoints

The api_validator probe is auto-registered when this package is imported, the
same pattern as naukri_server/health/probes/.
"""

from naukri_server.probes.drift_detector import DriftDetector, DriftReport

__all__ = ["DriftDetector", "DriftReport"]
