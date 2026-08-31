"""Single-module implementation of the delegate-task-advanced Hermes plugin."""

import json
import threading
import time
import unicodedata
import uuid
from typing import Any, Callable, Optional


# ---- Input validation ----

MAX_LIST_ITEMS = 8
MAX_ITEM_CHARS = 128
MAX_DISPLAY_NAME_CHARS = 80
_ALLOWED_DISPLAY_PUNCTUATION = " -_.()"
_ALLOWED_IDENTIFIER_PUNCTUATION = "-_/:."


def validate_display_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be a string.")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("name must not contain control characters.")
    name = " ".join(value.strip().split())
    if not name or len(name) > MAX_DISPLAY_NAME_CHARS:
        raise ValueError("name must contain 1 to 80 characters.")
    for char in name:
        if not (char.isalnum() or char in _ALLOWED_DISPLAY_PUNCTUATION):
            raise ValueError(
                "name may contain letters, numbers, spaces, hyphens, underscores, dots, and parentheses only."
            )
    return name


def normalize_name_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of names.")
    if len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"{field} accepts at most {MAX_LIST_ITEMS} entries.")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field} entries must be strings.")
        name = item.strip()
        if not name or len(name) > MAX_ITEM_CHARS:
            raise ValueError(
                f"{field} entries must contain 1 to {MAX_ITEM_CHARS} characters."
            )
        if name.startswith(("/", ".")) or ".." in name:
            raise ValueError(f"Invalid {field} entry: {name!r}.")
        if any(
            not (char.isalnum() or char in _ALLOWED_IDENTIFIER_PUNCTUATION)
            for char in name
        ):
            raise ValueError(f"Invalid {field} entry: {name!r}.")
        if name not in seen:
            normalized.append(name)
            seen.add(name)
    return normalized


