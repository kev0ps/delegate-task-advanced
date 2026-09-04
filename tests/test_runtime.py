import json
import threading
from pathlib import Path

from agent.subagent_lifecycle import (
    SubagentLaunchRequest,
    SubagentLifecycleService,
    SubagentState,
)
from support import BlockingChild, ImmediateChild, RuntimeContext, make_parent
from tools.delegate_tool import list_active_subagents
from tools.process_registry import process_registry


def test_cancellation_before_lifecycle_launch_does_not_orphan_child(
    registered_plugin, stub_delegation
):
    calls = stub_delegation(
        registered_plugin.module,
        before_return=lambda kwargs: kwargs["interrupt_fn"](),
    )

    payload = json.loads(
        registered_plugin.handler({"name": "Reviewer", "goal": "Review"})
    )

    assert payload["success"] is True
    assert payload["status"] == "interrupted"
    assert payload["subagent_id"] is None
    assert len(calls) == 1
    assert registered_plugin.context.subagent_lifecycle.requests == []


def test_async_lifecycle_exception_keeps_human_name(
    registered_plugin, stub_delegation, monkeypatch
):
    def raise_wait_error(*args, **kwargs):
        raise RuntimeError("wait exploded")

    calls = stub_delegation(registered_plugin.module)
    monkeypatch.setattr(
        registered_plugin.context.subagent_lifecycle,
        "wait",
        raise_wait_error,
    )
    payload = json.loads(
        registered_plugin.handler({"name": "Named reviewer", "goal": "Review"})
    )
    result = calls[0]["runner"]()

    assert payload["success"] is True
    assert result["status"] == "error"
    assert "Named reviewer" in result["error"]
    assert "wait exploded" in result["error"]


def test_invalid_structured_result_gets_one_bounded_correction_retry(
    registered_plugin, stub_delegation
):
    schema = {
        "type": "object",
        "properties": {"finding": {"type": "string"}},
        "required": ["finding"],
    }
    calls = stub_delegation(registered_plugin.module)
    payload = json.loads(
        registered_plugin.handler(
            {"name": "Structured", "goal": "Review", "output_schema": schema}
        )
    )
    result = calls[0]["runner"]()

    assert payload["success"] is True
    assert len(registered_plugin.context.subagent_lifecycle.requests) == 2
    assert result["schema_retries"] == 1
    assert result["schema_valid"] is False
    assert result["schema_errors"]
    assert "Correct the previous final response" in (
        registered_plugin.context.subagent_lifecycle.requests[1].goal
    )


def test_failed_correction_launch_preserves_first_validation_failure(
    registered_plugin, stub_delegation, monkeypatch
):
    def raise_retry_error(request):
        raise RuntimeError("retry launch failed")

    calls = stub_delegation(registered_plugin.module)
    payload = json.loads(
        registered_plugin.handler(
            {
                "name": "Structured",
                "goal": "Review",
                "output_schema": {"type": "object", "required": ["finding"]},
            }
        )
    )
    monkeypatch.setattr(
        registered_plugin.context.subagent_lifecycle,
        "launch",
        raise_retry_error,
    )
    result = calls[0]["runner"]()

    assert payload["success"] is True
    assert result["status"] == "error"
    assert result["schema_valid"] is False
    assert result["schema_errors"]
    assert result["schema_retries"] == 1
    assert "retry launch failed" in result["schema_retry_error"]
    assert result["exit_reason"] == "output_schema_invalid"
    assert "output_schema validation failed" in result["error"]


def test_plugin_routes_real_lifecycle_result_to_shared_completion_queue(
    plugin_package, clean_runtime_registry, monkeypatch
):
    from tools import delegate_tool

    parent = make_parent()
    context = RuntimeContext(parent)
    plugin_package.register(context)
    monkeypatch.setattr(
        delegate_tool,
        "_build_child_preserving_parent_tools",
        lambda **kwargs: ImmediateChild(),
    )

    launched = json.loads(
        context.tools[0]["handler"](
            {
                "name": "Runtime reviewer",
                "goal": "Inspect public files",
                "toolsets": ["file"],
            },
            parent_agent=parent,
        )
    )

    assert launched["success"] is True, launched
    assert launched["name"] == "Runtime reviewer"
    assert launched["model"] == "inherited-model"
    assert launched["toolsets"] == ["file"]
    assert launched["live_transcript"]

    event = process_registry.completion_queue.get(timeout=5)
    assert event["type"] == "async_delegation"
    assert event["delegation_id"] == launched["delegation_id"]
    assert event["parent_session_id"] == "parent-runtime"
    assert event["status"] == "completed"
    assert "Runtime reviewer" in event["summary"]
    assert event["toolsets"] == ["file"]

    live_text = Path(launched["live_transcript"]).read_text(encoding="utf-8")
    assert "Runtime reviewer" in live_text
    assert "end status=completed" in live_text


def test_public_lifecycle_orchestrator_adds_delegation_beyond_requested_toolsets(
    plugin_package, clean_runtime_registry, monkeypatch
):
    import run_agent
    from tools import delegate_tool

    parent = make_parent()
    context = RuntimeContext(parent)
    plugin_package.register(context)
    children = []

    def build_child(**kwargs):
        child = ImmediateChild(**kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(run_agent, "AIAgent", build_child)
    monkeypatch.setattr(delegate_tool, "_get_max_spawn_depth", lambda: 2)
    monkeypatch.setattr(delegate_tool, "_get_orchestrator_enabled", lambda: True)

    launched = json.loads(
        context.tools[0]["handler"](
            {
                "name": "Configured orchestrator",
                "goal": "Coordinate workers",
                "toolsets": ["file"],
            },
            parent_agent=parent,
        )
    )

    assert launched["success"] is True, launched
    assert launched["effective_role"] == "orchestrator"
    assert launched["depth"] == 1
    assert len(children) == 1
    assert "file" in children[0].enabled_toolsets
    assert "delegation" in children[0].enabled_toolsets
    process_registry.completion_queue.get(timeout=5)


def test_public_lifecycle_reuses_shared_active_registry_and_result_path(
    clean_runtime_registry, monkeypatch
):
    from tools import delegate_tool

    started = threading.Event()
    release = threading.Event()
    child = BlockingChild(started, release)
    parent = make_parent(session_id="parent-session", enabled_toolsets=["file"])
    service = SubagentLifecycleService(lambda: parent)

    try:
        monkeypatch.setattr(
            delegate_tool,
            "_build_child_preserving_parent_tools",
            lambda **kwargs: child,
        )
        handle = service.launch(
            SubagentLaunchRequest(
                goal="Subagent 'Integration reviewer' — inspect public files",
                allowed_toolsets=("file",),
                parent_session_id="parent-session",
                correlation_id="integration-correlation",
                metadata={"display_name": "Integration reviewer"},
            )
        )

        assert started.wait(3)
        active = list_active_subagents()
        assert any(row["subagent_id"] == handle.subagent_id for row in active)
        assert any("Integration reviewer" in row["goal"] for row in active)
        release.set()
        terminal = service.wait(handle, timeout_seconds=5)
        assert terminal.completed is True
        result = service.result(handle)
        assert result.terminal_state == SubagentState.SUCCEEDED
        assert result.summary == "integration result"
        assert not any(
            row["subagent_id"] == handle.subagent_id for row in list_active_subagents()
        )
        assert child.closed is True
    finally:
        release.set()
