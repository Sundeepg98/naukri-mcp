"""Registry framework — unified plugin pattern for subscribers, tasks, action dispatchers."""
from naukri_server.framework.registry import (
    registry, subscriber, scheduled_task, action_dispatcher,
    dispatch_action, wire_subscribers, wire_scheduled_tasks,
)
__all__ = ["registry", "subscriber", "scheduled_task", "action_dispatcher",
           "dispatch_action", "wire_subscribers", "wire_scheduled_tasks"]