def validate_identifier(value: Any, field: str) -> str | None:
    """Validate one optional Hermes/provider identifier without interpreting it."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    identifier = value.strip()
    if not identifier or len(identifier) > 128:
        raise ValueError(f"{field} must contain 1 to 128 characters.")
    if identifier.startswith(("/", ".")) or ".." in identifier:
        raise ValueError(f"Invalid {field}: {identifier!r}.")
    if any(
        not (char.isalnum() or char in _ALLOWED_IDENTIFIER_PUNCTUATION)
        for char in identifier
    ):
        raise ValueError(f"Invalid {field}: {identifier!r}.")
    return identifier


def validate_text(value: Any, field: str, *, required: bool, max_chars: int) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{field} must not be empty.")
    if len(text) > max_chars:
        raise ValueError(f"{field} exceeds the {max_chars}-character limit.")
    return text


# ---- Tool contract (schema + validation entry points) ----


TOOL_NAME = "delegate_task_advanced"
DEFAULT_ROLE = "orchestrator"
ROLES = frozenset({"leaf", "orchestrator"})
ALLOWED_FIELDS = frozenset(
    {"name", "goal", "context", "skills", "toolsets", "model", "role", "output_schema"}
)

SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Launch one named Hermes subagent in the background. Use it for a focused task that may require specific skills, toolsets, or a model override. Provide all necessary context, constraints, paths, and expected output, since the child starts with a fresh conversation. Hermes decides the subagent role and delegation depth. Omit toolsets and model to inherit the parent defaults. The call returns immediately and the result is delivered asynchronously. Prefer the native delegate_task by default; use this tool only when the display name, per-call skill injection, per-call toolset selection, same-provider model override, or per-call role and validated output_schema are actually needed for the mission."
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
            "role": {
                "type": "string",
                "enum": ["leaf", "orchestrator"],
                "description": (
                    "Optional role for this child. Defaults to orchestrator for backward "
                    "compatibility. Hermes may downgrade orchestrator according to "
                    "delegation.max_spawn_depth and delegation.orchestrator_enabled."
                ),
            },
            "output_schema": {
                "type": "object",
                "description": (
                    "Optional JSON Schema for the child's final answer. The contract is "
                    "injected before launch and the final answer is validated. One bounded "
                    "correction retry is allowed. The result gains schema_valid, "
                    "schema_retries, and schema_errors on final failure."
                ),
            },
        },
        "required": ["name", "goal"],
    },
}


def error(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def named_message(display_name: Optional[str], message: str) -> str:
    if display_name:
        return f"Subagent '{display_name}': {message}"
    return message


def wrap_goal(display_name: str, goal: str) -> str:
    """Build the display-prefixed goal once and reuse it everywhere."""
    return f"Subagent '{display_name}' — {goal}"


def validate_role(value: Any) -> str:
    if value is None:
        return DEFAULT_ROLE
    if not isinstance(value, str) or value.strip().lower() not in ROLES:
        raise ValueError("role must be 'leaf' or 'orchestrator'.")
    return value.strip().lower()


def coerce_output_schema(value: Any) -> Optional[dict[str, Any]]:
    from tools.delegation_output_schema import coerce_output_schema as native_coerce

    schema, schema_error = native_coerce(value)
    if schema_error:
        raise ValueError(f"output_schema invalid: {schema_error}")
    return schema


def validate_model(value: Any) -> Optional[str]:
    return validate_identifier(value, "model")


# ---- Skill injection ----

DEFAULT_MAX_SKILL_CHARS = 20_000
_CONTEXT_LIMIT = 32_000
_START = "<!-- BEGIN EXPLICIT SKILLS: delegate_task_advanced -->"
_END = "<!-- END EXPLICIT SKILLS: delegate_task_advanced -->"


def build_skill_context(
    base_context: Optional[str],
    skill_names: list[str],
    dispatch_tool: Callable[..., str],
    *,
    max_chars: int = DEFAULT_MAX_SKILL_CHARS,
    dispatch_kwargs: Optional[dict] = None,
) -> tuple[Optional[str], list[str]]:
    if not skill_names:
        return base_context, []

    blocks: list[str] = []
    total = 0
    kwargs = dispatch_kwargs or {}
    for name in skill_names:
        raw = dispatch_tool("skill_view", {"name": name}, **kwargs)
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Skill {name!r} returned a malformed response.") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Skill {name!r} returned a malformed response.")

        # skill_view deduplicates repeat reads per parent task. That stub is
        # useful for the model, but this plugin needs the full text to inject
        # into a new child context. Retry once without the parent's task_id;
        # keep session_id so usage attribution remains intact.
        if (
            payload.get("success") is True
            and payload.get("dedup") is True
            and payload.get("content_returned") is False
            and "task_id" in kwargs
        ):
            retry_kwargs = {key: value for key, value in kwargs.items() if key != "task_id"}
            raw = dispatch_tool("skill_view", {"name": name}, **retry_kwargs)
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Skill {name!r} returned a malformed response.") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"Skill {name!r} returned a malformed response.")

        if payload.get("success") is not True:
            detail = str(payload.get("error") or "skill_view failed")
            raise ValueError(f"Could not load skill {name!r}: {detail}")
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"Skill {name!r} returned no content.")
        block = f"### Skill: {name}\n\n{content.strip()}"
        total += len(block)
        if total > max_chars:
            raise ValueError(
                f"Requested skills exceed the {max_chars}-character injection limit."
            )
        blocks.append(block)

    framed = f"{_START}\n" + "\n\n".join(blocks) + f"\n{_END}"
    combined = f"{base_context}\n\n{framed}" if base_context else framed
    if len(combined) > _CONTEXT_LIMIT:
        raise ValueError(
            f"Mission context plus skills exceeds Hermes' {_CONTEXT_LIMIT}-character lifecycle limit."
        )
    return combined, list(skill_names)


# ---- Async lifecycle ----


def _value(obj: Any) -> str:
    return str(getattr(obj, "value", obj) or "")


def result_payload(
    lifecycle: Any,
    handle: Any,
    display_name: str,
    output_schema: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    lifecycle.wait(handle, timeout_seconds=None)
    result = lifecycle.result(handle)
    terminal = _value(result.terminal_state).upper()
    status = (
        "completed"
        if terminal == "SUCCEEDED"
        else "interrupted"
        if terminal in {"INTERRUPTED", "CANCELLED"}
        else "error"
    )
    summary = result.summary
    if summary:
        summary = f"Subagent '{display_name}'\n\n{summary}"
    else:
        summary = f"Subagent '{display_name}' finished without a usable result."
    result_error = result.error_message
    if result_error:
        result_error = named_message(display_name, str(result_error))
    usage = result.usage_metadata if isinstance(result.usage_metadata, dict) else {}
    execution = (
        result.tool_execution_summary
        if isinstance(result.tool_execution_summary, dict)
        else {}
    )
    payload = {
        "status": status,
        "summary": summary,
        "error": result_error,
        "api_calls": usage.get("api_calls", 0),
        "duration_seconds": execution.get("duration_seconds"),
        "model": getattr(handle, "model", None),
        "effective_role": getattr(handle, "role", None),
        "depth": getattr(handle, "depth", None),
        "exit_reason": terminal.lower(),
    }
    if output_schema is not None:
        from tools.delegation_output_schema import validate_output

        schema_valid, schema_errors = validate_output(
            str(result.summary or ""), output_schema
        )
        payload["schema_valid"] = schema_valid
        if not schema_valid:
            payload["schema_errors"] = schema_errors
    return payload


class DeferredLaunch:
    """Reserve completion routing before launch, eliminating orphaned children."""

    def __init__(
        self,
        lifecycle: Any,
        display_name: str,
        output_schema: Optional[dict[str, Any]] = None,
    ):
        self.lifecycle = lifecycle
        self.display_name = display_name
        self.output_schema = output_schema
        self.retry_request: Any = None
        self.handle: Any = None
        self.launch_error: Optional[str] = None
        self.cancel_requested = False
        self.writer: Any = None
        self.live_delegation_id: Optional[str] = None
        self._event = threading.Event()
        self._lock = threading.Lock()

    def set_handle(self, handle: Any) -> None:
        with self._lock:
            self.handle = handle
            cancel_now = self.cancel_requested
        if cancel_now:
            try:
                self.lifecycle.cancel(
                    handle, reason=named_message(self.display_name, "cancellation requested")
                )
            finally:
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
            self.lifecycle.cancel(
                handle, reason=named_message(self.display_name, "cancellation requested")
            )

    def _retry_invalid_output(self, first: dict[str, Any]) -> dict[str, Any]:
        from dataclasses import replace
        from tools.delegation_output_schema import build_retry_message

        errors = list(first.get("schema_errors") or [])
        correction = build_retry_message(errors)
        prior = str(first.get("summary") or "")
        if prior.startswith(f"Subagent '{self.display_name}'"):
            prior = prior.split("\n\n", 1)[-1]
        retry_context = (
            f"{str(self.retry_request.context or '')[:24000]}\n\n"
            "CORRECTION RETRY (fresh lifecycle child):\n"
            f"Previous response:\n{prior[:4000]}\n\n{correction[:3000]}"
        )
        retry_request = replace(
            self.retry_request,
            goal=(
                f"Correct the previous final response for subagent "
                f"'{self.display_name}' so it satisfies the output contract."
            ),
            context=retry_context,
            correlation_id=f"dta-retry-{uuid.uuid4().hex}",
            metadata={**dict(self.retry_request.metadata), "schema_retry": 1},
        )
        try:
            retry_handle = self.lifecycle.launch(retry_request)
            retried = result_payload(
                self.lifecycle,
                retry_handle,
                self.display_name,
                self.output_schema,
            )
            retried["schema_retries"] = 1
            retried["api_calls"] = int(first.get("api_calls", 0) or 0) + int(
                retried.get("api_calls", 0) or 0
            )
            retried["duration_seconds"] = round(
                float(first.get("duration_seconds", 0) or 0)
                + float(retried.get("duration_seconds", 0) or 0),
                2,
            )
            return retried
        except Exception as retry_exc:
            first["schema_retries"] = 1
            first["schema_retry_error"] = str(retry_exc)
            return first

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        self._event.wait()
        if self.launch_error is not None:
            result = {
                "status": "error",
                "summary": named_message(self.display_name, "launch failed."),
                "error": named_message(self.display_name, self.launch_error),
                "api_calls": 0,
                "duration_seconds": round(time.monotonic() - started, 2),
                "model": None,
                "exit_reason": "launch_error",
            }
        else:
            try:
                result = result_payload(
                    self.lifecycle,
                    self.handle,
                    self.display_name,
                    self.output_schema,
                )
                if (
                    self.output_schema is not None
                    and result.get("status") == "completed"
                    and result.get("schema_valid") is False
                    and self.retry_request is not None
                ):
                    result = self._retry_invalid_output(result)
            except Exception as exc:
                result = {
                    "status": "error",
                    "summary": named_message(
                        self.display_name, "lifecycle monitoring failed."
                    ),
                    "error": named_message(self.display_name, str(exc)),
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


def routing_context() -> tuple[str, str, str]:
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
    return session_key, origin_ui_session_id, origin_session_id


def dispatch_completion_watcher(
    *,
    state: DeferredLaunch,
    wrapped_goal: str,
    context: Optional[str],
    toolsets: Optional[list[str]],
    model: Optional[str],
    role: str,
    parent_session_id: Optional[str],
) -> dict[str, Any]:
    from tools.async_delegation import dispatch_async_delegation
    from tools.delegation_live_log import create_live_transcripts

    session_key, origin_ui_session_id, origin_session_id = routing_context()
    result = dispatch_async_delegation(
        goal=wrapped_goal,
        context=context,
        toolsets=toolsets,
        role=role,
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
            [{"goal": wrapped_goal, "context": context}],
            context,
            delegation_id=result.get("delegation_id"),
        )
        state.live_delegation_id = live_id
        state.writer = writers[0] if writers else None
        if paths:
            result["live_transcript"] = paths[0]
    return result


# ---- Model-facing handler ----


def active_parent(explicit_parent: Any) -> Any:
    if explicit_parent is not None:
        return explicit_parent
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent

        return get_active_subagent_parent()
    except Exception:
        return None


def validate_toolsets(toolsets: list[str], parent_agent: Any) -> None:
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


def make_handler(ctx: Any):
    def handle(params: dict, **kwargs: Any) -> str:
        display_name: Optional[str] = None
        state: Optional[DeferredLaunch] = None
        if not isinstance(params, dict):
            return error("Tool input must be an object.")
        unknown = sorted(set(params) - ALLOWED_FIELDS)
        if unknown:
            return error(f"Unknown fields: {', '.join(unknown)}.")
        try:
            display_name = validate_display_name(params.get("name"))
            goal = validate_text(
                params.get("goal"), "goal", required=True, max_chars=16000
            )
            wrapped_goal = wrap_goal(display_name, goal)
            if len(wrapped_goal) > 16000:
                raise ValueError(
                    "goal is too long once the required display-name prefix is applied."
                )
            context = validate_text(
                params.get("context"), "context", required=False, max_chars=12000
            )
            model = validate_model(params.get("model"))
            role = validate_role(params.get("role"))
            skills = normalize_name_list(params.get("skills"), "skills")
            if "toolsets" in params and params.get("toolsets") == []:
                raise ValueError(
                    "toolsets cannot be an explicit empty list; omit it to inherit Hermes capabilities."
                )
            toolsets = normalize_name_list(params.get("toolsets"), "toolsets")
            output_schema = coerce_output_schema(params.get("output_schema"))
            parent_agent = active_parent(kwargs.get("parent_agent"))
            validate_toolsets(toolsets, parent_agent)
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
            if output_schema is not None:
                from tools.delegation_output_schema import append_output_contract

                enriched_context = append_output_contract(
                    enriched_context, output_schema
                )

            from agent.subagent_lifecycle import SubagentLaunchRequest

            correlation_id = f"dta-{uuid.uuid4().hex}"
            parent_session_id = (
                str(getattr(parent_agent, "session_id", "") or "") or None
            )
            metadata = {
                "source_tool": TOOL_NAME,
                "display_name": display_name,
                "requested_skills": loaded_skills,
                "requested_toolsets": toolsets,
                "requested_model": model,
                "requested_role": role,
                "parent_session_id": parent_session_id,
                "correlation_id": correlation_id,
            }
            request = SubagentLaunchRequest(
                goal=wrapped_goal,
                context=enriched_context,
                role=role,
                model=model,
                allowed_toolsets=tuple(toolsets) if toolsets else None,
                parent_session_id=parent_session_id,
                correlation_id=correlation_id,
                metadata=metadata,
            )
            lifecycle = ctx.subagent_lifecycle
            state = DeferredLaunch(lifecycle, display_name, output_schema)
            if output_schema is not None:
                state.retry_request = request
            dispatch = dispatch_completion_watcher(
                state=state,
                wrapped_goal=wrapped_goal,
                context=enriched_context,
                toolsets=toolsets or None,
                model=model,
                role=role,
                parent_session_id=parent_session_id,
            )
            if dispatch.get("status") != "dispatched":
                message = str(
                    dispatch.get("error") or "Completion delivery rejected."
                )
                state.set_launch_error(message)
                return error(named_message(display_name, message))

            try:
                handle_obj = lifecycle.launch(request)
            except Exception as exc:
                state.set_launch_error(str(exc))
                return error(named_message(display_name, str(exc)))
            state.set_handle(handle_obj)
            effective_role = getattr(handle_obj, "role", None)
            if role == "orchestrator" and effective_role != "orchestrator":
                note = (
                    "Hermes configuration downgraded the requested orchestrator to leaf. "
                    "Check delegation.max_spawn_depth and delegation.orchestrator_enabled."
                )
            else:
                note = (
                    "The requested role was accepted. The result will re-enter this "
                    "conversation through the normal background completion rail."
                )
            return json.dumps(
                {
                    "success": True,
                    "status": "launched",
                    "name": display_name,
                    "subagent_id": handle_obj.subagent_id,
                    "delegation_id": dispatch.get("delegation_id"),
                    "model": handle_obj.model,
                    "requested_role": role,
                    "effective_role": effective_role,
                    "depth": getattr(handle_obj, "depth", None),
                    "toolsets": toolsets if toolsets else "inherited",
                    "skills": loaded_skills,
                    "correlation_id": correlation_id,
                    "live_transcript": dispatch.get("live_transcript"),
                    "note": note,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            if state is not None and state.handle is None and state.launch_error is None:
                state.set_launch_error(str(exc))
            return error(named_message(display_name, str(exc)))

    return handle
