import json

import pytest


@pytest.mark.parametrize("role", ["leaf", "orchestrator", None])
def test_role_field_is_rejected_as_unknown_before_launch(registered_plugin, role):
    payload = json.loads(
        registered_plugin.handler({"name": "Reviewer", "goal": "Review", "role": role})
    )

    assert payload["success"] is False
    assert "Unknown fields: role" in payload["error"]
    assert registered_plugin.context.subagent_lifecycle.requests == []


def test_batch_tasks_field_is_rejected(registered_plugin):
    payload = json.loads(
        registered_plugin.handler(
            {
                "tasks": [
                    {"name": "Security", "goal": "Analyze security risks"},
                    {"name": "Logic", "goal": "Analyze logic regressions"},
                ]
            }
        )
    )

    assert payload["success"] is False
    assert "Unknown fields: tasks" in payload["error"]
    assert registered_plugin.context.subagent_lifecycle.requests == []


@pytest.mark.parametrize("model", ["", " ", "bad model", "bad\nmodel"])
def test_rejects_empty_or_malformed_model_before_launch(registered_plugin, model):
    payload = json.loads(
        registered_plugin.handler(
            {"name": "Reviewer", "goal": "Review", "model": model}
        )
    )

    assert payload["success"] is False
    assert "model" in payload["error"]
    assert registered_plugin.context.subagent_lifecycle.requests == []


def test_rejects_control_characters_and_unknown_fields(registered_plugin):
    bad_name = json.loads(
        registered_plugin.handler({"name": "bad\nname", "goal": "Review"})
    )
    unknown = json.loads(
        registered_plugin.handler(
            {"name": "Reviewer", "goal": "Review", "provider": "x"}
        )
    )

    assert bad_name["success"] is False
    assert unknown["success"] is False
    assert "Unknown fields" in unknown["error"]


def test_rejects_toolset_not_enabled_for_parent(registered_plugin):
    from types import SimpleNamespace

    parent = SimpleNamespace(
        session_id="parent-session", enabled_toolsets=["delegation"]
    )
    payload = json.loads(
        registered_plugin.handler(
            {"name": "Reviewer", "goal": "Review", "toolsets": ["file"]},
            parent_agent=parent,
        )
    )

    assert payload["success"] is False
    assert "parent" in payload["error"].lower()
    assert registered_plugin.context.subagent_lifecycle.requests == []


def test_explicit_empty_toolsets_is_rejected(registered_plugin):
    payload = json.loads(
        registered_plugin.handler(
            {"name": "Reviewer", "goal": "Review", "toolsets": []}
        )
    )

    assert payload["success"] is False
    assert "toolsets" in payload["error"]


def test_normalize_name_list_dedupes_preserving_order(plugin_module):
    assert plugin_module.normalize_name_list(
        ["file_readonly", "file_readonly", "web"], "toolsets"
    ) == ["file_readonly", "web"]


def test_rejects_invalid_names_and_oversized_lists(plugin_module):
    with pytest.raises(ValueError, match="skills"):
        plugin_module.normalize_name_list(["../secret"], "skills")
    with pytest.raises(ValueError, match="at most"):
        plugin_module.normalize_name_list(
            [f"skill-{index}" for index in range(9)], "skills"
        )


def test_display_name_is_normalized_and_rejects_format_characters(plugin_module):
    assert plugin_module.validate_display_name("  Relay reviewer  ") == "Relay reviewer"
    with pytest.raises(ValueError):
        plugin_module.validate_display_name("**format injection**")
