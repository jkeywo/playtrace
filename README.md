# Play Trace

Play Trace is an evidence-backed game-design workspace. It helps a designer turn
changing rules, content, playtest observations, experiments, and judgments into
traceable design claims without replacing the files where the game is actually
made.

The project is starting with tabletop games. Its first pilot is **Crisis
Wardens**, an early cooperative card-game design whose Google Docs and Sheets
currently mix commitments, working rules, placeholders, open questions, and
prototype content. That messiness is intentional: Play Trace must be useful
while a game is being discovered, not only after it has become a specification.

## Current milestone

**M2: the taste/evidence system is implemented.** Play Trace now resolves
layered policy, retrieves approved precedents by scope, treats hypotheses and
experiential evidence as typed records, deterministically validates mandatory
coverage, routes specialist critics, persists complete reviews, and renders the
claim-linked argument in the review shell.

An initial deterministic [tabletop simulation layer](docs/simulation.md) now
turns supported snapshot semantics into replayable traces. It emits typed
blockers instead of inventing missing rules and links every result to an
evidence claim with reconstructable provenance.

- [M2 milestone](docs/M2.md)
- [M1 milestone](docs/M1.md)
- [M0 milestone](docs/M0.md)
- [Product definition](docs/product.md)
- [Domain model](docs/domain-model.md)
- [Reasoning workflow](docs/workflow.md)
- [Repository model](docs/repository-model.md)
- [Decision register](docs/decisions.md)

## The short version

1. A project's native artefacts remain canonical.
2. Play Trace records an intentionally incomplete, derived `DesignSnapshot`.
3. Assertions preserve what a source says, including the source's uncertainty.
4. Claims link conclusions to supporting, contradicting, or contextual evidence.
5. Proposals are reviewed before anything canonical changes.
6. Simulation and criticism produce evidence; neither is treated as authority.

Every game has its own Git repository. This repository contains the shared Play
Trace application, schemas, general policy, and reusable plugins; it does not
store game-specific design state or use a real game's files as checked-in test
fixtures.

## Run Play Trace

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Node.js for the
JavaScript syntax check.

```powershell
uv sync --extra dev
uv run pytest
uv run playtrace demo
uv run playtrace review
```

To replay the Crisis Wardens M1 capture:

```powershell
uv run playtrace snapshot C:\AnalogueGames\crisiswardens `
  --fixture C:\AnalogueGames\crisiswardens\tests\fixtures\workspace.json `
  --db C:\AnalogueGames\crisiswardens\.playtrace\playtrace.db `
  --output C:\AnalogueGames\crisiswardens\.playtrace\snapshot.json
```

For a live snapshot, replace `--fixture ...` with
`--access-token-env PLAYTRACE_GOOGLE_TOKEN`; the environment variable must hold
a Google OAuth bearer token with read access to the manifest and its sources.

To generate the current Crisis Wardens design review:

```powershell
uv run playtrace assess C:\AnalogueGames\crisiswardens `
  --fixture C:\AnalogueGames\crisiswardens\tests\fixtures\workspace.json `
  --intent C:\AnalogueGames\crisiswardens\design\intent\first-playable-scenario.json `
  --shared-root C:\Coding\playtrace

uv run playtrace review `
  --file C:\AnalogueGames\crisiswardens\.playtrace\review.json
```

The review shell is then available at `http://127.0.0.1:8765`.

Tension experiments run identical seeds for Orra, Senn, and the pair, then
compare a candidate rule against its baseline:

```powershell
uv run playtrace simulate-batch C:\AnalogueGames\crisiswardens `
  --fixture C:\AnalogueGames\crisiswardens\tests\fixtures\workspace.json `
  --config cases\crisis-wardens-tension-baseline.json --seeds 1-30 `
  --id baseline --output baseline.json
uv run playtrace compare-batches --baseline baseline.json `
  --candidate candidate.json --output comparison.json
uv run playtrace tension-report --baseline baseline.json `
  --candidate candidate.json --output tension-report.html
```

The danger heuristic and flow targets are documented in
[`docs/tension.md`](docs/tension.md).

To diagnose whether Crisis Wardens can produce a strict trace:

```powershell
uv run playtrace simulate C:\AnalogueGames\crisiswardens `
  --fixture C:\AnalogueGames\crisiswardens\tests\fixtures\workspace.json `
  --config C:\AnalogueGames\crisiswardens\design\simulation\strict-first-trace.json
```
