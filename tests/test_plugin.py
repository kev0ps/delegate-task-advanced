import copy
import json
from types import SimpleNamespace

import pytest
from support import FakeContext

# ---- Registration and tool contract ---------------------------------------


def test_register_adds_advanced_tool_without_touching_native_delegation(
    plugin_package, fake_context
):
    from tools.delegate_tool import DELEGATE_TASK_SCHEMA, delegate_task
    from toolsets import TOOLSETS

    original_toolsets = copy.deepcopy(TOOLSETS)
    original_delegate_toolset = dict(TOOLSETS["delegation"])
    original_delegate_schema = copy.deepcopy(DELEGATE_TASK_SCHEMA)
    original_delegate_handler = delegate_task

    plugin_package.register(fake_context)

    assert [tool["name"] for tool in fake_context.tools] == ["delegate_task_advanced"]
    assert fake_context.tools[0]["toolset"] == "delegation"
    assert TOOLSETS == original_toolsets
    assert TOOLSETS["delegation"] == original_delegate_toolset
    assert DELEGATE_TASK_SCHEMA == original_delegate_schema
    assert delegate_task is original_delegate_handler
    assert fake_context.unload_callbacks == []


def test_package_init_exports_the_public_register_hook(plugin_package):
    assert callable(plugin_package.register)


def test_schema_omits_provider_tuning_and_batch_fields(registered_plugin):
    parameters = registered_plugin.tool["schema"]["parameters"]

    assert parameters["additionalProperties"] is False
    assert set(parameters["required"]) == {"name", "goal"}
    assert "model" in parameters["properties"]
    assert "provider" not in parameters["properties"]
    assert "reasoning_effort" not in parameters["properties"]
    assert "role" not in parameters["properties"]
    assert "tasks" not in parameters["properties"]
    assert "output_schema" in parameters["properties"]
    assert (
        "Hermes decides the subagent role"
        in registered_plugin.tool["schema"]["description"]
    )


def test_schema_describes_selection_and_security_boundaries(registered_plugin):
    tool = registered_plugin.tool
    description = tool["schema"]["description"]
    properties = tool["schema"]["parameters"]["properties"]

    for phrase in (
        "one named Hermes subagent",
        "fresh conversation",
        "returns immediately",
    ):
        assert phrase in description
    assert "not the generated subagent_id" in properties["name"]["description"]
    assert "cannot see the parent conversation" in properties["goal"]["description"]
    assert "grant no permissions" in properties["skills"]["description"]
    assert "not individual tool names" in properties["toolsets"]["description"]
    assert "do not imply read-only access" in properties["toolsets"]["description"]
    assert "cannot switch providers" in properties["model"]["description"]
    assert "one named Hermes subagent" in tool["description"]
    assert "delegation depth" in tool["description"]


def test_sanitized_registered_schema_keeps_output_schema_open(registered_plugin):
    from tools.schema_sanitizer import sanitize_tool_schemas

    registered_schema = registered_plugin.tool["schema"]
    sanitized = sanitize_tool_schemas(
        [{"type": "function", "function": registered_schema}]
    )

    output_schema = sanitized[0]["function"]["parameters"]["properties"][
        "output_schema"
    ]

    assert output_schema["type"] == "object"
    assert output_schema["additionalProperties"] is True


# ---- Public handler validation ---------------------------------------------


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


def test_handler_normalizes_name_and_deduplicates_selection(
    registered_plugin, stub_delegation
):
    calls = stub_delegation(registered_plugin.module)
    registered_plugin.context.skill_payloads["review-skill"] = {
        "success": True,
        "content": "Review rules",
    }
    parent = SimpleNamespace(
        session_id="parent-session",
        enabled_toolsets=["delegation", "file"],
    )

    payload = json.loads(
        registered_plugin.handler(
            {
                "name": "  Relay reviewer  ",
                "goal": "Review",
                "skills": ["review-skill", "review-skill"],
                "toolsets": ["file", "file"],
            },
            parent_agent=parent,
        )
    )

    assert payload["success"] is True
    assert payload["name"] == "Relay reviewer"
    assert payload["skills"] == ["review-skill"]
    assert payload["toolsets"] == ["file"]
    assert len(calls) == 1
    request = registered_plugin.context.subagent_lifecycle.requests[0]
    assert request.allowed_toolsets == ("file",)
    assert request.metadata["requested_skills"] == ["review-skill"]


