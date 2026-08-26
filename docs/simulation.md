# Tabletop simulation layer

Play Trace can project a supported subset of a `DesignSnapshot` into a
deterministic tabletop trace. The trace is evidence about a declared model; it
is never treated as a canonical game rule.

## Safety boundary

Compilation stops with typed blockers when a missing, ambiguous, placeholder,
or unsupported rule would change executable behaviour. An experiment may supply
an override, but every override records a rationale and the game plugin must
register the key and its semantic origin. A caller's `canonical` flag does not
promote an unknown or experimental semantic into a plugin extension. The
resulting evidence claim carries those assumptions.

Each run records the snapshot and configuration digests, backend and plugin
versions, player policy, seeds, runner, and (for container runs) the pinned image
digest. The run ID and event stream are deterministic for the same inputs.

Override values cross a bounded strict-JSON boundary at construction. `value` is a
typed JSON value: null, boolean, safe integer, finite float, valid Unicode string,
array, or string-keyed object, recursively. Meaning is preserved with canonical
object-key ordering; integers are limited to JavaScript's exactly representable
range (`-9007199254740991` to `9007199254740991`). Values are capped at 32 levels,
10,000 nodes, and 262,144 compact serialized UTF-8 bytes. The validator counts nodes
and exact encoded size incrementally, including JSON escapes, before allocating a
serialized copy.

Python-only objects, sets, custom mappings, cycles, invalid Unicode, and excessive
values are outside the simulation contract. They raise a structured Pydantic
validation error and never become a `SimulationRun`. The validator does not inspect
their representation, contents, iterator, or type metadata. Valid JSON with a value
the game does not support is different: the plugin returns an
`invalid_configuration` blocker explaining the semantic correction required.

CLI configuration and isolated-worker request files additionally use a bounded JSON
loader. It checks the file size before reading, preflights nesting and node count
before recursive decoding, rejects oversized integer tokens before arbitrary-precision conversion,
and rejects non-finite numbers and malformed Unicode as input errors. The whole
boundary permits at most 16 MiB, 64 levels, and 250,000 nodes; individual override
limits still apply. Public Python callers must create `SimulationConfig` through its
normal constructor or `model_validate`; both routes produce structured Pydantic errors
for invalid values. The runners check the top-level type but do not reconstruct nested
models.

Pydantic's `model_construct()` bypasses validation by design. Configurations or nested
object graphs made with it are trusted-internal objects and are outside the runner's
public input contract; plugins and runners are not required to sanitize them. External
CLI and worker data never uses that path: bounded JSON decoding is followed by
`SimulationConfig.model_validate`. Docker creation also applies the worker's aggregate
byte, depth, and node budgets to the complete validated request before serialization.

Depth is counted by container nesting. A root object or array has depth one and a
scalar document has depth zero, so every in-memory validator, raw scanner, Docker
payload check, and worker loader accepts `N` nested containers and rejects `N + 1`.

Each new run also carries a semantic audit distinguishing compiled RuleIR,
registered plugin extensions, provisional overrides, and missing or ambiguous
semantics. A compiled entity is classified as compiled only when its typed RuleIR,
entity ID, and matching `compiles_to` assertion agree; malformed or contradictory
records remain missing/ambiguous. Set `strict_semantics` in the configuration to
reject the latter two categories. See [M4](M4.md) for the classification and
compatibility contract.

## Shared executable subset

`ExactRuleExecutor` executes exact-target `IMPROVE`, `WORSEN`, and cube
`TRANSFER` RuleIR. A phrase such as `one Hazard` is not silently turned into a
choice: the game must provide a deterministic selector, or execution returns a
typed ambiguity blocker. Crisis Wardens uses this shared state-change primitive
while retaining named plugin extensions for scenario and responder semantics.

The CLI and container worker select simulation plugins from a registry by
project or plugin ID. External packages can use the
`playtrace.simulation_plugins` entry-point group.

For exact player replays, choose `player_policy: "scripted"` and provide typed
`scripted_steps`. A step must name the exact legal action for the active actor
and turn. Illegal or exhausted scripts block; no heuristic policy is used as a
fallback.

## Crisis Wardens projection

The initial plugin models scenario setup, the escalation track, scenario-card
draw and discard, Worsen, the currently specified scenario events, threshold
checks, win/loss termination, semantic events, invariants, and aggregate
metrics. Player choices are represented by an explicit policy; the passive
policy is only allowed when an experiment declares that assumption.

Ruleset `cw:rules:provisional-core-v0.2` adds the designer-authorized complete
Crisis Wardens loop: one or two responder decks, three-card hands, activation,
AP, persistent prerequisite Elements, Rest, Fatigue, Assists, powers, threshold
timing, escalation modifiers, and provisional placeholder rulings. The ruleset
identifier is included in run identity and repeated as an evidence assumption.
An explicitly supplied `starting_cubes` map is audited as executed when scenario
setup consumes it, including under the provisional ruleset. The implicit provisional
zero-cube default does not invent an override usage, and a map rejected before setup
remains present but unexecuted in the audit.

Run a game-owned experiment from the Play Trace repository:

```powershell
uv run playtrace simulate C:\AnalogueGames\crisiswardens `
  --fixture C:\AnalogueGames\crisiswardens\tests\fixtures\workspace.json `
  --config C:\AnalogueGames\crisiswardens\design\simulation\strict-first-trace.json
```

The default in-process runner is intended for development. The Docker runner
requires both `--image` and a `sha256:` `--image-digest`; it disables networking,
uses a read-only root filesystem and input mount, and bounds memory, CPU, PIDs,
and wall-clock time.
