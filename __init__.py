"""delegate-task-advanced — a thin adapter over Hermes public subagent lifecycle."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Optional

from .skill_context import build_skill_context
from .validation import (
    normalize_name_list,
    validate_display_name,
    validate_identifier,
    validate_text,
)

_TOOL_NAME = "delegate_task_advanced"
_REQUESTED_ROLE = "orchestrator"
_ALLOWED_FIELDS = frozenset({"name", "goal", "context", "skills", "toolsets", "model"})

_SCHEMA = {
    "name": _TOOL_NAME,
    "description": (
        "Launch exactly one named Hermes subagent in the background. The plugin "
        "requests the orchestrator role; Hermes alone decides the effective role and "
        "spawning depth from delegation.max_spawn_depth and "
        "delegation.orchestrator_enabled. Use this tool when one focused child needs "
        "a display name, explicit skill injection, a selected baseline of parent toolsets, "
        "or a model override within the parent's provider. The child starts with a fresh "
        "conversation, so provide all required paths, constraints, background, and "
        "expected output in goal/context. Skills inject instructions but grant no "
        "permissions. Toolsets are groups, not individual tool names, and do not imply "
        "read-only access. When Hermes grants orchestrator, it adds the delegation "
        "toolset regardless of this baseline selection. Omit toolsets for normal Hermes "
        "inheritance. Omit model to inherit the parent model; a supplied model cannot "
        "switch providers. "
        "The call returns immediately and the result is delivered asynchronously; do "
        "not wait or poll. For batches, output schemas, or list/steer/stop controls, "
        "use the native delegate_task."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
                "description": (
                    "Required human-readable display label. It appears in logs, status, "
                    "and completion messages; it is not the generated subagent_id or a "
                    "control handle."
                ),
            },
            "goal": {
                "type": "string",
                "minLength": 1,
                "maxLength": 16000,
                "description": (
                    "Required self-contained objective. Include the expected deliverable "
                    "and acceptance criteria because the child cannot see the parent conversation."
                ),
            },
            "context": {
                "type": "string",
                "maxLength": 12000,
                "description": (
                    "Optional mission background: paths, relevant facts, constraints, "
                    "expected output format, language, and tone not already stated in goal."
                ),
            },
            "model": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": (
                    "Optional model identifier. Omit to inherit the parent model. A supplied "
                    "value overrides only the model within the parent's provider; it cannot "
                    "switch providers and must be compatible with that provider."
                ),
            },
            "skills": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "description": (
                    "Optional skill names loaded through skill_view and injected into the "
                    "child context before launch. Skills grant no permissions. Missing or "
                    "unloadable skills fail the launch; duplicate names are ignored."
                ),
            },
            "toolsets": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "description": (
                    "Optional baseline selection of existing Hermes toolset names. Omit to "
                    "inherit the parent's normal capabilities. Every requested toolset must "
                    "already be available to the parent. These are toolset names, not "
                    "individual tool names, and they do not imply read-only access. If Hermes "
                    "grants the requested role, orchestrator adds delegation regardless of "
                    "this list so the child can spawn within the globally configured depth. "
                    "An explicit empty list is invalid."
                ),
            },
        },
        "required": ["name", "goal"],
    },
}


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def _named_message(display_name: Optional[str], message: str) -> str:
    if display_name:
        return f"Sous-agent « {display_name} » : {message}"
    return message


def _value(obj: Any) -> str:
    return str(getattr(obj, "value", obj) or "")


def _result_payload(lifecycle: Any, handle: Any, display_name: str) -> dict[str, Any]:
    started = time.monotonic()
    lifecycle.wait(handle, timeout_seconds=None)
    result = lifecycle.result(handle)
    terminal = _value(result.terminal_state).upper()
    status = "completed" if terminal == "SUCCEEDED" else (
        "interrupted" if terminal in {"INTERRUPTED", "CANCELLED"} else "error"
    )
    summary = result.summary
    if summary:
        summary = f"Sous-agent « {display_name} »\n\n{summary}"
    else:
        summary = f"Sous-agent « {display_name} » terminé sans résultat exploitable."
    error = result.error_message
    if error:
        error = _named_message(display_name, str(error))
    usage = result.usage_metadata if isinstance(result.usage_metadata, dict) else {}
    execution = (
        result.tool_execution_summary
        if isinstance(result.tool_execution_summary, dict)
        else {}
    )
    return {
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": usage.get("api_calls", 0),
        "duration_seconds": execution.get(
            "duration_seconds", round(time.monotonic() - started, 2)
        ),
        "model": getattr(handle, "model", None),
        "effective_role": getattr(handle, "role", None),
        "depth": getattr(handle, "depth", None),
        "exit_reason": terminal.lower(),
    }


class _DeferredLaunch:
    """Reserve completion routing before launch, eliminating orphaned children."""

    def __init__(self, lifecycle: Any, display_name: str):
        self.lifecycle = lifecycle
        self.display_name = display_name
        self.handle: Any = None
        self.launch_error: Optional[str] = None
        self.cancel_requested = False
        self.writer: Any = None
        self.live_delegation_id: Optional[str] = None
        self.live_paths: list[str] = []
        self._event = threading.Event()
        self._lock = threading.Lock()

    def set_handle(self, handle: Any) -> None:
        with self._lock:
            self.handle = handle
            cancel_now = self.cancel_requested
        if cancel_now:
            try:
                self.lifecycle.cancel(
                    handle, reason=_named_message(self.display_name, "annulation demandée")
                )
            finally:
                # Even an unsupported cancellation must not strand completion
                # delivery: the watcher waits for the child's eventual result.
                self._event.set()
        else:
            self._event.set()

    def set_launch_error(self, message: str) -> None:
        with self._lock:
            self.launch_error = message
            self._event.set()

    def cancel(self) -> None:
        with self._lock:
            self.cancel_requested = True
            handle = self.handle
        if handle is not None:
            # A refused/unsupported cancellation is not converted into an
            # orphan: the already-reserved watcher keeps waiting for the real
            # terminal lifecycle result.
            self.lifecycle.cancel(
                handle, reason=_named_message(self.display_name, "annulation demandée")
            )

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        self._event.wait()
        if self.launch_error is not None:
            result = {
                "status": "error",
                "summary": _named_message(self.display_name, "échec du lancement."),
                "error": _named_message(self.display_name, self.launch_error),
                "api_calls": 0,
                "duration_seconds": round(time.monotonic() - started, 2),
                "model": None,
                "exit_reason": "launch_error",
            }
        else:
            try:
                result = _result_payload(
                    self.lifecycle, self.handle, self.display_name
                )
            except Exception as exc:
                result = {
                    "status": "error",
                    "summary": _named_message(
                        self.display_name, "échec du suivi du lifecycle."
                    ),
                    "error": _named_message(self.display_name, str(exc)),
                    "api_calls": 0,
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "model": getattr(self.handle, "model", None),
                    "exit_reason": "lifecycle_error",
                }
        if self.writer is not None:
            self.writer.finalize(result)
        if self.live_delegation_id:
            try:
                from tools.delegation_live_log import update_manifest_statuses

                update_manifest_statuses(
                    self.live_delegation_id, [{"task_index": 0, **result}]
                )
            except Exception:
                pass
        return result


def _routing_context() -> tuple[str, str, str]:
    session_key = ""
    origin_ui_session_id = ""
    origin_session_id = ""
    try:
        from tools.approval import get_current_session_key

        session_key = get_current_session_key() or ""
    except Exception:
        pass
    try:
        from gateway.session_context import get_session_env

        origin_ui_session_id = get_session_env("HERMES_UI_SESSION_ID", "") or ""
    except Exception:
        pass
    # There is no public plugin API for the raw api_server origin session.
    # Leave it empty rather than importing async_delegation internals; the
    # public session_key/UI routing fields remain authoritative.
    return session_key, origin_ui_session_id, origin_session_id


def _dispatch_completion_watcher(
    *, state: _DeferredLaunch, display_name: str, goal: str,
    context: Optional[str], toolsets: Optional[list[str]], model: Optional[str],
    parent_session_id: Optional[str],
) -> dict[str, Any]:
    from tools.async_delegation import dispatch_async_delegation
    from tools.delegation_live_log import create_live_transcripts

    session_key, origin_ui_session_id, origin_session_id = _routing_context()
    result = dispatch_async_delegation(
        goal=f"Sous-agent « {display_name} » — {goal}",
        context=context,
        toolsets=toolsets,
        role=_REQUESTED_ROLE,
        model=model,
        session_key=session_key,
        parent_session_id=parent_session_id,
        runner=state.run,
        origin_ui_session_id=origin_ui_session_id,
        origin_session_id=origin_session_id,
        interrupt_fn=state.cancel,
    )
    if result.get("status") == "dispatched":
        live_id, writers, paths = create_live_transcripts(
            [{"goal": f"Sous-agent « {display_name} » — {goal}", "context": context}],
            context,
            delegation_id=result.get("delegation_id"),
        )
        state.live_delegation_id = live_id
        state.writer = writers[0] if writers else None
        state.live_paths = paths
        if paths:
            result["live_transcript"] = paths[0]
    return result


def _active_parent(explicit_parent: Any) -> Any:
    if explicit_parent is not None:
        return explicit_parent
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent

        return get_active_subagent_parent()
    except Exception:
        return None


def _validate_toolsets(toolsets: list[str], parent_agent: Any) -> None:
    if not toolsets:
        return
    from toolsets import get_toolset

    unknown = [name for name in toolsets if get_toolset(name) is None]
    if unknown:
        raise ValueError(f"Unknown toolsets: {', '.join(sorted(unknown))}.")
    enabled = getattr(parent_agent, "enabled_toolsets", None)
    if enabled is not None and not set(toolsets).issubset(set(enabled)):
        raise ValueError(
            "Requested toolsets would broaden parent permissions. Enable the "
            "narrow toolset for the parent platform before selecting it."
        )


def _make_handler(ctx):
    def handle(params: dict, **kwargs: Any) -> str:
        display_name: Optional[str] = None
        state: Optional[_DeferredLaunch] = None
        if not isinstance(params, dict):
            return _error("Tool input must be an object.")
        unknown = sorted(set(params) - _ALLOWED_FIELDS)
        if unknown:
            return _error(f"Unknown fields: {', '.join(unknown)}.")
        try:
            display_name = validate_display_name(params.get("name"))
            goal = validate_text(params.get("goal"), "goal", required=True, max_chars=16000)
            context = validate_text(
                params.get("context"), "context", required=False, max_chars=12000
            )
            model = validate_identifier(params.get("model"), "model")
            skills = normalize_name_list(params.get("skills"), "skills")
            if "toolsets" in params and params.get("toolsets") == []:
                raise ValueError(
                    "toolsets cannot be an explicit empty list; omit it to inherit Hermes capabilities."
                )
            toolsets = normalize_name_list(params.get("toolsets"), "toolsets")
            parent_agent = _active_parent(kwargs.get("parent_agent"))
            _validate_toolsets(toolsets, parent_agent)
            dispatch_kwargs = {
                key: kwargs[key]
                for key in ("task_id", "session_id")
                if kwargs.get(key) is not None
            }
            enriched_context, loaded_skills = build_skill_context(
                context,
                skills,
                ctx.dispatch_tool,
                dispatch_kwargs=dispatch_kwargs,
            )

            from agent.subagent_lifecycle import SubagentLaunchRequest

            correlation_id = f"dta-{uuid.uuid4().hex}"
            parent_session_id = (
                str(getattr(parent_agent, "session_id", "") or "") or None
            )
            metadata = {
                "source_tool": _TOOL_NAME,
                "display_name": display_name,
                "requested_skills": loaded_skills,
                "requested_toolsets": toolsets,
                "requested_model": model,
                "requested_role": _REQUESTED_ROLE,
                "parent_session_id": parent_session_id,
                "correlation_id": correlation_id,
            }
            request = SubagentLaunchRequest(
                goal=f"Sous-agent « {display_name} » — {goal}",
                context=enriched_context,
                role=_REQUESTED_ROLE,
                model=model,
                allowed_toolsets=tuple(toolsets) if toolsets else None,
                parent_session_id=parent_session_id,
                correlation_id=correlation_id,
                metadata=metadata,
            )
            lifecycle = ctx.subagent_lifecycle
            state = _DeferredLaunch(lifecycle, display_name)
            dispatch = _dispatch_completion_watcher(
                state=state,
                display_name=display_name,
                goal=goal,
                context=enriched_context,
                toolsets=toolsets or None,
                model=model,
                parent_session_id=parent_session_id,
            )
            if dispatch.get("status") != "dispatched":
                message = str(dispatch.get("error") or "Completion delivery rejected.")
                state.set_launch_error(message)
                return _error(_named_message(display_name, message))

            try:
                handle_obj = lifecycle.launch(request)
            except Exception as exc:
                state.set_launch_error(str(exc))
                return _error(_named_message(display_name, str(exc)))
            state.set_handle(handle_obj)
            effective_role = getattr(handle_obj, "role", None)
            depth = getattr(handle_obj, "depth", None)
            if effective_role == _REQUESTED_ROLE:
                note = (
                    "Hermes granted orchestrator according to the active delegation "
                    "configuration. The result will re-enter this conversation through "
                    "the normal background delegation completion rail."
                )
            else:
                note = (
                    "Hermes configuration downgraded the requested orchestrator to leaf. "
                    "Check delegation.max_spawn_depth and "
                    "delegation.orchestrator_enabled."
                )
            return json.dumps(
                {
                    "success": True,
                    "status": "launched",
                    "name": display_name,
                    "subagent_id": handle_obj.subagent_id,
                    "delegation_id": dispatch.get("delegation_id"),
                    "model": handle_obj.model,
                    "requested_role": _REQUESTED_ROLE,
                    "effective_role": effective_role,
                    "depth": depth,
                    "toolsets": toolsets if toolsets else "inherited",
                    "skills": loaded_skills,
                    "correlation_id": correlation_id,
                    "live_transcript": dispatch.get("live_transcript"),
                    "note": note,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            if state is not None:
                state.set_launch_error(str(exc))
            return _error(_named_message(display_name, str(exc)))

    return handle


def register(ctx) -> None:
    ctx.register_tool(
        name=_TOOL_NAME,
        toolset="delegation",
        schema=_SCHEMA,
        handler=_make_handler(ctx),
        check_fn=lambda: True,
        description=(
            "Launch one named Hermes subagent with optional skill injection, narrowed "
            "child toolsets, a same-provider model override, and Hermes-configured "
            "spawning depth; use native delegate_task for batches, output schemas, or "
            "live controls."
        ),
        emoji="🧭",
    )
