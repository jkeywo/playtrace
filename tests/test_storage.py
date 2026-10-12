from playtrace.adapters import SyntheticProjectAdapter
from playtrace.models import (
    EvidenceClaim,
    EvidenceLink,
    EvidenceRelation,
    SourceMedium,
)
from playtrace.storage import PlayTraceStore
from playtrace.reviewing import Phase2ReviewEngine
from playtrace.models import Intent, IntentKind
from playtrace.plugins.crisis_wardens import CrisisWardensSimulationPlugin
from playtrace.simulation import InProcessSimulationRunner, SimulationConfig, SimulationOverride
from test_simulation import _snapshot


def test_sqlite_round_trip_and_fts5_search(tmp_path) -> None:
    snapshot = SyntheticProjectAdapter(SourceMedium.SYNTHETIC).snapshot()
    claim = EvidenceClaim(
        id="claim:pressure",
        statement="Pressure cadence requires scaling evidence.",
        scope=("synthetic:rule:pressure",),
        evidence=(
            EvidenceLink(
                evidence_id="synthetic:assertion:cadence",
                relation=EvidenceRelation.CONTEXT,
                rationale="It is currently a working assertion.",
            ),
        ),
    )

    with PlayTraceStore(tmp_path / "playtrace.db") as store:
        store.save_snapshot(snapshot)
        store.save_claim(snapshot.id, claim)

        loaded = store.load_snapshot(snapshot.id)
        results = store.search("pressure")

    assert loaded == snapshot
    assert {result["record_id"] for result in results} == {snapshot.id, claim.id}


def test_design_review_round_trip(tmp_path) -> None:
    snapshot = SyntheticProjectAdapter(SourceMedium.SYNTHETIC).snapshot()
    game = tmp_path / "game"
    game.mkdir()
    review = Phase2ReviewEngine(tmp_path, game).review(
        snapshot,
        Intent(
            id="intent:review",
            project_id=snapshot.project_id,
            kind=IntentKind.DIAGNOSIS,
            original_prompt="Review the pressure pacing.",
            scope=("synthetic:rule:pressure",),
        ),
    )

    with PlayTraceStore(tmp_path / "review.db") as store:
        store.save_snapshot(snapshot)
        store.save_review(review)
        loaded = store.load_review(review.id)

    assert loaded == review


def test_simulation_round_trip_and_fts5_search(tmp_path) -> None:
    snapshot = _snapshot()
    config = SimulationConfig(
        id="cw:experiment:persistence",
        scenario_id="SCN-001",
        character_ids=("CHR-001",),
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key="starting_cubes",
                value={"SCD-001": 0, "SCD-002": 0, "SCD-003": 0},
                rationale="Isolate persistence behavior.",
            ),
            SimulationOverride(
                key="allow_pass", value=True, rationale="Isolate scenario pressure."
            ),
        ),
    )
    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), snapshot, config
    )

    with PlayTraceStore(tmp_path / "simulation.db") as store:
        store.save_snapshot(snapshot)
        store.save_simulation(run)
        loaded = store.load_simulation(run.id)
        results = store.search("completed")
        counts = store.export_counts()

    assert loaded == run
    assert [result["record_id"] for result in results] == [run.id]
    assert counts["simulation_runs"] == 1


def test_blocked_simulation_with_semantically_invalid_json_override_round_trips(tmp_path) -> None:
    snapshot = _snapshot()
    config = SimulationConfig(
        id="cw:experiment:malformed-persistence",
        scenario_id="SCN-001",
        character_ids=("CHR-001",),
        ruleset_id="cw:rules:provisional-core-v0.2",
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key="end_turn_pressure_target",
                value="middle",
                rationale="Retain this rejection rationale through persistence.",
            ),
        ),
    )
    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), snapshot, config
    )

    with PlayTraceStore(tmp_path / "malformed-simulation.db") as store:
        store.save_snapshot(snapshot)
        store.save_simulation(run)
        loaded = store.load_simulation(run.id)

    assert loaded == run
    assert loaded is not None
    override = loaded.config.overrides[0]
    assert override.rationale == "Retain this rejection rationale through persistence."
    assert override.value == "middle"
    assert loaded.blockers[0].id == "cw:blocker:override-end-turn-pressure-target"
