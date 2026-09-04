"""Application service and runtime for the delegate-task-advanced plugin."""

import contextvars
import json
import logging
import threading
import time
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from agent.subagent_lifecycle import SubagentLaunchRequest, get_active_subagent_parent
from gateway.session_context import get_session_env
from tools.approval import get_current_session_key
from tools.async_delegation import dispatch_async_delegation
from tools.delegation_live_log import create_live_transcripts, update_manifest_statuses
from tools.delegation_output_schema import (
    append_output_contract,
    build_retry_message,
    coerce_output_schema,
    validate_output,
)
from tools.registry import tool_error, tool_result
from toolsets import get_toolset

TOOL_NAME = "delegate_task_advanced"
TOOLSET = "delegation"
EMOJI = "🧭"
MAX_LIST_ITEMS = 8
MAX_ITEM_CHARS = 128
MAX_DISPLAY_NAME_CHARS = 80
MAX_GOAL_CHARS = 16_000
MAX_USER_CONTEXT_CHARS = 12_000
MAX_IDENTIFIER_CHARS = 128
MAX_SKILL_CHARS = 20_000
MAX_TOTAL_CONTEXT_CHARS = 32_000
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
                "maxLength": MAX_USER_CONTEXT_CHARS,
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
                "additionalProperties": True,
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

_LOGGER = logging.getLogger(__name__)
_ALLOWED_DISPLAY_PUNCTUATION = " -_.()"
_ALLOWED_IDENTIFIER_PUNCTUATION = "-_/:."
_START = f"<!-- BEGIN EXPLICIT SKILLS: {TOOL_NAME} -->"
_END = f"<!-- END EXPLICIT SKILLS: {TOOL_NAME} -->"


# ---- Async lifecycle engine ----


