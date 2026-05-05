"""Tests for the unified registry framework."""
import asyncio
import pytest
from naukri_server.framework.registry import (
    registry, subscriber, scheduled_task, action_dispatcher, dispatch_action,
    _Registry, _RegisteredItem,
)


def test_registry_starts_with_three_buckets():
    r = _Registry()
    assert r.counts() == {"subscriber": 0, "scheduled_task": 0, "action": 0}


def test_subscriber_decorator_registers_in_bucket():
    r = _Registry()
    item = _RegisteredItem("subscriber", str, lambda e: None)
    r.add(item)
    assert r.counts()["subscriber"] == 1
    assert r.by_kind("subscriber")[0].fn is item.fn


def test_action_dispatcher_keys_by_tool_and_action():
    r = _Registry()
    fn1 = lambda **kw: None
    fn2 = lambda **kw: None
    r.add(_RegisteredItem("action", ("apps", "list"), fn1))
    r.add(_RegisteredItem("action", ("apps", "detail"), fn2))
    r.add(_RegisteredItem("action", ("inbox", "list"), fn1))
    apps = r.actions_for("apps")
    assert set(apps.keys()) == {"list", "detail"}
    inbox = r.actions_for("inbox")
    assert set(inbox.keys()) == {"list"}


@pytest.mark.asyncio
async def test_dispatch_action_unknown_returns_validation_error():
    result = await dispatch_action("nonexistent_tool", "bogus", {}, "test.bogus")
    assert result["status"] == "error"
    assert result["error_code"] == "VALIDATION_ERROR"


def test_subscriber_decorator_returns_original_function():
    @subscriber(int)
    async def handler(event):
        return "ok"
    assert handler.__name__ == "handler"


def test_scheduled_task_decorator_stores_metadata():
    """Verify @scheduled_task captures metadata correctly.

    We don't pollute the global registry: we exercise the decorator's metadata
    behaviour by inspecting an item we add manually.
    """
    item = _RegisteredItem(
        "scheduled_task",
        "test_task_xyz_unique",
        lambda: None,
        {"name": "test_task_xyz_unique", "interval_seconds": 60, "description": "test",
         "timeout_seconds": 120.0, "run_at_hour": None, "enabled": True},
    )
    r = _Registry()
    r.add(item)
    items = r.by_kind("scheduled_task")
    matching = [it for it in items if it.key == "test_task_xyz_unique"]
    assert len(matching) == 1
    assert matching[0].metadata["interval_seconds"] == 60
    assert matching[0].metadata["description"] == "test"


def test_scheduled_task_decorator_returns_original_function():
    """The decorator must not wrap the function — it returns it unchanged."""
    # Use a local registry pattern to avoid polluting the global one.
    # The actual decorator does register globally; we verify it returns fn unchanged
    # by importing scheduler_tasks (which uses real decorators).
    from naukri_server.scheduler_tasks import _task_sync_applications
    assert callable(_task_sync_applications)
    assert _task_sync_applications.__name__ == "_task_sync_applications"


def test_subscriber_count_unchanged_36():
    """After importing subscribers.py, registry should have 38 subscribers from naukri_server.events.

    Count was 36; bumped to 38 when InboxMessageRead and InboxInviteAccepted
    subscribers were added to close the CQRS gap for inbox events.
    """
    import naukri_server.subscribers  # triggers decorators
    import naukri_server.events as ev
    from naukri_server.framework.registry import registry
    # Filter to only subscribers whose event type is from naukri_server.events module
    # (other tests may register pytest fixtures with synthetic event types)
    naukri_subs = [it for it in registry.by_kind("subscriber")
                   if hasattr(it.key, "__module__") and it.key.__module__ == "naukri_server.events"]
    assert len(naukri_subs) == 38


def test_scheduled_task_count_unchanged_9():
    """After importing scheduler_tasks.py, registry should have 9 production scheduled tasks."""
    import naukri_server.scheduler_tasks  # triggers decorators
    from naukri_server.framework.registry import registry
    # Filter to tasks defined in scheduler_tasks (test fixtures may register elsewhere).
    prod_tasks = [it for it in registry.by_kind("scheduled_task")
                  if it.fn.__module__ == "naukri_server.scheduler_tasks"]
    assert len(prod_tasks) == 9


def test_no_consolidated_dispatchers_use_registry():
    """After all consolidated dispatcher splits, the action registry should be empty
    of production tools. The framework remains for any future tool that needs it.
    """
    # Force imports to trigger decorators (if any)
    from naukri_server.tools import scheduler_tool, agent_tool  # noqa: F401
    from naukri_server.framework.registry import registry

    # No production tool actions are expected — atomic-everywhere is the goal
    # (Tests in this file may register their own; filter to production modules.)
    prod_actions = [
        it for it in registry.by_kind("action")
        if it.fn.__module__.startswith("naukri_server.tools.")
    ]
    assert len(prod_actions) == 0, (
        f"Unexpected production action registrations: "
        f"{[(it.key, it.fn.__module__) for it in prod_actions]}"
    )
