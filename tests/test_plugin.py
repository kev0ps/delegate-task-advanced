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

    def tearDown(self):
        pass

    def test_registers_only_additive_tool_without_custom_toolset(self):
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

    def test_schema_delegates_orchestration_depth_to_hermes_config(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        params = ctx.tools[0]["schema"]["parameters"]
        self.assertFalse(params["additionalProperties"])
        self.assertEqual(set(params["required"]), {"name", "goal"})
        self.assertIn("model", params["properties"])
        self.assertNotIn("provider", params["properties"])
        self.assertNotIn("reasoning_effort", params["properties"])
        self.assertNotIn("role", params["properties"])
        description = ctx.tools[0]["schema"]["description"]
        self.assertIn("delegation.max_spawn_depth", description)
        self.assertIn("delegation.orchestrator_enabled", description)

    def test_schema_describes_selection_and_security_boundaries(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        tool = ctx.tools[0]
        description = tool["schema"]["description"]
        properties = tool["schema"]["parameters"]["properties"]

        for phrase in (
            "exactly one",
            "requests the orchestrator role",
            "fresh conversation",
            "not individual tool names",
            "do not imply read-only access",
            "cannot switch providers",
            "do not wait or poll",
            "use the native delegate_task",
        ):
            self.assertIn(phrase, description)

        self.assertIn("not the generated subagent_id", properties["name"]["description"])
        self.assertIn("cannot see the parent conversation", properties["goal"]["description"])
        self.assertIn("grant no permissions", properties["skills"]["description"])
        self.assertIn("not individual tool names", properties["toolsets"]["description"])
        self.assertIn("do not imply read-only access", properties["toolsets"]["description"])
        self.assertIn("orchestrator adds delegation", properties["toolsets"]["description"])
        self.assertIn("cannot switch providers", properties["model"]["description"])
        self.assertIn("Hermes-configured spawning depth", tool["description"])
        self.assertIn("use native delegate_task", tool["description"])

    def test_optional_model_is_passed_to_native_lifecycle(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        parent = SimpleNamespace(
            session_id="parent-session",
            enabled_toolsets=["delegation", "file"],
        )
        with patch.object(self.plugin, "_dispatch_completion_watcher", return_value={
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
        self.assertEqual(request.role, "orchestrator")
        self.assertEqual(request.metadata["requested_model"], "gpt-5.6-luna")
        self.assertEqual(request.metadata["requested_role"], "orchestrator")
        watcher.assert_called_once()
        self.assertEqual(watcher.call_args.kwargs["model"], "gpt-5.6-luna")

    def test_omitted_model_keeps_native_parent_inheritance(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        with patch.object(self.plugin, "_dispatch_completion_watcher", return_value={
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
        with patch.object(self.plugin, "_dispatch_completion_watcher", return_value={
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
        self.assertEqual(payload["requested_role"], "orchestrator")
        self.assertEqual(payload["effective_role"], "orchestrator")
        self.assertEqual(payload["depth"], 1)
        request = ctx.subagent_lifecycle.requests[0]
        self.assertEqual(request.role, "orchestrator")
        self.assertIsNone(request.model)
        self.assertEqual(request.allowed_toolsets, ("file",))
        self.assertIn("REVIEW RULES", request.context)
        self.assertEqual(request.metadata["display_name"], "Relay reviewer")
        self.assertEqual(request.metadata["requested_skills"], ["code-review"])

    def test_launch_reports_hermes_role_downgrade_without_reimplementing_depth_logic(self):
        ctx = FakeContext()
        ctx.subagent_lifecycle.launch = lambda request: SimpleNamespace(
            subagent_id="sa-downgraded",
            model=request.model or "inherited-model",
            role="leaf",
            depth=1,
        )
        self.plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        with patch.object(self.plugin, "_dispatch_completion_watcher", return_value={
            "status": "dispatched", "delegation_id": "deleg-test"
        }):
            payload = json.loads(handler({"name": "Reviewer", "goal": "Review"}))

        self.assertTrue(payload["success"])
        self.assertEqual(payload["requested_role"], "orchestrator")
        self.assertEqual(payload["effective_role"], "leaf")
        self.assertEqual(payload["depth"], 1)
        self.assertIn("Hermes configuration", payload["note"])

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
        with patch.object(self.plugin, "_dispatch_completion_watcher", return_value={
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
            self.plugin, "_dispatch_completion_watcher", side_effect=RuntimeError("queue offline")
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
        with patch.object(self.plugin, "_dispatch_completion_watcher", return_value={
            "status": "dispatched", "delegation_id": "deleg-test"
        }):
            launch_error = json.loads(handler({"name": "Named reviewer", "goal": "Review"}))
        self.assertIn("Named reviewer", launch_error["error"])

    def test_cancellation_before_handle_does_not_orphan_later_launch(self):
        lifecycle = FakeLifecycle()
        state = self.plugin._DeferredLaunch(lifecycle, "Reviewer")
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
        source = inspect.getsource(self.plugin)
        self.assertNotIn("file_readonly", source)

    def test_async_lifecycle_exception_keeps_human_name(self):
        lifecycle = FakeLifecycle()
        lifecycle.wait = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("wait exploded")
        )
        state = self.plugin._DeferredLaunch(lifecycle, "Named reviewer")
        handle = lifecycle.launch(SimpleNamespace(correlation_id="corr"))
        state.set_handle(handle)
        result = state.run()
        self.assertEqual(result["status"], "error")
        self.assertIn("Named reviewer", result["error"])
        self.assertIn("wait exploded", result["error"])

    def test_routing_context_does_not_use_private_async_api(self):
        source = inspect.getsource(self.plugin._routing_context)
        self.assertNotIn("_current_origin_session_id", source)


if __name__ == "__main__":
    unittest.main()
