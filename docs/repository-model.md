# Repository model

Play Trace and the games it analyses are separate products with separate Git
histories. Every game has its own repository, including tabletop games whose
canonical rules and content live in Google Drive.

## Shared Play Trace repository

This repository owns only reusable system concerns:

```text
playtrace/
  src/                 # shared reasoning kernel and application
  schemas/             # shared record and interface schemas
  grammar/             # reusable controlled-language infrastructure
  policy/              # genuinely cross-game policy
  cases/               # explicitly promoted cross-game precedents
  plugins/             # reusable analysis and adapter plugins
  tests/
    synthetic/         # invented fixtures, never a real game's working state
  docs/
```

It does not own a game's design intent, local policy, evidence, experiments,
fixture snapshots, or regression cases.

## Per-game repository

Every game repository owns its implementation, where applicable, plus all
game-specific Play Trace material:

```text
crisis-wardens/
  playtrace.toml       # project identity, capabilities, and source locators
  design/
    intent/            # intended experience, constraints, and non-goals
    policy/            # game-local policy and explicit shared-policy overrides
    assertions/        # curated assertions and stable design IDs
    evidence/          # irreplaceable observations and evidence metadata
    cases/             # game-specific decisions and precedents
    experiments/       # hypotheses, protocols, and retained results
  tests/
    fixtures/          # snapshots owned by this game
    regressions/       # approved game-specific behavior
```

A video-game repository will also contain its code, content, assets, telemetry,
and executable tests. A tabletop repository need not duplicate its Google Docs
or Sheets. It records their stable provider IDs, roles, and authority in project
configuration or a canonical Drive manifest, then stores only game-owned design
knowledge and reproducible fixtures in Git.

## Runtime working state

Indexes, normalized snapshots, caches, pending workflows, and bulky reproducible
outputs are disposable runtime state. They belong in a configured local data
directory or ignored `.playtrace/` directory associated with the active game,
not as committed content in the Play Trace repository.

Deleting runtime state must affect neither repository nor any canonical Drive
or game source.

## Boundary contract

Play Trace opens a game repository and reads `playtrace.toml`. The project
configuration selects adapters and declares capabilities. Those adapters create
a `DesignSnapshot` whose records retain both:

- the game repository commit that supplied game-specific knowledge; and
- the exact revisions of canonical external sources used for the snapshot.

An analysis therefore identifies at least:

```text
Play Trace commit
+ game repository commit
+ canonical source revisions
+ adapter/plugin versions
+ analysis configuration
```

The shared application may return proposed game-specific files or semantic
source patches, but they are reviewed and applied to the game repository or its
declared canonical source—not committed to Play Trace.

## Crisis Wardens boundary

Crisis Wardens remains the first integration pilot, but its baseline inventory,
stable design IDs, curated conflicts, assertions, evidence links, and M1 fixture
belong in a Crisis Wardens repository. Its Google Docs and Sheets remain the
canonical tabletop artefacts. This repository contains only the generic
contracts required to load and reason about that external project.

Creating or locating the Crisis Wardens repository is therefore a prerequisite
for the M1 pilot; copying the material into `playtrace/tests` is not an
acceptable substitute.
