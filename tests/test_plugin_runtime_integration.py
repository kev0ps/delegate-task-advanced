import importlib.util
import json
import queue
import threading
import sys
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.subagent_lifecycle import SubagentLaunchRequest, SubagentLifecycleService, SubagentState
from tools.async_delegation import _reset_for_tests
from tools.delegate_tool import list_active_subagents
from tools.process_registry import process_registry

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    name = "delegate_task_advanced_runtime_plugin"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ImmediateChild:
    def __init__(self, **kwargs):
        self._subagent_id = f"sa-plugin-runtime-{uuid.uuid4().hex[:8]}"
        self._delegate_depth = 1
        self._delegate_role = "leaf"
        self._parent_subagent_id = None
        self._parent_session_id = "parent-runtime"
        self._delegate_saved_tool_names = []
        self._credential_pool = None
        self.tool_progress_callback = None
        self.session_id = "child-runtime"
        self.provider = kwargs.get("provider", "inherited-provider")
        self.model = kwargs.get("model", "inherited-model")
        self.enabled_toolsets = list(kwargs.get("enabled_toolsets") or [])
        self.disabled_toolsets = list(kwargs.get("disabled_toolsets") or [])
        self.session_prompt_tokens = 10
        self.session_completion_tokens = 5
        self.session_reasoning_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"

    def get_activity_summary(self):
        return {
            "current_tool": "read_file",
            "api_call_count": 1,
            "max_iterations": 10,
            "last_activity_ts": time.time(),
        }

    def run_conversation(self, user_message, task_id, stream_callback=None):
        return {
            "final_response": "runtime integration result",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

    def close(self):
        pass


class RuntimeContext:
    def __init__(self, parent):
        self.subagent_lifecycle = SubagentLifecycleService(lambda: parent)
        self.tools = []
        self.unload_callbacks = []

    def dispatch_tool(self, name, args, **kwargs):
        raise AssertionError(f"unexpected tool dispatch: {name}")

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)
        return object()

    def on_unload(self, callback):
        self.unload_callbacks.append(callback)


class PluginRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        _reset_for_tests()
        while True:
            try:
                process_registry.completion_queue.get_nowait()
            except queue.Empty:
                break

    def tearDown(self):
        _reset_for_tests()

    def test_plugin_routes_real_lifecycle_result_to_shared_completion_queue(self):
        parent = SimpleNamespace(
            session_id="parent-runtime",
            enabled_toolsets=["delegation", "file"],
            disabled_toolsets=[],
            _delegate_depth=0,
            _current_task_id="parent-task",
            _active_children=[],
            _memory_manager=None,
            model="inherited-model",
            provider="inherited-provider",
            base_url=None,
            api_mode=None,
            api_key=None,
            _touch_activity=lambda *_args, **_kwargs: None,
            session_estimated_cost_usd=0.0,
            session_cost_source="none",
            session_cost_status="unknown",
        )
        plugin = load_plugin()
        ctx = RuntimeContext(parent)
        plugin.register(ctx)
        handler = ctx.tools[0]["handler"]

        with patch(
            "tools.delegate_tool._build_child_preserving_parent_tools",
            return_value=ImmediateChild(),
        ):
            launched = json.loads(
                handler(
                    {
                        "name": "Runtime reviewer",
                        "goal": "Inspect public files",
                        "toolsets": ["file"],
                    },
                    parent_agent=parent,
                )
            )

        self.assertTrue(launched["success"], launched)
        self.assertEqual(launched["name"], "Runtime reviewer")
        self.assertEqual(launched["model"], "inherited-model")
        self.assertEqual(launched["toolsets"], ["file"])
        self.assertTrue(launched["live_transcript"])

        event = process_registry.completion_queue.get(timeout=5)
        self.assertEqual(event["type"], "async_delegation")
        self.assertEqual(event["delegation_id"], launched["delegation_id"])
        self.assertEqual(event["parent_session_id"], "parent-runtime")
        self.assertEqual(event["status"], "completed")
        self.assertIn("Runtime reviewer", event["summary"])
        self.assertEqual(event["toolsets"], ["file"])

        live_text = Path(launched["live_transcript"]).read_text(encoding="utf-8")
        self.assertIn("Runtime reviewer", live_text)
        self.assertIn("end status=completed", live_text)

    def test_public_lifecycle_orchestrator_adds_delegation_beyond_requested_toolsets(self):
        parent = SimpleNamespace(
            session_id="parent-runtime",
            enabled_toolsets=["delegation", "file"],
            disabled_toolsets=[],
            _delegate_depth=0,
            _current_task_id="parent-task",
            _active_children=[],
            _memory_manager=None,
            model="inherited-model",
            provider="inherited-provider",
            base_url=None,
            api_mode=None,
            api_key=None,
            _touch_activity=lambda *_args, **_kwargs: None,
            session_estimated_cost_usd=0.0,
            session_cost_source="none",
            session_cost_status="unknown",
        )
        plugin = load_plugin()
        ctx = RuntimeContext(parent)
        plugin.register(ctx)
        handler = ctx.tools[0]["handler"]
        children = []

        def build_child(**kwargs):
            child = ImmediateChild(**kwargs)
            children.append(child)
            return child

        with (
            patch("run_agent.AIAgent", side_effect=build_child),
            patch("tools.delegate_tool._get_max_spawn_depth", return_value=2),
            patch("tools.delegate_tool._get_orchestrator_enabled", return_value=True),
        ):
            launched = json.loads(handler({
                "name": "Configured orchestrator",
                "goal": "Coordinate workers",
                "toolsets": ["file"],
            }, parent_agent=parent))

        self.assertTrue(launched["success"], launched)
        self.assertEqual(launched["effective_role"], "orchestrator")
        self.assertEqual(launched["depth"], 1)
        self.assertEqual(len(children), 1)
        self.assertIn("file", children[0].enabled_toolsets)
        self.assertIn("delegation", children[0].enabled_toolsets)
        process_registry.completion_queue.get(timeout=5)

