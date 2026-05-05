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

# Importing api_validator triggers its @health_probe registration so the
# probe scheduler picks it up at startup. Wrapped in try/except so a probe
# import failure (e.g. missing config constant during dev) doesn't break
# package import for callers who only need DriftDetector.
import logging as _logging
_logger = _logging.getLogger(__name__)
try:
    from naukri_server.probes import api_validator  # noqa: F401
except Exception as _e:  # pragma: no cover — defensive
    _logger.warning("Failed to load api_validator probe: %s", _e)

__all__ = ["DriftDetector", "DriftReport"]
