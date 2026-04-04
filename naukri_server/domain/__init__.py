"""Domain layer — rich domain objects encoding business rules.

The ``safe_get`` helper is the universal API response accessor.
All external API data MUST be accessed through safe_get or domain factories.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def safe_get(
    data: dict,
    *keys: str,
    default: Any = None,
    field_name: str = "",
    warn: bool = False,
    context: str = "",
) -> Any:
    """Try *keys* in order on *data*, return first non-None hit.

    This is the Anti-Corruption Layer accessor — all external API data
    should flow through this function to enable missing-field detection.

    Args:
        data: Source dictionary (typically a raw API response fragment).
        *keys: One or more dict keys to try, in priority order.
        default: Fallback when every key misses.
        field_name: Human label for log messages (only used when *warn* is True).
        warn: If True and every key misses, emit a WARNING log line.
        context: Additional context for the log message (e.g., "job_id=123").

    Returns:
        The first non-None value found, or *default*.
    """
    for key in keys:
        if not isinstance(data, dict):
            break
        val = data.get(key)
        if val is not None:
            return val
    if warn and field_name:
        ctx = f" ({context})" if context else ""
        logger.warning("Field '%s' missing%s — tried keys %s", field_name, ctx, keys)
    return default
