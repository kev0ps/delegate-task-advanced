# delegate-task-advanced

An additive Hermes Agent plugin that registers `delegate_task_advanced` without modifying the native `delegate_task` tool.

## Installation

```bash
hermes plugins install kev0ps/delegate-task-advanced --enable
```

Restart the gateway, then start a new session or use `/reset`.

## What it does

`delegate_task_advanced` launches one named Hermes subagent with per-call skill injection, selected child toolsets, and an optional model from the parent provider. It uses Hermes' native subagent lifecycle and delegation settings, while leaving the existing `delegate_task` tool unchanged.

Use it when a task needs a specialized child agent—for example, a named code reviewer with a review skill, a specific model, and a focused set of toolsets.

## V1 contract

```json
{
  "name": "Relay reviewer",
  "goal": "Review the public diff and report regressions",
  "context": "The test repository contains no secrets.",
  "model": "gpt-5.6-luna",
  "skills": ["requesting-code-review"],
  "toolsets": ["file"]
}
```

- `name` and `goal` are required.
- `skills` are loaded through the public `skill_view` tool, deduplicated, capped at 20,000 characters, and injected into the child context.
- `toolsets` is an optional baseline selection from existing Hermes toolsets that are already enabled for the parent. Omitting it uses normal Hermes inheritance. If Hermes grants the `orchestrator` role, it then adds `delegation` regardless of this selection so the child can spawn within the globally configured depth.
- `model` is optional. When omitted, the child inherits the parent model. When provided, it is passed to the public lifecycle and must be compatible with the parent provider.
- The plugin does not create, extend, or modify any Hermes toolset.
- The provider cannot be selected per call and remains the parent provider. The plugin always requests the `orchestrator` role; Hermes alone decides the effective role and depth from `delegation.max_spawn_depth` and `delegation.orchestrator_enabled`. Role and depth cannot be selected per call.
- Reasoning follows native Hermes behavior: `delegation.reasoning_effort` when configured, otherwise the parent’s effective reasoning level. It cannot be selected per call.
- Launches use `ctx.subagent_lifecycle`, so state, activity, and active controls use the same shared registry as `delegate_task`.
- Final results use the existing `async_delegation` completion queue.
- Completion delivery is reserved **before** launch, so a capacity rejection cannot leave an orphaned child.
- The plugin adds no depth or fan-out ceiling. Configuration and cost remain the user’s responsibility, as with `delegate_task`.

## Advanced vs native `delegate_task`

`delegate_task_advanced` is not a general replacement for the native tool. Use it when one focused child needs at least one Advanced differentiator: a display name, explicit skill injection, selected baseline toolsets, or a same-provider model override.

| Capability | Native `delegate_task` | `delegate_task_advanced` |
|---|---|---|
| Primary use | General delegation, orchestration, and validated review | One named, specialized child |
| Single child | Yes | Yes |
| Human-readable display name | No dedicated field | Yes — `name` |
| Per-call skill injection | No | Yes — `skills` |
| Per-call toolset selection | No | Yes — `toolsets` |
| Per-call model selection | No | Yes — `model`, within the parent provider |
| Per-call provider selection | No | No |
| Parallel batch with `tasks` | Yes, with consolidated completion | No; use separate Advanced calls |
| Validated `output_schema` | Yes | No |
| Per-call role selection | `leaf` or `orchestrator` | No; the plugin requests `orchestrator` and Hermes decides the effective role |
| Per-call depth selection | No | No |
| Depth limit | Global `delegation.max_spawn_depth` | Global `delegation.max_spawn_depth` |
| `list`, `steer`, and `stop` | Yes | Yes, through the shared native subagent registry |
| Completion delivery | Standard async completion queue | Same standard async completion queue |
| Read-only guarantee from `file` | No — `file` also includes write and patch tools | No — toolset selection is not per-tool sandboxing |

Use native `delegate_task` for simple delegation, batches, explicit role selection, or fail-closed structured review. Use Advanced for a single child whose name, injected skills, baseline toolsets, or same-provider model materially improve the mission.

An Advanced call immediately returns a launch acknowledgement. The final result later returns through the Hermes completion queue; do not wait or poll after launch.

Advanced example:

```json
{
  "name": "Luna reviewer",
  "goal": "Review the project and return findings ordered by severity",
  "context": "Project: /opt/data/workspace/example. Respond in English.",
  "model": "gpt-5.6-luna",
  "skills": ["requesting-code-review"],
  "toolsets": ["file"]
}
```

This example is not technically read-only: `file` also contains write and patch capabilities.

Native batch example:

```json
{
  "tasks": [
    {"goal": "Analyze security risks"},
    {"goal": "Analyze logic regressions"}
  ]
}
```

## Parent toolset

The plugin registers its tool in the existing Hermes `delegation` toolset. When that toolset is enabled for a platform, it exposes both tools:

```text
delegation = delegate_task + delegate_task_advanced
```

Do not add `delegate_task_advanced` directly to `platform_toolsets.*`. Those keys contain **toolset names**, not tool names. Enabling `delegation` is sufficient.

## Compatibility

Version `1.0.0` targets the public plugin API available in Hermes Agent `v0.20.5`. Later Hermes versions may evolve the public subagent lifecycle.

## License

Distributed under the [MIT License](LICENSE).
