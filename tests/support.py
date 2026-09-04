from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

from agent.subagent_lifecycle import SubagentLifecycleService


class FakeLifecycle:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.cancelled: list[tuple[Any, str]] = []

    def launch(self, request: Any) -> Any:
        self.requests.append(request)
        return SimpleNamespace(
            subagent_id="sa-test-1234",
            parent_session_id="parent-session",
            correlation_id=request.correlation_id,
            model=getattr(request, "model", None) or "inherited-model",
            role="orchestrator",
            depth=1,
        )

    def wait(self, handle: Any, timeout_seconds: float | None = None) -> Any:
        return SimpleNamespace(completed=True, state=SimpleNamespace(value="SUCCEEDED"))

    def result(self, handle: Any) -> Any:
        return SimpleNamespace(
            terminal_state=SimpleNamespace(value="SUCCEEDED"),
            summary="review complete",
            error_message=None,
            usage_metadata={"api_calls": 2},
            tool_execution_summary={"duration_seconds": 1.25},
        )

    def cancel(self, handle: Any, reason: str) -> Any:
        self.cancelled.append((handle, reason))
        return SimpleNamespace(accepted=True)


class FakeContext:
    def __init__(self, skill_payloads: dict[str, Any] | None = None) -> None:
        self.subagent_lifecycle = FakeLifecycle()
        self.skill_payloads = skill_payloads or {}
        self.tools: list[dict[str, Any]] = []
        self.unload_callbacks: list[Any] = []

    def dispatch_tool(self, name: str, args: dict[str, Any], **kwargs: Any) -> str:
        assert name == "skill_view"
        return json.dumps(
            self.skill_payloads.get(
                args["name"],
                {"success": False, "error": f"Skill '{args['name']}' not found"},
            )
        )

    def register_tool(self, **kwargs: Any) -> object:
        self.tools.append(kwargs)
        return object()

    def on_unload(self, callback: Any) -> None:
        self.unload_callbacks.append(callback)


class RuntimeContext:
    def __init__(self, parent: Any) -> None:
        self.subagent_lifecycle = SubagentLifecycleService(lambda: parent)
        self.tools: list[dict[str, Any]] = []
        self.unload_callbacks: list[Any] = []

    def dispatch_tool(self, name: str, args: dict[str, Any], **kwargs: Any) -> str:
        raise AssertionError(f"unexpected tool dispatch: {name}")

    def register_tool(self, **kwargs: Any) -> object:
        self.tools.append(kwargs)
        return object()

    def on_unload(self, callback: Any) -> None:
        self.unload_callbacks.append(callback)


class ImmediateChild:
    def __init__(self, **kwargs: Any) -> None:
        self._subagent_id = f"sa-plugin-runtime-{uuid.uuid4().hex[:8]}"
        self._delegate_depth = 1
        self._delegate_role = "leaf"
        self._parent_subagent_id = None
        self._parent_session_id = "parent-runtime"
        self._delegate_saved_tool_names: list[str] = []
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

    def get_activity_summary(self) -> dict[str, Any]:
        return {
            "current_tool": "read_file",
            "api_call_count": 1,
            "max_iterations": 10,
            "last_activity_ts": time.time(),
        }

    def run_conversation(
        self, user_message: str, task_id: str, stream_callback: Any = None
    ) -> dict[str, Any]:
        return {
            "final_response": "runtime integration result",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

    def close(self) -> None:
        pass


class BlockingChild:
    def __init__(self, started: Any, release: Any) -> None:
        self._subagent_id = "sa-0-integration"
        self._delegate_depth = 1
        self._delegate_role = "leaf"
        self._parent_subagent_id = None
        self._parent_session_id = "parent-session"
        self._delegate_saved_tool_names: list[str] = []
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

    def get_activity_summary(self) -> dict[str, Any]:
        return {
            "current_tool": "read_file",
            "api_call_count": 1,
            "max_iterations": 10,
            "last_activity_ts": time.time(),
        }

    def run_conversation(
        self, user_message: str, task_id: str, stream_callback: Any = None
    ) -> dict[str, Any]:
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

    def close(self) -> None:
        self.closed = True


def make_parent(
    *,
    session_id: str = "parent-runtime",
    enabled_toolsets: list[str] | None = None,
) -> Any:
    return SimpleNamespace(
        session_id=session_id,
        enabled_toolsets=enabled_toolsets or ["delegation", "file"],
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
