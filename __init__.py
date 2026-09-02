"""Hermes plugin entry point."""

from __future__ import annotations

from .plugin import DelegateTaskAdvanced


def register(ctx) -> None:
    tool = DelegateTaskAdvanced(ctx)

    ctx.register_tool(
        name=tool.name,
        toolset=tool.toolset,
        schema=tool.schema,
        handler=tool.handle,
        check_fn=lambda: True,
        description=tool.description,
        emoji=tool.emoji,
    )
