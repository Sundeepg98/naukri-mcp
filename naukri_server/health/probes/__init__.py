"""Health probes — auto-imported to trigger registration via @health_probe decorator."""

import logging
logger = logging.getLogger(__name__)

_probe_modules = [
    "naukri_server.health.probes.browser",
    "naukri_server.health.probes.api",
    "naukri_server.health.probes.session",
    "naukri_server.health.probes.database",
    "naukri_server.health.probes.pool",
    "naukri_server.health.probes.events",
    "naukri_server.health.probes.notifications",
    "naukri_server.health.probes.scraping",
    "naukri_server.health.probes.system",
    "naukri_server.health.probes.discovery",
]

for mod in _probe_modules:
    try:
        __import__(mod)
    except Exception as e:
        logger.warning("Failed to load probe module %s: %s", mod, e)