class FakeChild:
    def __init__(self, started, release):
        self._subagent_id = "sa-0-integration"
        self._delegate_depth = 1
        self._delegate_role = "leaf"
        self._parent_subagent_id = None
        self._parent_session_id = "parent-session"
        self._delegate_saved_tool_names = []
        self._credential_pool = None
        self.tool_progress_callback = None
        self.session_id = "child-session"
        self.provider = "inherited-provider"
        self.model = "inherited-model"
        self.session_prompt_tokens = 10
        self.session_completion_tokens = 5
        self.session_reasoning_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self._started = started
        self._release = release
        self.closed = False

    def get_activity_summary(self):
        return {
            "current_tool": "read_file",
            "api_call_count": 1,
            "max_iterations": 10,
            "last_activity_ts": time.time(),
        }

    def run_conversation(self, user_message, task_id, stream_callback=None):
        self._started.set()
        if not self._release.wait(5):
            raise TimeoutError("test child was not released")
        return {
            "final_response": "integration result",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

    def close(self):
        self.closed = True


class LifecycleRegistryIntegrationTests(unittest.TestCase):
    def test_public_lifecycle_reuses_shared_active_registry_and_result_path(self):
        started = threading.Event()
        release = threading.Event()
        child = FakeChild(started, release)
        parent = SimpleNamespace(
            session_id="parent-session",
            enabled_toolsets=["file"],
            disabled_toolsets=[],
            _delegate_depth=0,
            _current_task_id="parent-task",
            _active_children=[],
            _memory_manager=None,
            model="inherited-model",
            provider="inherited-provider",
            base_url=None,
            api_mode=None,
            api_key=None,
            _touch_activity=lambda *_args, **_kwargs: None,
            session_estimated_cost_usd=0.0,
            session_cost_source="none",
            session_cost_status="unknown",
        )
        service = SubagentLifecycleService(lambda: parent)
        try:
            with patch(
                "tools.delegate_tool._build_child_preserving_parent_tools",
                return_value=child,
            ):
                handle = service.launch(
                    SubagentLaunchRequest(
                        goal="Subagent 'Integration reviewer' — inspect public files",
                        allowed_toolsets=("file",),
                        parent_session_id="parent-session",
                        correlation_id="integration-correlation",
                        metadata={"display_name": "Integration reviewer"},
                    )
                )
            self.assertTrue(started.wait(3))
            active = list_active_subagents()
            self.assertTrue(any(row["subagent_id"] == handle.subagent_id for row in active))
            self.assertTrue(any("Integration reviewer" in row["goal"] for row in active))
            release.set()
            terminal = service.wait(handle, timeout_seconds=5)
            self.assertTrue(terminal.completed)
            result = service.result(handle)
            self.assertEqual(result.terminal_state, SubagentState.SUCCEEDED)
            self.assertEqual(result.summary, "integration result")
            self.assertFalse(any(row["subagent_id"] == handle.subagent_id for row in list_active_subagents()))
            self.assertTrue(child.closed)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
