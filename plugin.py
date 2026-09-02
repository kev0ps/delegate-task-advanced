"""Application service and runtime for the delegate-task-advanced plugin."""

import contextvars
import json
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

TOOL_NAME = "delegate_task_advanced"
TOOLSET = "delegation"
EMOJI = "🧭"

MAX_LIST_ITEMS = 8
MAX_ITEM_CHARS = 128
MAX_DISPLAY_NAME_CHARS = 80
MAX_GOAL_CHARS = 16_000
MAX_CONTEXT_CHARS = 12_000
MAX_IDENTIFIER_CHARS = 128

DEFAULT_MAX_SKILL_CHARS = 20_000
_CONTEXT_LIMIT = 32_000

_ALLOWED_DISPLAY_PUNCTUATION = " -_.()"
_ALLOWED_IDENTIFIER_PUNCTUATION = "-_/:."

_START = f"<!-- BEGIN EXPLICIT SKILLS: {TOOL_NAME} -->"
_END = f"<!-- END EXPLICIT SKILLS: {TOOL_NAME} -->"

DESCRIPTION = (
    "Launch one named Hermes subagent in the background. Use it for a focused "
    "task that may require specific skills, toolsets, or a model override. "
    "Provide all necessary context, constraints, paths, and expected output, "
    "since the child starts with a fresh conversation. Hermes decides the "
    "subagent role and delegation depth. Omit toolsets and model to inherit "
    "the parent defaults. The call returns immediately and the result is "
    "delivered asynchronously. Prefer the native delegate_task by default; "
    "use this tool only when the display name, per-call skill injection, "
    "per-call toolset selection, same-provider model override, or validated "
    "output_schema are actually needed for the mission."
)

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": DESCRIPTION,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_DISPLAY_NAME_CHARS,
                "description": (
                    "Required human-readable display label. It appears in logs, status, "
                    "and completion messages; it is not the generated subagent_id or a "
                    "control handle."
                ),
            },
            "goal": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_GOAL_CHARS,
                "description": (
                    "Required self-contained objective. Include the expected deliverable "
                    "and acceptance criteria because the child cannot see the parent conversation."
                ),
            },
            "context": {
                "type": "string",
                "maxLength": MAX_CONTEXT_CHARS,
                "description": (
                    "Optional mission background: paths, relevant facts, constraints, "
                    "expected output format, language, and tone not already stated in goal."
                ),
            },
            "model": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_IDENTIFIER_CHARS,
                "description": (
                    "Optional model identifier. Omit to inherit the parent model. A supplied "
                    "value overrides only the model within the parent's provider; it cannot "
                    "switch providers and must be compatible with that provider."
                ),
            },
            "skills": {
                "type": "array",
                "maxItems": MAX_LIST_ITEMS,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_ITEM_CHARS,
                },
                "description": (
                    "Optional skill names loaded through skill_view and injected into the "
                    "child context before launch. Skills grant no permissions. Missing or "
                    "unloadable skills fail the launch; duplicate names are ignored."
                ),
            },
            "toolsets": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_LIST_ITEMS,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_ITEM_CHARS,
                },
                "description": (
                    "Optional baseline selection of existing Hermes toolset names. Omit to "
                    "inherit the parent's normal capabilities. Every requested toolset must "
                    "already be available to the parent. These are toolset names, not "
                    "individual tool names, and they do not imply read-only access. Hermes "
                    "may add delegation to the child's capabilities when the derived role "
                    "permits it, so the child can spawn within the globally configured "
                    "depth. An explicit empty list is invalid."
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


# ---- Input validation ----


def validate_display_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be a string.")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("name must not contain control characters.")
    name = " ".join(value.strip().split())
    if not name or len(name) > MAX_DISPLAY_NAME_CHARS:
        raise ValueError(f"name must contain 1 to {MAX_DISPLAY_NAME_CHARS} characters.")
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
    if not identifier or len(identifier) > MAX_IDENTIFIER_CHARS:
        raise ValueError(
            f"{field} must contain 1 to {MAX_IDENTIFIER_CHARS} characters."
        )
    if identifier.startswith(("/", ".")) or ".." in identifier:
        raise ValueError(f"Invalid {field}: {identifier!r}.")
    if any(
        not (char.isalnum() or char in _ALLOWED_IDENTIFIER_PUNCTUATION)
        for char in identifier
    ):
        raise ValueError(f"Invalid {field}: {identifier!r}.")
    return identifier


def validate_text(
    value: Any, field: str, *, required: bool, max_chars: int
) -> str | None:
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


ALLOWED_FIELDS = frozenset(SCHEMA["parameters"]["properties"])


def error(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def named_message(display_name: Optional[str], message: str) -> str:
    if display_name:
        return f"Subagent '{display_name}': {message}"
    return message


def wrap_goal(display_name: str, goal: str) -> str:
    """Build the display-prefixed goal once and reuse it everywhere."""
    return f"Subagent '{display_name}' — {goal}"


def coerce_output_schema(value: Any) -> Optional[dict[str, Any]]:
    from tools.delegation_output_schema import coerce_output_schema as native_coerce

    schema, schema_error = native_coerce(value)
    if schema_error:
        raise ValueError(f"output_schema invalid: {schema_error}")
    return schema


def validate_model(value: Any) -> Optional[str]:
    return validate_identifier(value, "model")


# ---- Skill injection ----


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
            retry_kwargs = {
                key: value for key, value in kwargs.items() if key != "task_id"
            }
            raw = dispatch_tool("skill_view", {"name": name}, **retry_kwargs)
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"Skill {name!r} returned a malformed response."
                ) from exc
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
        payload["schema_retries"] = 0
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
        self.cancelled_before_launch = False
        self.writer: Any = None
        self.live_delegation_id: Optional[str] = None
        self._cancel_context = contextvars.copy_context()
        self._event = threading.Event()
        self._lock = threading.Lock()

    def begin_launch(self) -> bool:
        """Claim the launch boundary unless cancellation already won the race."""
        with self._lock:
            if not self.cancel_requested:
                return True
            self.cancelled_before_launch = True
            self._event.set()
            return False

    def _cancel_handle(self, handle: Any) -> None:
        self._cancel_context.copy().run(
            self.lifecycle.cancel,
            handle,
            reason=named_message(self.display_name, "cancellation requested"),
        )

    def set_handle(self, handle: Any) -> None:
        cancel_now = self._activate_handle(handle)
        try:
            if cancel_now:
                self._cancel_handle(handle)
        finally:
            self._event.set()

    def _activate_handle(self, handle: Any) -> bool:
        """Publish the currently cancellable lifecycle handle under the state lock."""
        with self._lock:
            self.handle = handle
            return self.cancel_requested

    def set_launch_error(self, message: str) -> None:
        with self._lock:
            if self.handle is not None or self.launch_error is not None:
                return
            self.launch_error = message
            self._event.set()

    def cancel(self) -> None:
        with self._lock:
            self.cancel_requested = True
            handle = self.handle
        if handle is not None:
            self._cancel_handle(handle)

    def _retry_invalid_output(self, first: dict[str, Any]) -> dict[str, Any]:
        from dataclasses import replace
        from tools.delegation_output_schema import build_retry_message

        with self._lock:
            if self.cancel_requested:
                return {
                    **first,
                    "status": "interrupted",
                    "exit_reason": "cancelled",
                    "schema_retries": 0,
                    "schema_retry_error": "Correction retry cancelled before launch.",
                }

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
        retry_correlation_id = f"dta-retry-{uuid.uuid4().hex}"
        retry_request = replace(
            self.retry_request,
            goal=(
                f"Correct the previous final response for subagent "
                f"'{self.display_name}' so it satisfies the output contract."
            ),
            context=retry_context,
            correlation_id=retry_correlation_id,
            metadata={
                **dict(self.retry_request.metadata),
                "correlation_id": retry_correlation_id,
                "schema_retry": 1,
            },
        )
        try:
            retry_handle = self.lifecycle.launch(retry_request)
            cancel_now = self._activate_handle(retry_handle)
            if cancel_now:
                self._cancel_handle(retry_handle)
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
        if self.cancelled_before_launch:
            result = {
                "status": "interrupted",
                "summary": named_message(
                    self.display_name, "cancelled before lifecycle launch."
                ),
                "error": named_message(self.display_name, "cancellation requested"),
                "api_calls": 0,
                "duration_seconds": round(time.monotonic() - started, 2),
                "model": None,
                "exit_reason": "cancelled_before_launch",
            }
        elif self.launch_error is not None:
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
        if result.get("status") == "completed" and result.get("schema_valid") is False:
            details = list(result.get("schema_errors") or [])
            retry_error = result.get("schema_retry_error")
            if retry_error:
                details.append(str(retry_error))
            detail = "; ".join(str(item) for item in details[:3])
            result["status"] = "error"
            result["error"] = named_message(
                self.display_name,
                "output_schema validation failed after the bounded correction retry"
                + (f": {detail}" if detail else "."),
            )
            result["exit_reason"] = "output_schema_invalid"
        completion_metadata = {
            key: result[key]
            for key in (
                "effective_role",
                "depth",
                "schema_valid",
                "schema_retries",
                "schema_errors",
            )
            if key in result
        }
        if completion_metadata and result.get("summary"):
            result["summary"] += "\n\ndelegate_task_advanced metadata: " + json.dumps(
                completion_metadata, ensure_ascii=False
            )
        if self.writer is not None:
            try:
                self.writer.finalize(result)
            except Exception:
                pass
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
    parent_session_id: Optional[str],
) -> dict[str, Any]:
    from tools.async_delegation import dispatch_async_delegation
    from tools.delegation_live_log import create_live_transcripts

    session_key, origin_ui_session_id, origin_session_id = routing_context()
    result = dispatch_async_delegation(
        goal=wrapped_goal,
        context=context,
        toolsets=toolsets,
        role="leaf",
        model=model,
        session_key=session_key,
        parent_session_id=parent_session_id,
        runner=state.run,
        origin_ui_session_id=origin_ui_session_id,
        origin_session_id=origin_session_id,
        interrupt_fn=state.cancel,
    )
    if result.get("status") == "dispatched":
        try:
            live_id, writers, paths = create_live_transcripts(
                [{"goal": wrapped_goal, "context": context}],
                context,
                delegation_id=result.get("delegation_id"),
            )
            state.live_delegation_id = live_id
            state.writer = writers[0] if writers else None
            if paths:
                result["live_transcript"] = paths[0]
        except Exception:
            pass
    return result


