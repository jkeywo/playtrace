# Tabletop simulation layer

Play Trace can project a supported subset of a `DesignSnapshot` into a
deterministic tabletop trace. The trace is evidence about a declared model; it
is never treated as a canonical game rule.

## Safety boundary

Compilation stops with typed blockers when a missing, ambiguous, placeholder,
or unsupported rule would change executable behaviour. An experiment may supply
an override, but every override records a rationale and whether it is canonical.
The resulting evidence claim carries those assumptions.

Each run records the snapshot and configuration digests, backend and plugin
versions, player policy, seeds, runner, and (for container runs) the pinned image
digest. The run ID and event stream are deterministic for the same inputs.

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
