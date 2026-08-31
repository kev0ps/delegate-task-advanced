# delegate-task-advanced

An additive Hermes Agent plugin that registers `delegate_task_advanced` without modifying the native `delegate_task` tool.

## Installation

```bash
hermes plugins install kev0ps/delegate-task-advanced --enable
```

Restart the gateway, then start a new session or use `/reset`.

## Advanced vs native `delegate_task`

`delegate_task_advanced` is not a general replacement for the native tool. Use it when one focused child needs an Advanced differentiator: a display name, explicit skill injection, selected baseline toolsets, a same-provider model override, a role, or a validated output contract.

| Capability | Native `delegate_task` | `delegate_task_advanced` |
|---|---|---|
| Primary use | General delegation, orchestration, and validated review | One named, specialized child |
| Single child | ✅ | ✅ |
| Human-readable display name | ❌ | ✅ `name` |
| Per-call skill injection | ❌ | ✅ `skills` |
| Per-call toolset selection | ❌ | ✅ `toolsets` |
| Per-call model selection | ❌ | ✅ `model` |
| Per-call provider selection | ❌ | ❌ |
| Parallel batch with `tasks` | ✅ | ❌ |
| Validated `output_schema` | ✅ | ✅ |
| Per-call role selection (`leaf`/`orchestrator`) | ✅ | ✅ |
| Per-call depth selection | ❌ | ❌ |
| Depth limit (global `delegation.max_spawn_depth`) | ✅ | ✅ |
| `list`, `steer`, and `stop` | ✅ | ✅ |

Use native `delegate_task` for simple delegation, parallel batches, or live control actions. Use Advanced when a human-readable name, injected skills, baseline toolsets, a same-provider model override, role selection, or validated output materially improves one focused mission. Launch multiple Advanced children with separate calls when manual sequencing is preferable.

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

Manual sequential launches use separate Advanced calls, each with a complete contract. The plugin intentionally provides no `tasks` field and no batch aggregation.

## Contract

```json
{
  "name": "Relay reviewer",
  "goal": "Review the public diff and report regressions",
  "context": "The test repository contains no secrets.",
  "model": "gpt-5.6-luna",
  "role": "leaf",
  "skills": ["requesting-code-review"],
  "toolsets": ["file"],
  "output_schema": {
    "type": "object",
    "properties": {"findings": {"type": "array"}},
    "required": ["findings"]
  }
}
```

- `name` and `goal` are required.
- `skills` are loaded through the public `skill_view` tool, deduplicated, capped at 20,000 characters, and injected into the child context.
- `toolsets` is an optional baseline selection from existing Hermes toolsets that are already enabled for the parent. Omitting it uses normal Hermes inheritance. If Hermes grants the `orchestrator` role, it then adds `delegation` regardless of this selection so the child can spawn within the globally configured depth.
- `model` is optional. When omitted, the child inherits the parent model. When provided, it is passed to the public lifecycle and must be compatible with the parent provider.
- The plugin does not create, extend, or modify any Hermes toolset.
- The provider cannot be selected per call and remains the parent provider. `role` accepts `leaf` or `orchestrator` and defaults to `orchestrator` for backward compatibility. Hermes alone decides the effective role and depth from `delegation.max_spawn_depth` and `delegation.orchestrator_enabled`. Depth cannot be selected per call.
- `output_schema` is meta-validated before launch, injected into the child context, and used to validate the final answer. Invalid output receives exactly one bounded correction retry through a fresh public-lifecycle child; the result reports `schema_valid`, `schema_retries`, and final `schema_errors` when applicable.
- Reasoning follows native Hermes behavior: `delegation.reasoning_effort` when configured, otherwise the parent’s effective reasoning level. It cannot be selected per call.
- Launches use `ctx.subagent_lifecycle`, so state, activity, and active controls use the same shared registry as `delegate_task`.
- Final results use the existing `async_delegation` completion queue.
- Completion delivery is reserved **before** launch, so a capacity rejection cannot leave an orphaned child.
- The plugin adds no depth ceiling. Configuration and cost remain the user’s responsibility, as with `delegate_task`.

## Parent toolset

The plugin registers its tool in the existing Hermes `delegation` toolset. When that toolset is enabled for a platform, it exposes both tools:

```text
delegation = delegate_task + delegate_task_advanced
```

Do not add `delegate_task_advanced` directly to `platform_toolsets.*`. Those keys contain **toolset names**, not tool names. Enabling `delegation` is sufficient.

## Compatibility

Version `1.1.0` targets the public plugin API available in Hermes Agent `v0.20.5`. Later Hermes versions may evolve the public subagent lifecycle.

## License

Distributed under the [MIT License](LICENSE).
