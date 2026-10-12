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

Native [Google Drive and Sheets support](docs/google-sheets.md) now includes
explicit single-editor application of approved literal-cell patches, verified
readback and journaled recovery. Table row provenance and cache range expansion
are checked. Formula cells remain read-only to this patch path.

A first [local Unreal source and telemetry pilot](docs/unreal.md) supports
`snapshot --local`, `validate-telemetry` and hypothesis-linked
`record-experiment --telemetry`. Dread Meridian supplies the game-owned combat
sandbox; Play Trace preserves source uncertainty, provenance and omitted semantics.
This does not add full-game simulation or native-asset patching.

**M6: local-first production hardening is implemented.** Versioned SQLite
migrations, authoritative revision-aware Workspace caching, durable canonical
write sagas, typed bounded critic fallback, a loopback-only unified review API,
and a pinned simulation-container contract make the M0–M5 workflow safer to run
locally. See [`docs/M6.md`](docs/M6.md) for operating commands and boundaries.

**M5: approval and durable learning are implemented.** Typed proposals bind the
complete evidence/policy/snapshot basis to a digest, keep immutable decision and
application history, and separate human approval from canonical application.
Approved changes are preflighted, rebuilt, and checked against postconditions
and behavioral regressions. Only successfully applied proposals can be promoted
into game-owned precedent; generated engine extensions remain inert review
artefacts.

M4's tabletop compiler and semantic strictness boundary are implemented.
Exact Improve, Worsen, and cube Transfer rules can execute from typed RuleIR;
selector phrases require an explicit game-owned resolver. Plugins are resolved
through a registry, scripted policies never invent fallback actions, and every
new run inventories compiled, extension, provisional, and missing semantics.
Strict runs reject the latter two categories before play.

M3's experiment evidence loop records
simulation runs, tension batches, and paired A/B comparisons as typed evidence
linked to an explicit hypothesis. It preserves assumptions and reproducibility
provenance, marks evidence stale when dependency revisions or digests change,
and records a separate human review decision without changing canonical game
files.

An initial deterministic [tabletop simulation layer](docs/simulation.md) now
turns supported snapshot semantics into replayable traces. It emits typed
blockers instead of inventing missing rules and links every result to an
evidence claim with reconstructable provenance.

- [M5 milestone](docs/M5.md)
- [M4 milestone](docs/M4.md)
- [M3 milestone](docs/M3.md)
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

Run the complete M6 release gate with `uv run python scripts/check_release.py`.
It also verifies the built wheel and runs in CI on Windows and Linux.

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

To carry a result into review, run `record-experiment` with an explicit
`supports`, `contradicts`, `context`, or `judgment` relationship; pass its
bundle to `assess --experiment-evidence`; then use `decide-review` to record the
human decision. The detailed commands, lifecycle, staleness rules, and authority
boundary are in [the M3 milestone](docs/M3.md).

To diagnose whether Crisis Wardens can produce a strict trace:

```powershell
uv run playtrace simulate C:\AnalogueGames\crisiswardens `
  --fixture C:\AnalogueGames\crisiswardens\tests\fixtures\workspace.json `
  --config C:\AnalogueGames\crisiswardens\design\simulation\strict-first-trace.json
```

The M5 proposal flow uses distinct commands for `propose`, `submit-proposal`,
`decide-proposal`, `dry-run-proposal`, and `apply-proposal`. Fixtures support
dry-runs; application requires a live writable Workspace connection. Successful
applications may then be promoted explicitly with `promote-precedent` or used
to accept an approved-behavior case through `accept-regression-case`. Accepted
cases are reverified before `run-regressions`. See [M5](docs/M5.md) for the
authority and failure boundaries.
