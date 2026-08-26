import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.subagent_lifecycle import (
    SubagentLaunchRequest,
    SubagentLifecycleService,
    SubagentState,
)
from tools.delegate_tool import list_active_subagents


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


class LifecycleIntegrationTests(unittest.TestCase):
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
                        goal="Sous-agent « Integration reviewer » — inspect public files",
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
