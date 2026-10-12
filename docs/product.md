# Product definition

## Vision

Play Trace is a design critic and design memory for games in development. It
helps a designer make evidence-backed judgments while keeping every important
conclusion connected to its sources, assumptions, uncertainty, and downstream
consequences.

It is not an autonomous designer and it is not a new canonical editor for every
kind of game. The designer continues to work in the game's natural tools. Play
Trace builds a derived view for analysis and review.

Each game has its own Git repository. That repository owns the game's Play Trace
configuration and all game-specific durable design knowledge. The shared Play
Trace repository owns only the product, general schemas, common policy,
cross-game precedents, and reusable tooling.

## Initial user

The initial user is a game designer developing one or more games across native
documents and structured content. They need to:

- recover what has actually been decided;
- distinguish commitments from working ideas and temporary scaffolding;
- detect disagreement between artefacts;
- connect playtest observations and experiments to design questions;
- assess a change without losing its rationale;
- approve proposals before canonical material changes; and
- carry useful design learning forward explicitly rather than through hidden
  conversational memory.

## Core promise

For any material design conclusion, Play Trace should show:

- the claim being made;
- the sources and revisions it depends on;
- evidence that supports, contradicts, or contextualises it;
- assumptions and known gaps;
- whether any part is current judgment rather than evidence;
- the entities and prior conclusions a change may affect; and
- the human decision that accepted, rejected, or deferred it.

## Product principles

### Canon stays native

Tabletop games may live in Google Docs and Sheets. Video games may live in Git,
code, content, builds, and assets. Play Trace does not turn an export or internal
database into a competing editable truth.

### Games own their design memory

Game-specific intent, policy, assertions, evidence, experiments, precedents,
fixtures, and regression tests live in that game's repository. The shared Play
Trace repository may contain synthetic tests, but never a real game's working
design state as a convenient fixture.

### A snapshot is a view

`DesignSnapshot` is the intentionally incomplete set of facts and relationships
the current system can reason about. Missing representation is visible. It is
never evidence that the underlying game lacks the omitted concept.

### Uncertainty is data

`CONFIRMED`, `WORKING`, `PLACEHOLDER`, `OPEN QUESTION`, and `DEFERRED` express
different design states. Preserving them is more useful than manufacturing one
confidence percentage.

### Evidence attaches to claims

A reading list is not an evidence model. Each material claim identifies how a
source bears on that claim. Unsupported design judgment remains allowed when it
is labelled honestly.

### Deterministic work is software

Identity, parsing, schema checks, dependency propagation, revision checks, and
invariants should be deterministic wherever practical. Models are used for
interpretation, hypothesis generation, criticism, and explanation.

### Propose, review, then apply

Analysis may end with observations. A proposed design change is a separate
object. Approval of a design direction does not silently grant permission for
unrelated edits or durable policy changes.

### Evidence informs; people decide

Simulation, telemetry, playtest notes, precedents, and critics can disagree.
Play Trace preserves the disagreement and helps a human make the call.

## Non-goals

Play Trace is not intended to:

- define a universal formal language for all games;
- reduce fun or taste to a score;
- infer durable policy from ordinary requests;
- make simulation the arbiter of player experience;
- average specialist disagreement into false consensus;
- rewrite canonical sources as a side effect of analysis; or
- require a finished, internally consistent game before it becomes useful.

## Why Crisis Wardens is the first pilot

Crisis Wardens has enough structure to trace—stable card IDs, named rules,
keywords, elements, and explicit status markers—but remains incomplete enough
to expose the real problem. Its current sources disagree in places, mix rules
with hypotheses, and contain deliberately provisional values. A system that can
help the designer improve that game without laundering uncertainty into fact
has proved something useful.

The pilot is exercised from the Crisis Wardens repository. Only general lessons
that are explicitly promoted and reviewed may move into Play Trace's shared
policy, cases, schemas, or plugins.
