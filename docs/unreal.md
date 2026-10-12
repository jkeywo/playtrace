# Local Unreal source and telemetry pilot

Play Trace now supports read-only local Unreal manifests alongside the existing
Workspace/tabletop path. Use `manifest_version = 2`, `adapter = "unreal-local"`,
project identity, `engine_version` and explicit repository-relative sources with
`id`, `path`, `kind` and `role`. Local manifests need no Drive fields.

Kinds: `markdown`, `unreal_project`, `text`, `binary`. Only Markdown produces
verbatim assertions. `role = "design"` identifies one primary GDD for the capture
contract. Native assets and code are inventories, not executable semantics.

```powershell
uv run playtrace snapshot C:\Coding\dread-meridian --local `
  --db C:\Coding\dread-meridian\.playtrace\playtrace.db `
  --output C:\Coding\dread-meridian\.playtrace\snapshot.json
uv run playtrace validate-telemetry path\to\events.jsonl
uv run playtrace record-experiment C:\Coding\dread-meridian `
  --snapshot C:\Coding\dread-meridian\.playtrace\snapshot.json `
  --hypothesis path\to\hypothesis.json --telemetry path\to\events.jsonl `
  --relation context --rationale "Describe why the observation matters."
```

Sources retain Git revision, current file/repository SHA-256, source anchors and
explicit authored uncertainty. Fragment IDs survive unrelated line insertions.
Required missing sources, escaping paths and changing inputs fail. Optional
missing sources become omissions. Working-tree fingerprints include uncommitted,
nonignored inputs; digests identify bytes but cannot reconstruct them.

JSONL ingestion validates the versioned server/developer envelope, single-run
lifecycle/sequence/timing and shared combat health transitions. Malformed,
incomplete and unsupported combat events fail. It never executes game code.
Victory, defeat and aborted runs remain distinct.

Imported `telemetry_run` records use existing hypothesis, claim and SQLite
persistence. Raw/config digests, run kind, engine and semantic omissions stay
explicit. Captured repository/design/engine dependencies are assessed against
the current snapshot; changes or unrecorded provenance are stale. Import does
not approve a hypothesis or establish full-game balance/simulation validity.

No C++/Blueprint semantic interpretation, native-asset patching or whole-game
deterministic replay is claimed. Tests use synthetic repositories/events; real
consumer GDDs and observations stay in the game repository.
