"""Agent service — abstracts database operations for the autonomous agent."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AgentService:
    """Service layer for agent database operations.

    Provides a clean API for the agent cycle, isolating business logic
    from direct database access.
    """

    @staticmethod
    async def get_applied_ids() -> set:
        """Get set of all applied job IDs."""
        from naukri_server.database import get_applied_job_ids
        return await get_applied_job_ids()

    @staticmethod
    async def count_daily_applied(date_str: str) -> int:
        """Count applications submitted today (excludes synced)."""
        from naukri_server.database import count_daily_applied
        return await count_daily_applied(date_str)

    @staticmethod
    async def insert_run(run: dict) -> int:
        """Insert an agent cycle run record."""
        from naukri_server.database import insert_agent_run
        return await insert_agent_run(run)

    @staticmethod
    async def update_run(cycle_id: str, **fields):
        """Update an agent run with results."""
        from naukri_server.database import update_agent_run
        return await update_agent_run(cycle_id, **fields)

    @staticmethod
    async def insert_decision(cycle_id: str, job_id: str, decision: str, **kw) -> int:
        """Record an agent decision about a job."""
        from naukri_server.database import insert_agent_decision
        return await insert_agent_decision(cycle_id, job_id, decision, **kw)

    @staticmethod
    async def update_decision(cycle_id: str, job_id: str, apply_status: str):
        """Update the apply status of a decision."""
        from naukri_server.database import update_agent_decision
        return await update_agent_decision(cycle_id, job_id, apply_status)

    @staticmethod
    async def store_notification(notif: dict):
        """Store a notification for the user."""
        from naukri_server.database import store_notification
        return await store_notification(notif)

    @staticmethod
    async def get_metrics(days: int = 7) -> dict:
        """Get agent performance metrics."""
        from naukri_server.database import get_agent_metrics
        return await get_agent_metrics(days)

    @staticmethod
    async def get_skip_stats(days: int = 7) -> dict:
        """Get aggregated skip reason statistics."""
        from naukri_server.database import get_agent_skip_stats
        return await get_agent_skip_stats(days)


# Module-level singleton
agent_service = AgentService()