def test_rejects_invalid_names_and_oversized_lists(registered_plugin):
    invalid = json.loads(
        registered_plugin.handler(
            {"name": "Reviewer", "goal": "Review", "skills": ["../secret"]}
        )
    )
    oversized = json.loads(
        registered_plugin.handler(
            {
                "name": "Reviewer",
                "goal": "Review",
                "skills": [f"skill-{index}" for index in range(9)],
            }
        )
    )

    assert invalid["success"] is False
    assert "skills" in invalid["error"]
    assert oversized["success"] is False
    assert "at most" in oversized["error"]


def test_handler_rejects_format_characters(registered_plugin):
    payload = json.loads(
        registered_plugin.handler({"name": "**format injection**", "goal": "Review"})
    )

    assert payload["success"] is False
    assert "name" in payload["error"]


# ---- Launch acknowledgement ------------------------------------------------


def test_optional_model_is_passed_to_native_lifecycle(
    registered_plugin, stub_delegation
):
    calls = stub_delegation(registered_plugin.module)
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


def test_omitted_model_keeps_native_parent_inheritance(
    registered_plugin, stub_delegation
):
    calls = stub_delegation(registered_plugin.module)
    payload = json.loads(
        registered_plugin.handler({"name": "Reviewer", "goal": "Review"})
    )

    assert payload["success"] is True
    request = registered_plugin.context.subagent_lifecycle.requests[0]
    assert request.model is None
    assert request.metadata["requested_model"] is None
    assert calls[0]["model"] is None


