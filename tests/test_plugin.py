import copy
import importlib.util
import inspect
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    name = "delegate_task_advanced_plugin"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeLifecycle:
    def __init__(self):
        self.requests = []
        self.cancelled = []

    def launch(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            subagent_id="sa-test-1234",
            parent_session_id="parent-session",
            correlation_id=request.correlation_id,
            model=getattr(request, "model", None) or "inherited-model",
            role="orchestrator",
            depth=1,
        )

    def wait(self, handle, timeout_seconds=None):
        return SimpleNamespace(completed=True, state=SimpleNamespace(value="SUCCEEDED"))

    def result(self, handle):
        return SimpleNamespace(
            terminal_state=SimpleNamespace(value="SUCCEEDED"),
            summary="review complete",
            error_message=None,
            usage_metadata={"api_calls": 2},
            tool_execution_summary={"duration_seconds": 1.25},
        )

    def cancel(self, handle, reason):
        self.cancelled.append((handle, reason))
        return SimpleNamespace(accepted=True)


class FakeContext:
    def __init__(self, skill_payloads=None):
        self.subagent_lifecycle = FakeLifecycle()
        self.skill_payloads = skill_payloads or {}
        self.tools = []
        self.unload_callbacks = []

    def dispatch_tool(self, name, args, **kwargs):
        assert name == "skill_view"
        return json.dumps(self.skill_payloads.get(args["name"], {
            "success": False,
            "error": f"Skill '{args['name']}' not found",
        }))

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)
        return object()

    def on_unload(self, callback):
        self.unload_callbacks.append(callback)


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.plugin_module = sys.modules[f"{self.plugin.__name__}.plugin"]

    def test_register_adds_advanced_tool_without_touching_native_delegation(self):
        ctx = FakeContext()
        from toolsets import TOOLSETS
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA, delegate_task

        original_toolsets = copy.deepcopy(TOOLSETS)
        original_delegate_toolset = dict(TOOLSETS["delegation"])
        original_delegate_schema = copy.deepcopy(DELEGATE_TASK_SCHEMA)
        original_delegate_handler = delegate_task
        self.plugin.register(ctx)
        self.assertEqual([t["name"] for t in ctx.tools], ["delegate_task_advanced"])
        self.assertEqual(ctx.tools[0]["toolset"], "delegation")
        self.assertEqual(TOOLSETS, original_toolsets)
        self.assertEqual(TOOLSETS["delegation"], original_delegate_toolset)
        self.assertEqual(DELEGATE_TASK_SCHEMA, original_delegate_schema)
        self.assertIs(delegate_task, original_delegate_handler)
        self.assertEqual(ctx.unload_callbacks, [])

    def test_package_init_exports_only_the_public_register_hook(self):
        source = inspect.getsource(self.plugin)
        self.assertIn("def register(", source)
        self.assertNotIn("def make_handler(", source)
        self.assertNotIn("SubagentLaunchRequest", source)

    def test_schema_omits_provider_tuning_and_batch_fields(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        params = ctx.tools[0]["schema"]["parameters"]
        self.assertFalse(params["additionalProperties"])
        self.assertEqual(set(params["required"]), {"name", "goal"})
        self.assertIn("model", params["properties"])
        self.assertNotIn("provider", params["properties"])
        self.assertNotIn("reasoning_effort", params["properties"])
        self.assertNotIn("role", params["properties"])
        self.assertNotIn("tasks", params["properties"])
        self.assertIn("output_schema", params["properties"])
        description = ctx.tools[0]["schema"]["description"]
        self.assertIn("Hermes decides the subagent role", description)

    def test_role_field_is_rejected_as_unknown_before_launch(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        for role in ("leaf", "orchestrator", None):
            with self.subTest(role=role):
                payload = json.loads(handler({
                    "name": "Reviewer", "goal": "Review", "role": role
                }))
                self.assertFalse(payload["success"])
                self.assertIn("Unknown fields: role", payload["error"])
        self.assertEqual(ctx.subagent_lifecycle.requests, [])

    def test_schema_describes_selection_and_security_boundaries(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        tool = ctx.tools[0]
        description = tool["schema"]["description"]
        properties = tool["schema"]["parameters"]["properties"]

        for phrase in (
            "one named Hermes subagent",
            "fresh conversation",
            "returns immediately",
        ):
            self.assertIn(phrase, description)
        toolsets_description = properties["toolsets"]["description"]
        self.assertIn("not individual tool names", toolsets_description)
        self.assertIn("do not imply read-only access", toolsets_description)
        model_description = properties["model"]["description"]
        self.assertIn("cannot switch providers", model_description)

        self.assertIn("not the generated subagent_id", properties["name"]["description"])
        self.assertIn("cannot see the parent conversation", properties["goal"]["description"])
        self.assertIn("grant no permissions", properties["skills"]["description"])
        self.assertIn("not individual tool names", properties["toolsets"]["description"])
        self.assertIn("do not imply read-only access", properties["toolsets"]["description"])
        self.assertIn("cannot switch providers", properties["model"]["description"])
        self.assertIn("one named Hermes subagent", tool["description"])
        self.assertIn("delegation depth", tool["description"])

    def test_batch_tasks_field_is_rejected(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        payload = json.loads(handler({
            "tasks": [
                {"name": "Security", "goal": "Analyze security risks"},
                {"name": "Logic", "goal": "Analyze logic regressions"},
            ],
        }))

        self.assertFalse(payload["success"])
        self.assertIn("Unknown fields: tasks", payload["error"])
        self.assertEqual(ctx.subagent_lifecycle.requests, [])

    def test_output_schema_is_validated_and_appended_to_child_context(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        schema = {
            "type": "object",
            "properties": {"finding": {"type": "string"}},
            "required": ["finding"],
        }
        with patch.object(self.plugin_module, "dispatch_completion_watcher", return_value={
            "status": "dispatched", "delegation_id": "deleg-schema"
        }):
            payload = json.loads(ctx.tools[0]["handler"]({
                "name": "Structured", "goal": "Review", "output_schema": schema
            }))

        self.assertTrue(payload["success"])
        request = ctx.subagent_lifecycle.requests[0]
        self.assertIn("OUTPUT CONTRACT (machine-validated)", request.context)
        result = self.plugin_module.result_payload(
            ctx.subagent_lifecycle,
            ctx.subagent_lifecycle.launch(request),
            "Structured",
            schema,
        )
        self.assertFalse(result["schema_valid"])
        self.assertTrue(result["schema_errors"])

    def test_invalid_output_schema_fails_before_launch(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        payload = json.loads(ctx.tools[0]["handler"]({
            "name": "Structured",
            "goal": "Review",
            "output_schema": {"type": "definitely-not-a-json-schema-type"},
        }))
        self.assertFalse(payload["success"])
        self.assertIn("output_schema", payload["error"])
        self.assertEqual(ctx.subagent_lifecycle.requests, [])

    def test_invalid_structured_result_gets_one_bounded_correction_retry(self):
        from agent.subagent_lifecycle import SubagentLaunchRequest

        lifecycle = FakeLifecycle()
        schema = {
            "type": "object",
            "properties": {"finding": {"type": "string"}},
            "required": ["finding"],
        }
        request = SubagentLaunchRequest(
            goal="Return structured finding",
            context="OUTPUT CONTRACT (machine-validated)",
            correlation_id="initial",
            metadata={},
        )
        state = self.plugin_module.DeferredLaunch(lifecycle, "Structured", schema)
        state.retry_request = request
        state.set_handle(lifecycle.launch(request))

        result = state.run()

        self.assertEqual(len(lifecycle.requests), 2)
        self.assertEqual(result["schema_retries"], 1)
        self.assertFalse(result["schema_valid"])
        self.assertTrue(result["schema_errors"])
        self.assertIn("Correct the previous final response", lifecycle.requests[1].goal)

    def test_failed_correction_launch_preserves_first_validation_failure(self):
        from agent.subagent_lifecycle import SubagentLaunchRequest

        lifecycle = FakeLifecycle()
        request = SubagentLaunchRequest(
            goal="Return structured finding",
            context="OUTPUT CONTRACT (machine-validated)",
            correlation_id="initial",
            metadata={},
        )
        initial_handle = lifecycle.launch(request)
        lifecycle.launch = lambda _request: (_ for _ in ()).throw(
            RuntimeError("retry launch failed")
        )
        state = self.plugin_module.DeferredLaunch(
            lifecycle,
            "Structured",
            {"type": "object", "required": ["finding"]},
        )
        state.retry_request = request
        state.set_handle(initial_handle)

        result = state.run()

        self.assertEqual(result["status"], "error")
        self.assertFalse(result["schema_valid"])
        self.assertTrue(result["schema_errors"])
        self.assertEqual(result["schema_retries"], 1)
        self.assertIn("retry launch failed", result["schema_retry_error"])
        self.assertEqual(result["exit_reason"], "output_schema_invalid")
        self.assertIn("output_schema validation failed", result["error"])

    def test_optional_model_is_passed_to_native_lifecycle(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        parent = SimpleNamespace(
            session_id="parent-session",
            enabled_toolsets=["delegation", "file"],
        )
        with patch.object(self.plugin_module, "dispatch_completion_watcher", return_value={
            "status": "dispatched", "delegation_id": "deleg-test"
        }) as watcher:
            payload = json.loads(handler({
                "name": "Luna reviewer",
                "goal": "Review the public diff",
                "model": "gpt-5.6-luna",
                "toolsets": ["file"],
            }, parent_agent=parent))

        self.assertTrue(payload["success"])
        self.assertEqual(payload["model"], "gpt-5.6-luna")
        request = ctx.subagent_lifecycle.requests[0]
        self.assertEqual(request.model, "gpt-5.6-luna")
        self.assertEqual(request.metadata["requested_model"], "gpt-5.6-luna")
        watcher.assert_called_once()
        self.assertEqual(watcher.call_args.kwargs["model"], "gpt-5.6-luna")

    def test_omitted_model_keeps_native_parent_inheritance(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        with patch.object(self.plugin_module, "dispatch_completion_watcher", return_value={
            "status": "dispatched", "delegation_id": "deleg-test"
        }) as watcher:
            payload = json.loads(handler({"name": "Reviewer", "goal": "Review"}))

        self.assertTrue(payload["success"])
        request = ctx.subagent_lifecycle.requests[0]
        self.assertIsNone(request.model)
        self.assertIsNone(request.metadata["requested_model"])
        self.assertIsNone(watcher.call_args.kwargs["model"])

    def test_rejects_empty_or_malformed_model_before_launch(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        for model in ("", " ", "bad model", "bad\nmodel"):
            with self.subTest(model=model):
                payload = json.loads(handler({
                    "name": "Reviewer", "goal": "Review", "model": model
                }))
                self.assertFalse(payload["success"])
                self.assertIn("model", payload["error"])
        self.assertEqual(ctx.subagent_lifecycle.requests, [])

    def test_launch_loads_deduplicated_skills_and_passes_metadata(self):
        ctx = FakeContext({
            "code-review": {"success": True, "content": "REVIEW RULES"},
        })
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        parent = SimpleNamespace(
            session_id="parent-session",
            enabled_toolsets=["delegation", "file"],
        )
        with patch.object(self.plugin_module, "dispatch_completion_watcher", return_value={
            "status": "dispatched", "delegation_id": "deleg-test"
        }):
            payload = json.loads(handler({
                "name": "Relay reviewer",
                "goal": "Review the public diff",
                "context": "Public repository",
                "skills": ["code-review", "code-review"],
                "toolsets": ["file"],
            }, parent_agent=parent, task_id="parent-task"))

        self.assertTrue(payload["success"])
        self.assertEqual(payload["subagent_id"], "sa-test-1234")
        self.assertEqual(payload["depth"], 1)
        request = ctx.subagent_lifecycle.requests[0]
        self.assertIsNone(request.model)
        self.assertEqual(request.allowed_toolsets, ("file",))
        self.assertIn("REVIEW RULES", request.context)
        self.assertEqual(request.metadata["display_name"], "Relay reviewer")
        self.assertEqual(request.metadata["requested_skills"], ["code-review"])

    def test_launch_reports_derived_effective_role_from_hermes_handle(self):
        ctx = FakeContext()
        ctx.subagent_lifecycle.launch = lambda request: SimpleNamespace(
            subagent_id="sa-derived",
            model=request.model or "inherited-model",
            role="leaf",
            depth=1,
        )
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        with patch.object(self.plugin_module, "dispatch_completion_watcher", return_value={
            "status": "dispatched", "delegation_id": "deleg-test"
        }):
            payload = json.loads(handler({"name": "Reviewer", "goal": "Review"}))

        self.assertTrue(payload["success"])
        self.assertEqual(payload["effective_role"], "leaf")
        self.assertEqual(payload["depth"], 1)
        self.assertNotIn("requested_role", payload)
        self.assertIn("from the spawn depth", payload["note"])

    def test_rejects_unknown_skill_before_launch(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        payload = json.loads(handler({
            "name": "Reviewer",
            "goal": "Review this",
            "skills": ["missing-skill"],
        }))
        self.assertFalse(payload["success"])
        self.assertIn("missing-skill", payload["error"])
        self.assertEqual(ctx.subagent_lifecycle.requests, [])

    def test_rejects_control_characters_and_unknown_fields(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        bad_name = json.loads(handler({"name": "bad\nname", "goal": "Review"}))
        unknown = json.loads(handler({"name": "Reviewer", "goal": "Review", "provider": "x"}))
        self.assertFalse(bad_name["success"])
        self.assertFalse(unknown["success"])
        self.assertIn("Unknown fields", unknown["error"])

    def test_rejects_toolset_not_enabled_for_parent(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        parent = SimpleNamespace(session_id="parent-session", enabled_toolsets=["delegation"])
        payload = json.loads(handler({
            "name": "Reviewer",
            "goal": "Review",
            "toolsets": ["file"],
        }, parent_agent=parent))
        self.assertFalse(payload["success"])
        self.assertIn("parent", payload["error"].lower())
        self.assertEqual(ctx.subagent_lifecycle.requests, [])

    def test_watcher_rejection_never_launches_child(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        with patch.object(self.plugin_module, "dispatch_completion_watcher", return_value={
            "status": "rejected", "error": "capacity"
        }):
            payload = json.loads(handler({"name": "Reviewer", "goal": "Review"}))
        self.assertFalse(payload["success"])
        self.assertIn("Reviewer", payload["error"])
        self.assertEqual(ctx.subagent_lifecycle.requests, [])

    def test_watcher_setup_exception_never_launches_child(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        with patch.object(
            self.plugin_module,
            "dispatch_completion_watcher",
            side_effect=RuntimeError("queue offline"),
        ):
            payload = json.loads(handler({"name": "Reviewer", "goal": "Review"}))
        self.assertFalse(payload["success"])
        self.assertIn("Reviewer", payload["error"])
        self.assertEqual(ctx.subagent_lifecycle.requests, [])

    def test_named_errors_cover_skill_toolset_and_launch_failures(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        skill_error = json.loads(handler({
            "name": "Named reviewer", "goal": "Review", "skills": ["missing-skill"]
        }))
        self.assertIn("Named reviewer", skill_error["error"])

        parent = SimpleNamespace(session_id="parent-session", enabled_toolsets=["delegation"])
        toolset_error = json.loads(handler({
            "name": "Named reviewer", "goal": "Review", "toolsets": ["file_readonly"]
        }, parent_agent=parent))
        self.assertIn("Named reviewer", toolset_error["error"])

        ctx.subagent_lifecycle.launch = lambda _request: (_ for _ in ()).throw(
            RuntimeError("launch exploded")
        )
        with patch.object(self.plugin_module, "dispatch_completion_watcher", return_value={
            "status": "dispatched", "delegation_id": "deleg-test"
        }):
            launch_error = json.loads(handler({"name": "Named reviewer", "goal": "Review"}))
        self.assertIn("Named reviewer", launch_error["error"])

    def test_cancellation_before_handle_does_not_orphan_later_launch(self):
        lifecycle = FakeLifecycle()
        state = self.plugin_module.DeferredLaunch(lifecycle, "Reviewer")
        state.cancel()
        self.assertTrue(state.cancel_requested)
        self.assertIsNone(state.launch_error)
        handle = lifecycle.launch(SimpleNamespace(correlation_id="corr"))
        state.set_handle(handle)
        result = state.run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(lifecycle.cancelled), 1)

    def test_explicit_empty_toolsets_is_rejected(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        payload = json.loads(ctx.tools[0]["handler"]({
            "name": "Reviewer", "goal": "Review", "toolsets": []
        }))
        self.assertFalse(payload["success"])
        self.assertIn("toolsets", payload["error"])

    def test_source_does_not_define_custom_file_readonly_toolset(self):
        self.assertNotIn(
            "file_readonly", inspect.getsource(self.plugin_module)
        )

    def test_async_lifecycle_exception_keeps_human_name(self):
        lifecycle = FakeLifecycle()
        lifecycle.wait = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("wait exploded")
        )
        state = self.plugin_module.DeferredLaunch(lifecycle, "Named reviewer")
        handle = lifecycle.launch(SimpleNamespace(correlation_id="corr"))
        state.set_handle(handle)
        result = state.run()
        self.assertEqual(result["status"], "error")
        self.assertIn("Named reviewer", result["error"])
        self.assertIn("wait exploded", result["error"])

    def test_routing_context_does_not_use_private_async_api(self):
        source = inspect.getsource(self.plugin_module.routing_context)
        self.assertNotIn("_current_origin_session_id", source)



class PluginModuleMixin(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.plugin_module = sys.modules[f"{self.plugin.__name__}.plugin"]


class InputValidationTests(PluginModuleMixin):
    def test_normalize_name_list_dedupes_preserving_order(self):
        self.assertEqual(
            self.plugin_module.normalize_name_list(["file_readonly", "file_readonly", "web"], "toolsets"),
            ["file_readonly", "web"],
        )

    def test_rejects_invalid_names_and_oversized_lists(self):
        with self.assertRaisesRegex(ValueError, "skills"):
            self.plugin_module.normalize_name_list(["../secret"], "skills")
        with self.assertRaisesRegex(ValueError, "at most"):
            self.plugin_module.normalize_name_list([f"skill-{i}" for i in range(9)], "skills")

    def test_display_name_is_normalized_and_rejects_format_characters(self):
        self.assertEqual(self.plugin_module.validate_display_name("  Relay reviewer  "), "Relay reviewer")
        with self.assertRaises(ValueError):
            self.plugin_module.validate_display_name("**format injection**")


class SkillInjectionTests(PluginModuleMixin):
    def test_appends_framed_skill_context(self):
        def dispatch(_name, args, **_kwargs):
            return json.dumps({"success": True, "content": f"content:{args['name']}"})

        context, names = self.plugin_module.build_skill_context(
            "base", ["alpha", "beta"], dispatch
        )
        self.assertEqual(names, ["alpha", "beta"])
        self.assertIn("base", context)
        self.assertIn("BEGIN EXPLICIT SKILLS", context)
        self.assertIn("content:alpha", context)

    def test_retries_skill_view_without_task_id_when_parent_dedup_hides_content(self):
        calls = []

        def dispatch(_name, args, **kwargs):
            calls.append((args, kwargs))
            if kwargs.get("task_id") == "parent-task":
                return json.dumps({
                    "success": True,
                    "status": "unchanged",
                    "dedup": True,
                    "content_returned": False,
                })
            return json.dumps({"success": True, "content": "# Injected skill"})

        context, names = self.plugin_module.build_skill_context(
            None,
            ["alpha"],
            dispatch,
            dispatch_kwargs={"task_id": "parent-task", "session_id": "session"},
        )

        self.assertEqual(names, ["alpha"])
        self.assertIn("# Injected skill", context)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1], {"session_id": "session"})

    def test_fails_loudly_on_malformed_or_oversized_skill_payload(self):
        def malformed(*_args, **_kwargs):
            return "not-json"
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.plugin_module.build_skill_context(None, ["alpha"], malformed)

        def huge(*_args, **_kwargs):
            return json.dumps({"success": True, "content": "x" * 25000})
        with self.assertRaisesRegex(ValueError, "limit"):
            self.plugin_module.build_skill_context(None, ["alpha"], huge, max_chars=100)


if __name__ == "__main__":
    unittest.main()
