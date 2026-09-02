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


def test_plugin_routes_real_lifecycle_result_to_shared_completion_queue(
    plugin_package, clean_runtime_registry, monkeypatch
):
    import tools.delegate_tool as delegate_tool

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
    import tools.delegate_tool as delegate_tool

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
    import tools.delegate_tool as delegate_tool

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
            row["subagent_id"] == handle.subagent_id
            for row in list_active_subagents()
        )
        assert child.closed is True
    finally:
        release.set()
