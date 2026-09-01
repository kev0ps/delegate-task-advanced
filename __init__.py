"""Hermes plugin: delegate-task-advanced — public interface and registration."""

from __future__ import annotations

from .plugin import TOOL_NAME, SCHEMA, make_handler


def register(ctx) -> None:
    ctx.register_tool(
        name=TOOL_NAME,
        toolset="delegation",
        schema=SCHEMA,
        handler=make_handler(ctx),
        check_fn=lambda: True,
        description=(
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
        ),
        emoji="🧭",
    )