# ---- Model-facing application service ----


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Validated launch data produced by the application service."""

    display_name: str
    goal: str
    context: Optional[str]
    model: Optional[str]
    skills: tuple[str, ...]
    toolsets: tuple[str, ...]
    output_schema: Optional[dict[str, Any]]
    parent_session_id: Optional[str]

    @property
    def wrapped_goal(self) -> str:
        return wrap_goal(self.display_name, self.goal)


class DelegateTaskAdvanced:
    """Application service and public definition of the advanced delegation tool."""

    name = TOOL_NAME
    toolset = TOOLSET
    schema = SCHEMA
    description = DESCRIPTION
    emoji = EMOJI

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.lifecycle = ctx.subagent_lifecycle

    def handle(self, params: dict, **kwargs: Any) -> str:
        if not isinstance(params, dict):
            return error("Tool input must be an object.")

        display_name: Optional[str] = None
        state: Optional[DeferredLaunch] = None
        try:
            display_name = self._validate_envelope(params)
            spec = self._build_spec(params, display_name, kwargs.get("parent_agent"))
            request = self._prepare(spec, kwargs)
            state = DeferredLaunch(
                self.lifecycle, spec.display_name, spec.output_schema
            )
            if spec.output_schema is not None:
                state.retry_request = request
            dispatch = self._reserve(spec, request, state)
            return self._launch(spec, request, state, dispatch)
        except Exception as exc:
            if state is not None:
                state.set_launch_error(str(exc))
            return error(named_message(display_name, str(exc)))

    @staticmethod
    def _validate_envelope(params: dict[str, Any]) -> str:
        unknown = sorted(set(params) - ALLOWED_FIELDS)
        if unknown:
            raise ValueError(f"Unknown fields: {', '.join(unknown)}.")
        return validate_display_name(params.get("name"))

    def _build_spec(
        self,
        params: dict[str, Any],
        display_name: str,
        explicit_parent: Any = None,
    ) -> LaunchSpec:
        goal = validate_text(
            params.get("goal"),
            "goal",
            required=True,
            max_chars=MAX_GOAL_CHARS,
        )
        assert goal is not None
        if len(wrap_goal(display_name, goal)) > MAX_GOAL_CHARS:
            raise ValueError(
                "goal is too long once the required display-name prefix is applied."
            )

        context = validate_text(
            params.get("context"),
            "context",
            required=False,
            max_chars=MAX_CONTEXT_CHARS,
        )
        model = validate_model(params.get("model"))
        skills = tuple(normalize_name_list(params.get("skills"), "skills"))
        if "toolsets" in params and params.get("toolsets") == []:
            raise ValueError(
                "toolsets cannot be an explicit empty list; omit it to inherit Hermes capabilities."
            )
        toolsets = tuple(normalize_name_list(params.get("toolsets"), "toolsets"))
        output_schema = coerce_output_schema(params.get("output_schema"))

        parent = self._resolve_parent(explicit_parent)
        self._validate_toolsets(toolsets, parent)
        parent_session_id = str(getattr(parent, "session_id", "") or "") or None
        return LaunchSpec(
            display_name=display_name,
            goal=goal,
            context=context,
            model=model,
            skills=skills,
            toolsets=toolsets,
            output_schema=output_schema,
            parent_session_id=parent_session_id,
        )

    def _prepare(self, spec: LaunchSpec, kwargs: dict[str, Any]) -> Any:
        dispatch_kwargs = {
            key: kwargs[key]
            for key in ("task_id", "session_id")
            if kwargs.get(key) is not None
        }
        context, _ = build_skill_context(
            spec.context,
            list(spec.skills),
            self.ctx.dispatch_tool,
            dispatch_kwargs=dispatch_kwargs,
        )
        if spec.output_schema is not None:
            from tools.delegation_output_schema import append_output_contract

            context = append_output_contract(context, spec.output_schema)
            if len(context) > _CONTEXT_LIMIT:
                raise ValueError(
                    "Mission context plus skills and output_schema exceeds Hermes' "
                    f"{_CONTEXT_LIMIT}-character lifecycle limit."
                )
        return self._build_request(spec, context)

    def _reserve(
        self,
        spec: LaunchSpec,
        request: Any,
        state: DeferredLaunch,
    ) -> dict[str, Any]:
        dispatch = dispatch_completion_watcher(
            state=state,
            wrapped_goal=spec.wrapped_goal,
            context=request.context,
            toolsets=list(spec.toolsets) or None,
            model=spec.model,
            parent_session_id=spec.parent_session_id,
        )
        if dispatch.get("status") != "dispatched":
            raise RuntimeError(
                str(dispatch.get("error") or "Completion delivery rejected.")
            )
        return dispatch

    def _launch(
        self,
        spec: LaunchSpec,
        request: Any,
        state: DeferredLaunch,
        dispatch: dict[str, Any],
    ) -> str:
        if not state.begin_launch():
            return json.dumps(
                {
                    "success": True,
                    "status": "interrupted",
                    "name": spec.display_name,
                    "subagent_id": None,
                    "delegation_id": dispatch.get("delegation_id"),
                    "model": spec.model,
                    "effective_role": None,
                    "depth": None,
                    "toolsets": list(spec.toolsets) if spec.toolsets else "inherited",
                    "skills": list(spec.skills),
                    "correlation_id": request.correlation_id,
                    "live_transcript": dispatch.get("live_transcript"),
                    "note": "Cancellation won before the lifecycle child was launched.",
                },
                ensure_ascii=False,
            )
        handle = self.lifecycle.launch(request)
        state.set_handle(handle)
        return json.dumps(
            {
                "success": True,
                "status": "launched",
                "name": spec.display_name,
                "subagent_id": handle.subagent_id,
                "delegation_id": dispatch.get("delegation_id"),
                "model": handle.model,
                "effective_role": getattr(handle, "role", None),
                "depth": getattr(handle, "depth", None),
                "toolsets": list(spec.toolsets) if spec.toolsets else "inherited",
                "skills": list(spec.skills),
                "correlation_id": request.correlation_id,
                "live_transcript": dispatch.get("live_transcript"),
                "note": (
                    "Hermes derived the subagent capabilities from the spawn depth. "
                    "The result will re-enter this conversation through the normal "
                    "background completion rail."
                ),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _resolve_parent(explicit_parent: Any) -> Any:
        if explicit_parent is not None:
            return explicit_parent
        from agent.subagent_lifecycle import get_active_subagent_parent

        return get_active_subagent_parent()

    @staticmethod
    def _validate_toolsets(toolsets: Sequence[str], parent_agent: Any) -> None:
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

    @staticmethod
    def _build_request(spec: LaunchSpec, context: Optional[str]) -> Any:
        from agent.subagent_lifecycle import SubagentLaunchRequest

        correlation_id = f"dta-{uuid.uuid4().hex}"
        return SubagentLaunchRequest(
            goal=spec.wrapped_goal,
            context=context,
            model=spec.model,
            allowed_toolsets=spec.toolsets or None,
            parent_session_id=spec.parent_session_id,
            correlation_id=correlation_id,
            metadata={
                "source_tool": TOOL_NAME,
                "display_name": spec.display_name,
                "requested_skills": list(spec.skills),
                "requested_toolsets": list(spec.toolsets),
                "requested_model": spec.model,
                "parent_session_id": spec.parent_session_id,
                "correlation_id": correlation_id,
            },
        )
