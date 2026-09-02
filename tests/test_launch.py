import json
from types import SimpleNamespace


def dispatched(**extra):
    return {"status": "dispatched", "delegation_id": "deleg-test", **extra}


def test_optional_model_is_passed_to_native_lifecycle(registered_plugin, monkeypatch):
    calls = []

    def watcher(**kwargs):
        calls.append(kwargs)
        return dispatched()

    monkeypatch.setattr(registered_plugin.module, "dispatch_completion_watcher", watcher)
    parent = SimpleNamespace(
        session_id="parent-session", enabled_toolsets=["delegation", "file"]
    )
    payload = json.loads(
        registered_plugin.handler(
            {
                "name": "Luna reviewer",
                "goal": "Review the public diff",
                "model": "gpt-5.6-luna",
                "toolsets": ["file"],
            },
            parent_agent=parent,
        )
    )

    assert payload["success"] is True
    assert payload["model"] == "gpt-5.6-luna"
    request = registered_plugin.context.subagent_lifecycle.requests[0]
    assert request.model == "gpt-5.6-luna"
    assert request.metadata["requested_model"] == "gpt-5.6-luna"
    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-5.6-luna"


def test_omitted_model_keeps_native_parent_inheritance(registered_plugin, monkeypatch):
    calls = []

    def watcher(**kwargs):
        calls.append(kwargs)
        return dispatched()

    monkeypatch.setattr(registered_plugin.module, "dispatch_completion_watcher", watcher)
    payload = json.loads(
        registered_plugin.handler({"name": "Reviewer", "goal": "Review"})
    )

    assert payload["success"] is True
    request = registered_plugin.context.subagent_lifecycle.requests[0]
    assert request.model is None
    assert request.metadata["requested_model"] is None
    assert calls[0]["model"] is None


def test_launch_reports_derived_effective_role_from_hermes_handle(
    registered_plugin, monkeypatch
):
    monkeypatch.setattr(
        registered_plugin.context.subagent_lifecycle,
        "launch",
        lambda request: SimpleNamespace(
            subagent_id="sa-derived",
            model=request.model or "inherited-model",
            role="leaf",
            depth=1,
        ),
    )
    monkeypatch.setattr(
        registered_plugin.module,
        "dispatch_completion_watcher",
        lambda **kwargs: dispatched(),
    )

    payload = json.loads(
        registered_plugin.handler({"name": "Reviewer", "goal": "Review"})
    )

    assert payload["success"] is True
    assert payload["effective_role"] == "leaf"
    assert payload["depth"] == 1
    assert "requested_role" not in payload
    assert "from the spawn depth" in payload["note"]


def test_watcher_rejection_never_launches_child(registered_plugin, monkeypatch):
    monkeypatch.setattr(
        registered_plugin.module,
        "dispatch_completion_watcher",
        lambda **kwargs: {"status": "rejected", "error": "capacity"},
    )

    payload = json.loads(
        registered_plugin.handler({"name": "Reviewer", "goal": "Review"})
    )

    assert payload["success"] is False
    assert "Reviewer" in payload["error"]
    assert registered_plugin.context.subagent_lifecycle.requests == []


def test_watcher_setup_exception_never_launches_child(registered_plugin, monkeypatch):
    def raise_queue_error(**kwargs):
        raise RuntimeError("queue offline")

    monkeypatch.setattr(
        registered_plugin.module, "dispatch_completion_watcher", raise_queue_error
    )
    payload = json.loads(
        registered_plugin.handler({"name": "Reviewer", "goal": "Review"})
    )

    assert payload["success"] is False
    assert "Reviewer" in payload["error"]
    assert registered_plugin.context.subagent_lifecycle.requests == []


def test_named_errors_cover_skill_toolset_and_launch_failures(
    registered_plugin, monkeypatch
):
    skill_error = json.loads(
        registered_plugin.handler(
            {"name": "Named reviewer", "goal": "Review", "skills": ["missing-skill"]}
        )
    )
    assert "Named reviewer" in skill_error["error"]

    parent = SimpleNamespace(
        session_id="parent-session", enabled_toolsets=["delegation"]
    )
    toolset_error = json.loads(
        registered_plugin.handler(
            {
                "name": "Named reviewer",
                "goal": "Review",
                "toolsets": ["file_readonly"],
            },
            parent_agent=parent,
        )
    )
    assert "Named reviewer" in toolset_error["error"]

    def raise_launch_error(request):
        raise RuntimeError("launch exploded")

    monkeypatch.setattr(
        registered_plugin.context.subagent_lifecycle, "launch", raise_launch_error
    )
    monkeypatch.setattr(
        registered_plugin.module,
        "dispatch_completion_watcher",
        lambda **kwargs: dispatched(),
    )
    launch_error = json.loads(
        registered_plugin.handler({"name": "Named reviewer", "goal": "Review"})
    )
    assert "Named reviewer" in launch_error["error"]


def test_cancellation_before_handle_does_not_orphan_later_launch(
    plugin_module, fake_lifecycle
):
    state = plugin_module.DeferredLaunch(fake_lifecycle, "Reviewer")
    state.cancel()

    assert state.cancel_requested is True
    assert state.launch_error is None
    handle = fake_lifecycle.launch(SimpleNamespace(correlation_id="corr"))
    state.set_handle(handle)
    result = state.run()
    assert result["status"] == "completed"
    assert len(fake_lifecycle.cancelled) == 1


def test_async_lifecycle_exception_keeps_human_name(
    plugin_module, fake_lifecycle, monkeypatch
):
    def raise_wait_error(*args, **kwargs):
        raise RuntimeError("wait exploded")

    monkeypatch.setattr(fake_lifecycle, "wait", raise_wait_error)
    state = plugin_module.DeferredLaunch(fake_lifecycle, "Named reviewer")
    handle = fake_lifecycle.launch(SimpleNamespace(correlation_id="corr"))
    state.set_handle(handle)

    result = state.run()

    assert result["status"] == "error"
    assert "Named reviewer" in result["error"]
    assert "wait exploded" in result["error"]