def test_launch_reports_derived_effective_role_from_hermes_handle(
    registered_plugin, stub_delegation, monkeypatch
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
    stub_delegation(registered_plugin.module)

    payload = json.loads(
        registered_plugin.handler({"name": "Reviewer", "goal": "Review"})
    )

    assert payload["success"] is True
    assert payload["effective_role"] == "leaf"
    assert payload["depth"] == 1
    assert "requested_role" not in payload
    assert "from the spawn depth" in payload["note"]


def test_dispatch_rejection_never_launches_child(registered_plugin, stub_delegation):
    stub_delegation(
        registered_plugin.module,
        response={"status": "rejected", "error": "capacity"},
    )

    payload = json.loads(
        registered_plugin.handler({"name": "Reviewer", "goal": "Review"})
    )

    assert payload["success"] is False
    assert "Reviewer" in payload["error"]
    assert registered_plugin.context.subagent_lifecycle.requests == []


def test_dispatch_setup_exception_never_launches_child(
    registered_plugin, stub_delegation
):
    stub_delegation(
        registered_plugin.module,
        exception=RuntimeError("queue offline"),
    )
    payload = json.loads(
        registered_plugin.handler({"name": "Reviewer", "goal": "Review"})
    )

    assert payload["success"] is False
    assert "Reviewer" in payload["error"]
    assert registered_plugin.context.subagent_lifecycle.requests == []


def test_named_errors_cover_skill_toolset_and_launch_failures(
    registered_plugin, stub_delegation, monkeypatch
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
    stub_delegation(registered_plugin.module)
    launch_error = json.loads(
        registered_plugin.handler({"name": "Named reviewer", "goal": "Review"})
    )
    assert "Named reviewer" in launch_error["error"]


# ---- Skills and output contract --------------------------------------------


def test_launch_loads_deduplicated_skills_and_passes_metadata(
    plugin_package, plugin_module, stub_delegation
):
    context = FakeContext({"code-review": {"success": True, "content": "REVIEW RULES"}})
    plugin_package.register(context)
    parent = SimpleNamespace(
        session_id="parent-session", enabled_toolsets=["delegation", "file"]
    )
    stub_delegation(plugin_module)

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


def test_skill_content_is_framed_in_lifecycle_request(
    plugin_package, plugin_module, stub_delegation
):
    context = FakeContext(
        {
            "alpha": {"success": True, "content": "content:alpha"},
            "beta": {"success": True, "content": "content:beta"},
        }
    )
    plugin_package.register(context)
    stub_delegation(plugin_module)

    payload = json.loads(
        context.tools[0]["handler"](
            {
                "name": "Skill reviewer",
                "goal": "Review",
                "context": "base",
                "skills": ["alpha", "beta"],
            }
        )
    )

    assert payload["success"] is True
    request = context.subagent_lifecycle.requests[0]
    assert "base" in request.context
    assert "BEGIN EXPLICIT SKILLS" in request.context
    assert "content:alpha" in request.context


def test_skill_view_retry_preserves_session_context(
    plugin_package, plugin_module, stub_delegation
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

    context = FakeContext()
    context.dispatch_tool = dispatch
    plugin_package.register(context)
    stub_delegation(plugin_module)

    payload = json.loads(
        context.tools[0]["handler"](
            {
                "name": "Skill reviewer",
                "goal": "Review",
                "skills": ["alpha"],
            },
            task_id="parent-task",
            session_id="session",
        )
    )

    assert payload["success"] is True
    request = context.subagent_lifecycle.requests[0]
    assert "# Injected skill" in request.context
    assert len(calls) == 2
    assert calls[1][1] == {"session_id": "session"}


def test_fails_loudly_on_malformed_or_oversized_skill_payload(plugin_package):
    def malformed(*_args, **_kwargs):
        return "not-json"

    context = FakeContext()
    context.dispatch_tool = malformed
    plugin_package.register(context)
    malformed_payload = json.loads(
        context.tools[0]["handler"](
            {"name": "Skill reviewer", "goal": "Review", "skills": ["alpha"]}
        )
    )

    def huge(*_args, **_kwargs):
        return json.dumps({"success": True, "content": "x" * 25_000})

    huge_context = FakeContext()
    huge_context.dispatch_tool = huge
    plugin_package.register(huge_context)
    huge_payload = json.loads(
        huge_context.tools[0]["handler"](
            {"name": "Skill reviewer", "goal": "Review", "skills": ["alpha"]}
        )
    )

    assert malformed_payload["success"] is False
    assert "malformed" in malformed_payload["error"]
    assert huge_payload["success"] is False
    assert "limit" in huge_payload["error"]


def test_output_schema_is_appended_to_child_context(registered_plugin, stub_delegation):
    schema = {
        "type": "object",
        "properties": {"finding": {"type": "string"}},
        "required": ["finding"],
    }
    stub_delegation(
        registered_plugin.module,
        response={"status": "dispatched", "delegation_id": "deleg-schema"},
    )

    payload = json.loads(
        registered_plugin.handler(
            {
                "name": "Structured",
                "goal": "Review",
                "output_schema": schema,
            }
        )
    )

    assert payload["success"] is True
    request = registered_plugin.context.subagent_lifecycle.requests[0]
    assert "OUTPUT CONTRACT (machine-validated)" in request.context


def test_invalid_output_schema_fails_before_launch(registered_plugin):
    payload = json.loads(
        registered_plugin.handler(
            {
                "name": "Structured",
                "goal": "Review",
                "output_schema": {"type": "definitely-not-a-json-schema-type"},
            }
        )
    )

    assert payload["success"] is False
    assert "output_schema" in payload["error"]
    assert registered_plugin.context.subagent_lifecycle.requests == []