class _DeferredLaunch:
    """Reserve completion routing before launch, eliminating orphaned children."""

    def __init__(
        self,
        lifecycle: Any,
        display_name: str,
        output_schema: dict[str, Any] | None = None,
    ):
        self.lifecycle, self.display_name, self.output_schema = (
            lifecycle,
            display_name,
            output_schema,
        )
        self.retry_request = self.handle = None
        self.launch_error: str | None = None
        self.cancel_requested = self.cancelled_before_launch = False
        self.writer = None
        self.live_delegation_id: str | None = None
        self._cancel_context = contextvars.copy_context()
        self._event, self._lock = threading.Event(), threading.Lock()

    def _named_message(self, message: str) -> str:
        return f"Subagent '{self.display_name}': {message}"

    def begin_launch(self) -> bool:
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
            reason=self._named_message("cancellation requested"),
        )

    def set_handle(self, handle: Any) -> None:
        with self._lock:
            self.handle, cancel_now = handle, self.cancel_requested

        try:
            if cancel_now:
                self._cancel_handle(handle)
        finally:
            self._event.set()

    def set_launch_error(self, message: str) -> None:
        with self._lock:
            if self.handle is None and self.launch_error is None:
                self.launch_error = message
                self._event.set()

    def cancel(self) -> None:
        with self._lock:
            self.cancel_requested = True
            handle = self.handle

        if handle is not None:
            self._cancel_handle(handle)

    def _result_payload(self, handle: Any) -> dict[str, Any]:
        self.lifecycle.wait(handle, timeout_seconds=None)
        result = self.lifecycle.result(handle)

        terminal_state = result.terminal_state
        terminal = str(getattr(terminal_state, "value", terminal_state) or "").upper()

        status = (
            "completed"
            if terminal == "SUCCEEDED"
            else "interrupted"
            if terminal in {"INTERRUPTED", "CANCELLED"}
            else "error"
        )

        usage = result.usage_metadata if isinstance(result.usage_metadata, dict) else {}
        execution = (
            result.tool_execution_summary
            if isinstance(result.tool_execution_summary, dict)
            else {}
        )

        payload = {
            "status": status,
            "summary": (
                f"Subagent '{self.display_name}'\n\n{result.summary}"
                if result.summary
                else f"Subagent '{self.display_name}' finished without a usable result."
            ),
            "error": (
                self._named_message(str(result.error_message))
                if result.error_message
                else None
            ),
            "api_calls": usage.get("api_calls", 0),
            "duration_seconds": execution.get("duration_seconds"),
            "model": getattr(handle, "model", None),
            "effective_role": getattr(handle, "role", None),
            "depth": getattr(handle, "depth", None),
            "exit_reason": terminal.lower(),
        }

        if self.output_schema is not None:
            schema_valid, schema_errors = validate_output(
                str(result.summary or ""),
                self.output_schema,
            )

            payload.update(
                schema_valid=schema_valid,
                schema_retries=0,
            )

            if not schema_valid:
                payload["schema_errors"] = schema_errors

        return payload

    def _failure(
        self,
        status: str,
        summary: str,
        error_message: str,
        exit_reason: str,
        started: float,
        model: Any = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "summary": self._named_message(summary),
            "error": self._named_message(error_message),
            "api_calls": 0,
            "duration_seconds": round(time.monotonic() - started, 2),
            "model": model,
            "exit_reason": exit_reason,
        }

    def _retry_invalid_output(
        self,
        first: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            if self.cancel_requested:
                return {
                    **first,
                    "status": "interrupted",
                    "exit_reason": "cancelled",
                    "schema_retries": 0,
                    "schema_retry_error": ("Correction retry cancelled before launch."),
                }

        errors = list(first.get("schema_errors") or [])
        prior = str(first.get("summary") or "")

        if prior.startswith(f"Subagent '{self.display_name}'"):
            prior = prior.split("\n\n", 1)[-1]

        retry_context = (
            f"{str(self.retry_request.context or '')[:24000]}\n\n"
            "CORRECTION RETRY (fresh lifecycle child):\n"
            f"Previous response:\n{prior[:4000]}\n\n"
            f"{build_retry_message(errors)[:3000]}"
        )

        correlation_id = f"dta-retry-{uuid.uuid4().hex}"

        retry_request = replace(
            self.retry_request,
            goal=(
                f"Correct the previous final response for subagent "
                f"'{self.display_name}' so it satisfies the output contract."
            ),
            context=retry_context,
            correlation_id=correlation_id,
            metadata={
                **dict(self.retry_request.metadata),
                "correlation_id": correlation_id,
                "schema_retry": 1,
            },
        )

        try:
            retry_handle = self.lifecycle.launch(retry_request)
            self.set_handle(retry_handle)

            retried = self._result_payload(retry_handle)
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

        # A correction retry is an optional recovery path. Any provider or
        # lifecycle failure is reported in the original result instead of
        # masking the first validation failure.
        except Exception as exc:  # noqa: BLE001
            first.update(
                schema_retries=1,
                schema_retry_error=str(exc),
            )
            return first

    def _finalize_result(
        self,
        result: dict[str, Any],
    ) -> None:
        if result.get("status") == "completed" and result.get("schema_valid") is False:
            details = list(result.get("schema_errors") or [])

            if result.get("schema_retry_error"):
                details.append(str(result["schema_retry_error"]))

            detail = "; ".join(str(item) for item in details[:3])

            result.update(
                status="error",
                error=self._named_message(
                    "output_schema validation failed after the bounded correction retry"
                    + (f": {detail}" if detail else ".")
                ),
                exit_reason="output_schema_invalid",
            )

        metadata = {
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

        if metadata and result.get("summary"):
            result["summary"] += "\n\ndelegate_task_advanced metadata: " + json.dumps(
                metadata, ensure_ascii=False
            )

    def _publish_result(
        self,
        result: dict[str, Any],
    ) -> None:
        if self.writer is not None:
            try:
                self.writer.finalize(result)
            # Result delivery must remain best effort once the lifecycle has
            # completed; a transcript sink must not replace the real result.
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Could not finalize the live transcript: %s", exc)

        if self.live_delegation_id:
            try:
                update_manifest_statuses(
                    self.live_delegation_id,
                    [{"task_index": 0, **result}],
                )
            # The manifest is observability data and must not affect the
            # completion payload when its backend is unavailable.
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Could not update the delegation manifest: %s", exc)

    def _resolve_result(
        self,
        started: float,
    ) -> dict[str, Any]:
        if self.cancelled_before_launch:
            return self._failure(
                "interrupted",
                "cancelled before lifecycle launch.",
                "cancellation requested",
                "cancelled_before_launch",
                started,
            )

        if self.launch_error is not None:
            return self._failure(
                "error",
                "launch failed.",
                self.launch_error,
                "launch_error",
                started,
            )

        try:
            result = self._result_payload(self.handle)

            if (
                self.output_schema is not None
                and result.get("status") == "completed"
                and result.get("schema_valid") is False
                and self.retry_request is not None
            ):
                return self._retry_invalid_output(result)

            return result

        # The lifecycle is an external runtime boundary. Convert provider
        # failures into the stable error payload exposed by this tool.
        except Exception as exc:  # noqa: BLE001
            return self._failure(
                "error",
                "lifecycle monitoring failed.",
                str(exc),
                "lifecycle_error",
                started,
                getattr(self.handle, "model", None),
            )

    def run(self) -> dict[str, Any]:
        started = time.monotonic()

        self._event.wait()

        result = self._resolve_result(started)

        self._finalize_result(result)
        self._publish_result(result)

        return result


# ---- Model-facing application service ----


class DelegateTaskAdvanced:
    """Application service and public definition of the advanced delegation tool."""

    name = TOOL_NAME
    toolset = TOOLSET
    schema = SCHEMA
    description = DESCRIPTION
    emoji = EMOJI

    @dataclass(frozen=True, slots=True)
    class _LaunchSpec:
        display_name: str
        goal: str
        context: str | None
        model: str | None
        skills: tuple[str, ...]
        toolsets: tuple[str, ...]
        output_schema: dict[str, Any] | None
        parent_session_id: str | None

        @property
        def wrapped_goal(self) -> str:
            return f"Subagent '{self.display_name}' — {self.goal}"

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.lifecycle = ctx.subagent_lifecycle

    @staticmethod
    def _named_message(
        display_name: str | None,
        message: str,
    ) -> str:
        return f"Subagent '{display_name}': {message}" if display_name else message

    def handle(
        self,
        params: dict,
        **kwargs: Any,
    ) -> str:
        if not isinstance(params, dict):
            return tool_error("Tool input must be an object.", success=False)
        display_name: str | None = None
        state: _DeferredLaunch | None = None

        try:
            display_name = self._validate_envelope(params)

            spec = self._build_spec(
                params,
                display_name,
                kwargs.get("parent_agent"),
            )

            request = self._prepare(
                spec,
                kwargs,
            )

            state = _DeferredLaunch(
                self.lifecycle,
                spec.display_name,
                spec.output_schema,
            )

            if spec.output_schema is not None:
                state.retry_request = request

            dispatch = self._reserve(
                spec,
                request,
                state,
            )

            return self._launch(
                spec,
                request,
                state,
                dispatch,
            )

        # Keep the public handler total: setup failures become a structured
        # error response instead of escaping into the host tool runner.
        except Exception as exc:  # noqa: BLE001
            if state is not None:
                state.set_launch_error(str(exc))

            return tool_error(self._named_message(display_name, str(exc)), success=False)

    @staticmethod
    def _validate_envelope(
        params: dict[str, Any],
    ) -> str:
        unknown = sorted(set(params) - set(SCHEMA["parameters"]["properties"]))

        if unknown:
            raise ValueError(f"Unknown fields: {', '.join(unknown)}.")

        value = params.get("name")

        if not isinstance(value, str):
            raise TypeError("name must be a string.")

        if any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("name must not contain control characters.")

        name = " ".join(value.strip().split())

        if not name or len(name) > MAX_DISPLAY_NAME_CHARS:
            raise ValueError(
                f"name must contain 1 to {MAX_DISPLAY_NAME_CHARS} characters."
            )

        if any(
            not (char.isalnum() or char in _ALLOWED_DISPLAY_PUNCTUATION)
            for char in name
        ):
            raise ValueError(
                "name may contain letters, numbers, spaces, "
                "hyphens, underscores, dots, and parentheses only."
            )

        return name

    @staticmethod
    def _normalize_name_list(
        value: Any,
        field: str,
    ) -> list[str]:
        if value is None:
            return []

        if not isinstance(value, list):
            raise TypeError(f"{field} must be a list of names.")

        if len(value) > MAX_LIST_ITEMS:
            raise ValueError(f"{field} accepts at most {MAX_LIST_ITEMS} entries.")

        normalized: list[str] = []
        seen: set[str] = set()

        for item in value:
            if not isinstance(item, str):
                raise TypeError(f"{field} entries must be strings.")

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

    @staticmethod
    def _validate_identifier(
        value: Any,
        field: str,
    ) -> str | None:
        if value is None:
            return None

        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string.")

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

    @staticmethod
    def _validate_text(
        value: Any,
        field: str,
        *,
        required: bool,
        max_chars: int,
    ) -> str | None:
        if value is None and not required:
            return None

        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string.")

        text = value.strip()

        if required and not text:
            raise ValueError(f"{field} must not be empty.")

        if len(text) > max_chars:
            raise ValueError(f"{field} exceeds the {max_chars}-character limit.")

        return text

    @staticmethod
    def _coerce_output_schema(
        value: Any,
    ) -> dict[str, Any] | None:
        schema, schema_error = coerce_output_schema(value)

        if schema_error:
            raise ValueError(f"output_schema invalid: {schema_error}")

        return schema

    def _load_skill(
        self,
        name: str,
        dispatch_kwargs: dict[str, Any],
    ) -> str:
        label = f"Skill {name!r}"

        def parse(raw: Any) -> dict[str, Any]:
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except (
                TypeError,
                json.JSONDecodeError,
            ) as exc:
                raise ValueError(f"{label} returned a malformed response.") from exc

            if not isinstance(payload, dict):
                raise TypeError(f"{label} returned a malformed response.")

            return payload

        payload = parse(
            self.ctx.dispatch_tool(
                "skill_view",
                {"name": name},
                **dispatch_kwargs,
            )
        )

        if (
            payload.get("success") is True
            and payload.get("dedup") is True
            and payload.get("content_returned") is False
            and "task_id" in dispatch_kwargs
        ):
            retry_kwargs = {
                key: value for key, value in dispatch_kwargs.items() if key != "task_id"
            }

            payload = parse(
                self.ctx.dispatch_tool(
                    "skill_view",
                    {"name": name},
                    **retry_kwargs,
                )
            )

        if payload.get("success") is not True:
            raise ValueError(
                f"Could not load skill {name!r}: "
                f"{payload.get('error') or 'skill_view failed'}"
            )

        content = payload.get("content")

        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"Skill {name!r} returned no content.")

        return f"### Skill: {name}\n\n{content.strip()}"

    def _build_skill_context(
        self,
        base_context: str | None,
        skill_names: Sequence[str],
        dispatch_kwargs: dict[str, Any],
        *,
        max_chars: int = MAX_SKILL_CHARS,
    ) -> str | None:
        if not skill_names:
            return base_context

        blocks: list[str] = []
        total = 0

        for name in skill_names:
            block = self._load_skill(
                name,
                dispatch_kwargs,
            )

            total += len(block)

            if total > max_chars:
                raise ValueError(
                    f"Requested skills exceed the "
                    f"{max_chars}-character injection limit."
                )

            blocks.append(block)

        framed = f"{_START}\n" + "\n\n".join(blocks) + f"\n{_END}"

        combined = f"{base_context}\n\n{framed}" if base_context else framed

        if len(combined) > MAX_TOTAL_CONTEXT_CHARS:
            raise ValueError(
                "Mission context plus skills exceeds Hermes' "
                f"{MAX_TOTAL_CONTEXT_CHARS}-character lifecycle limit."
            )

        return combined

    def _build_spec(
        self,
        params: dict[str, Any],
        display_name: str,
        explicit_parent: Any = None,
    ) -> "DelegateTaskAdvanced._LaunchSpec":
        goal = self._validate_text(
            params.get("goal"),
            "goal",
            required=True,
            max_chars=MAX_GOAL_CHARS,
        )

        assert goal is not None

        if len(f"Subagent '{display_name}' — {goal}") > MAX_GOAL_CHARS:
            raise ValueError(
                "goal is too long once the required display-name prefix is applied."
            )

        context = self._validate_text(
            params.get("context"),
            "context",
            required=False,
            max_chars=MAX_USER_CONTEXT_CHARS,
        )

        model = self._validate_identifier(
            params.get("model"),
            "model",
        )

        skills = tuple(
            self._normalize_name_list(
                params.get("skills"),
                "skills",
            )
        )

        if "toolsets" in params and params.get("toolsets") == []:
            raise ValueError(
                "toolsets cannot be an explicit empty list; "
                "omit it to inherit Hermes capabilities."
            )

        toolsets = tuple(
            self._normalize_name_list(
                params.get("toolsets"),
                "toolsets",
            )
        )

        output_schema = self._coerce_output_schema(params.get("output_schema"))

        parent = explicit_parent

        if parent is None:
            parent = get_active_subagent_parent()

        self._validate_toolsets(
            toolsets,
            parent,
        )

        parent_session_id = str(getattr(parent, "session_id", "") or "") or None

        return self._LaunchSpec(
            display_name=display_name,
            goal=goal,
            context=context,
            model=model,
            skills=skills,
            toolsets=toolsets,
            output_schema=output_schema,
            parent_session_id=parent_session_id,
        )

    def _prepare(
        self,
        spec: "DelegateTaskAdvanced._LaunchSpec",
        kwargs: dict[str, Any],
    ) -> SubagentLaunchRequest:
        dispatch_kwargs = {
            key: kwargs[key]
            for key in (
                "task_id",
                "session_id",
            )
            if kwargs.get(key) is not None
        }

        context = self._build_skill_context(
            spec.context,
            spec.skills,
            dispatch_kwargs,
        )

        if spec.output_schema is not None:
            context = append_output_contract(
                context,
                spec.output_schema,
            )

            if len(context) > MAX_TOTAL_CONTEXT_CHARS:
                raise ValueError(
                    "Mission context plus skills and "
                    "output_schema exceeds Hermes' "
                    f"{MAX_TOTAL_CONTEXT_CHARS}-character lifecycle limit."
                )

        correlation_id = f"dta-{uuid.uuid4().hex}"

        return SubagentLaunchRequest(
            goal=spec.wrapped_goal,
            context=context,
            model=spec.model,
            allowed_toolsets=(spec.toolsets or None),
            parent_session_id=(spec.parent_session_id),
            correlation_id=correlation_id,
            metadata={
                "source_tool": TOOL_NAME,
                "display_name": spec.display_name,
                "requested_skills": list(spec.skills),
                "requested_toolsets": list(spec.toolsets),
                "requested_model": spec.model,
                "parent_session_id": (spec.parent_session_id),
                "correlation_id": correlation_id,
            },
        )

    def _reserve(
        self,
        spec: "DelegateTaskAdvanced._LaunchSpec",
        request: SubagentLaunchRequest,
        state: _DeferredLaunch,
    ) -> dict[str, Any]:
        session_key = get_current_session_key("")
        origin_ui_session_id = get_session_env(
            "HERMES_UI_SESSION_ID",
            "",
        )
        dispatch = dispatch_async_delegation(
            goal=spec.wrapped_goal,
            context=request.context,
            toolsets=(list(spec.toolsets) or None),
            role="leaf",
            model=spec.model,
            session_key=session_key,
            parent_session_id=(spec.parent_session_id),
            runner=state.run,
            origin_ui_session_id=(origin_ui_session_id),
            origin_session_id="",
            interrupt_fn=state.cancel,
        )

        if dispatch.get("status") != "dispatched":
            raise RuntimeError(
                str(dispatch.get("error") or "Completion delivery rejected.")
            )

        try:
            live_id, writers, paths = create_live_transcripts(
                [
                    {
                        "goal": spec.wrapped_goal,
                        "context": request.context,
                    }
                ],
                request.context,
                delegation_id=dispatch.get("delegation_id"),
            )

            state.live_delegation_id = live_id
            state.writer = writers[0] if writers else None

            if paths:
                dispatch["live_transcript"] = paths[0]

        # Live transcript creation is optional observability. Do not reject a
        # successful dispatch when that auxiliary backend is unavailable.
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Could not create the live transcript: %s", exc)

        return dispatch

    def _launch(
        self,
        spec: "DelegateTaskAdvanced._LaunchSpec",
        request: SubagentLaunchRequest,
        state: _DeferredLaunch,
        dispatch: dict[str, Any],
    ) -> str:
        payload = {
            "success": True,
            "name": spec.display_name,
            "delegation_id": dispatch.get("delegation_id"),
            "toolsets": (list(spec.toolsets) if spec.toolsets else "inherited"),
            "skills": list(spec.skills),
            "correlation_id": (request.correlation_id),
            "live_transcript": dispatch.get("live_transcript"),
        }

        if not state.begin_launch():
            payload.update(
                status="interrupted",
                subagent_id=None,
                model=spec.model,
                effective_role=None,
                depth=None,
                note=("Cancellation won before the lifecycle child was launched."),
            )

            return tool_result(payload)

        handle = self.lifecycle.launch(request)

        state.set_handle(handle)

        payload.update(
            status="launched",
            subagent_id=handle.subagent_id,
            model=handle.model,
            effective_role=getattr(
                handle,
                "role",
                None,
            ),
            depth=getattr(
                handle,
                "depth",
                None,
            ),
            note=(
                "Hermes derived the subagent capabilities "
                "from the spawn depth. The result will "
                "re-enter this conversation through the "
                "normal background completion rail."
            ),
        )

        return json.dumps(
            payload,
            ensure_ascii=False,
        )

    @staticmethod
    def _validate_toolsets(
        toolsets: Sequence[str],
        parent_agent: Any,
    ) -> None:
        if not toolsets:
            return

        unknown = [name for name in toolsets if get_toolset(name) is None]

        if unknown:
            raise ValueError(f"Unknown toolsets: {', '.join(sorted(unknown))}.")

        enabled = getattr(
            parent_agent,
            "enabled_toolsets",
            None,
        )

        if enabled is not None and not set(toolsets).issubset(set(enabled)):
            raise ValueError(
                "Requested toolsets would broaden parent permissions. "
                "Enable the narrow toolset for the parent platform "
                "before selecting it."
            )
