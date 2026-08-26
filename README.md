# delegate-task-advanced

An additive Hermes Agent plugin that registers `delegate_task_advanced` without modifying the native `delegate_task` tool.

## Installation

```bash
hermes plugins install kev0ps/delegate-task-advanced --enable
```

Restart the gateway, then start a new session or use `/reset`.

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

## Hermes-governed orchestration

Advanced does not read, copy, or reimplement any depth logic. It passes `role="orchestrator"` to the public lifecycle, then returns `requested_role`, `effective_role`, and `depth` from the handle produced by Hermes.

```yaml
delegation:
  max_spawn_depth: 2
  orchestrator_enabled: true
```

With `max_spawn_depth: 1` or `orchestrator_enabled: false`, Hermes downgrades the child to `leaf`. With a depth of 2 or greater, the child can use native delegation within the limits enforced by Hermes. High depth or concurrency values can quickly multiply the number of agents and API calls; the plugin deliberately applies the same global settings without adding another limit.

## Choosing Advanced or `delegate_task`

`delegate_task_advanced` is not a general replacement for the native tool. It is intended for **one named child**, whose ability to delegate follows Hermes configuration, when the call needs at least one Advanced differentiator: explicit skill injection, child toolset selection, or a different model within the parent provider.

| Need | Recommended tool |
|---|---|
| One named child with skills, selected toolsets, a same-provider model, or globally governed orchestration | `delegate_task_advanced` |
| Simple delegation with no Advanced differentiator | `delegate_task` |
| Multiple tasks or a parallel batch | `delegate_task` |
| `output_schema` validation | `delegate_task` |
| `list`, `steer`, or `stop` controls | `delegate_task` |

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

## No custom child toolset in V1

V1 does not provide a read-only file lane. The child `toolsets` parameter selects a baseline from toolsets already provided by Hermes and enabled for the parent. This selection is not a boundary against delegation: when Hermes grants `orchestrator`, the runtime deliberately adds `delegation` even when that name was not requested.

The Hermes `file` toolset includes write and patch capabilities. Selecting it is **not** a read-only guarantee. An instruction telling the model not to modify anything remains an instruction, not a security boundary. Real read-only isolation must rely on a future public and auditable API or toolset; this plugin does not simulate it through registry mutation, private proxies, or monkey-patching.

## Hermes v0.20.5 public API limitations

This plugin deliberately remains plugin-only: it does not modify `/opt/hermes`, import private registries, or monkey-patch the runtime.

- Hermes validates `SubagentLaunchRequest.metadata`, but does not yet preserve it in the lifecycle handle or registry. The plugin therefore keeps the display name, skills, toolsets, and correlation data in the launch acknowledgement and available public surfaces.
- The public API does not expose the child progress callback. The live transcript created by the plugin contains its header and terminal result, but not the detailed stream of child tool calls. Activity and the latest tool remain visible through the shared Hermes registry.
- `SubagentLaunchRequest` exposes `model`, but not `provider`. An `openai-codex` parent can therefore launch `gpt-5.6-luna` instead of `gpt-5.6-sol`, but cannot use this path to launch an OpenRouter or Anthropic model.
- The async completion event records the requested orchestrator role. The launch acknowledgement reports the effective role and depth returned by Hermes, including any downgrade to `leaf` caused by global configuration.

These limitations are preferable to fragile workarounds around private APIs.

## Compatibility

Version `1.0.0` targets the public plugin API available in Hermes Agent `v0.20.5`. Later Hermes versions may evolve the public subagent lifecycle.

## License

Distributed under the [MIT License](LICENSE).
