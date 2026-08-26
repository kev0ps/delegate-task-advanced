"""Load explicitly requested skills through Hermes' public tool dispatch."""

from __future__ import annotations

import json
from typing import Callable, Optional

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
