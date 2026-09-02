import json
from types import SimpleNamespace

import pytest

from support import FakeContext


def test_launch_loads_deduplicated_skills_and_passes_metadata(
    plugin_package, plugin_module, monkeypatch
):
    context = FakeContext(
        {"code-review": {"success": True, "content": "REVIEW RULES"}}
    )
    plugin_package.register(context)
    parent = SimpleNamespace(
        session_id="parent-session", enabled_toolsets=["delegation", "file"]
    )
    monkeypatch.setattr(
        plugin_module,
        "dispatch_completion_watcher",
        lambda **kwargs: {"status": "dispatched", "delegation_id": "deleg-test"},
    )

    payload = json.loads(
        context.tools[0]["handler"](
            {
                "name": "Relay reviewer",
                "goal": "Review the public diff",
                "context": "Public repository",
                "skills": ["code-review", "code-review"],
                "toolsets": ["file"],
            },
            parent_agent=parent,
            task_id="parent-task",
        )
    )

    assert payload["success"] is True
    assert payload["subagent_id"] == "sa-test-1234"
    assert payload["depth"] == 1
    request = context.subagent_lifecycle.requests[0]
    assert request.model is None
    assert request.allowed_toolsets == ("file",)
    assert "REVIEW RULES" in request.context
    assert request.metadata["display_name"] == "Relay reviewer"
    assert request.metadata["requested_skills"] == ["code-review"]


def test_rejects_unknown_skill_before_launch(registered_plugin):
    payload = json.loads(
        registered_plugin.handler(
            {
                "name": "Reviewer",
                "goal": "Review this",
                "skills": ["missing-skill"],
            }
        )
    )

    assert payload["success"] is False
    assert "missing-skill" in payload["error"]
    assert registered_plugin.context.subagent_lifecycle.requests == []


def test_appends_framed_skill_context(plugin_module):
    def dispatch(_name, args, **_kwargs):
        return json.dumps({"success": True, "content": f"content:{args['name']}"})

    context, names = plugin_module.build_skill_context(
        "base", ["alpha", "beta"], dispatch
    )

    assert names == ["alpha", "beta"]
    assert "base" in context
    assert "BEGIN EXPLICIT SKILLS" in context
    assert "content:alpha" in context


def test_retries_skill_view_without_task_id_when_parent_dedup_hides_content(
    plugin_module,
):
    calls = []

    def dispatch(_name, args, **kwargs):
        calls.append((args, kwargs))
        if kwargs.get("task_id") == "parent-task":
            return json.dumps(
                {
                    "success": True,
                    "status": "unchanged",
                    "dedup": True,
                    "content_returned": False,
                }
            )
        return json.dumps({"success": True, "content": "# Injected skill"})

    context, names = plugin_module.build_skill_context(
        None,
        ["alpha"],
        dispatch,
        dispatch_kwargs={"task_id": "parent-task", "session_id": "session"},
    )

    assert names == ["alpha"]
    assert "# Injected skill" in context
    assert len(calls) == 2
    assert calls[1][1] == {"session_id": "session"}


def test_fails_loudly_on_malformed_or_oversized_skill_payload(plugin_module):
    def malformed(*_args, **_kwargs):
        return "not-json"

    with pytest.raises(ValueError, match="malformed"):
        plugin_module.build_skill_context(None, ["alpha"], malformed)

    def huge(*_args, **_kwargs):
        return json.dumps({"success": True, "content": "x" * 25_000})

    with pytest.raises(ValueError, match="limit"):
        plugin_module.build_skill_context(None, ["alpha"], huge, max_chars=100)
