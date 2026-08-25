from datetime import UTC, datetime
from pathlib import Path

from playtrace.models import DesignEntity, DesignSnapshot, SourceMedium, SourceRevision
from playtrace.plugins.crisis_wardens import CrisisWardensSimulationPlugin
from playtrace.simulation import (
    DockerSimulationRunner,
    InProcessSimulationRunner,
    PlayerPolicyKind,
    SimulationConfig,
    SimulationOverride,
    SimulationStatus,
    snapshot_content_digest,
)


def test_strict_crisis_wardens_projection_reports_source_blockers() -> None:
    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), _config()
    )

    assert run.final_state is None
    assert {item.id for item in run.blockers} == {
        "cw:blocker:starting-cubes",
        "cw:blocker:pass-action",
        "cw:blocker:deck-zones",
    }
    assert all(item.required_input for item in run.blockers)
    assert run.evidence_claim().evidence[0].evidence_id == run.id


def test_declared_smoke_overrides_produce_deterministic_replay() -> None:
    config = _config(
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key="starting_cubes",
                value={"SCD-001": 0, "SCD-002": 0, "SCD-003": 0},
                rationale="Boundary smoke test; not a canonical setup value.",
            ),
            SimulationOverride(
                key="allow_pass",
                value=True,
                rationale="Exercise scenario pressure without a player-action model.",
            ),
        ),
    )
    runner = InProcessSimulationRunner()

    first = runner.run(CrisisWardensSimulationPlugin(), _snapshot(), config)
    second = runner.run(CrisisWardensSimulationPlugin(), _snapshot(), config)

    assert first.id == second.id
    assert first.events == second.events
    assert first.final_state.status is SimulationStatus.WON
    assert first.final_state.tableau["SCD-001"] == 1
    assert first.metrics.turns_completed == 1
    assert first.metrics.worsens == 1


def test_snapshot_digest_ignores_extraction_clock_times() -> None:
    first = _snapshot()
    second = first.model_copy(
        update={
            "created_at": datetime(2030, 1, 1, tzinfo=UTC),
            "source_revisions": tuple(
                revision.model_copy(
                    update={"observed_at": datetime(2030, 1, 1, tzinfo=UTC)}
                )
                for revision in first.source_revisions
            ),
            "assertions": tuple(
                assertion.model_copy(
                    update={
                        "provenance": assertion.provenance.model_copy(
                            update={"extracted_at": datetime(2030, 1, 1, tzinfo=UTC)}
                        )
                    }
                )
                for assertion in first.assertions
            ),
            "capabilities": frozenset(reversed(sorted(first.capabilities))),
        }
    )

    assert snapshot_content_digest(first) == snapshot_content_digest(second)


def test_placeholder_threshold_blocks_instead_of_guessing() -> None:
    config = _config(
        deck_order=("SCD-006",),
        overrides=(
            SimulationOverride(
                key="starting_cubes",
                value={"SCD-001": 0, "SCD-002": 3, "SCD-003": 0},
                rationale="Force the documented threshold for a diagnostic test.",
            ),
            SimulationOverride(
                key="allow_pass", value=True, rationale="Scenario-only diagnostic."
            ),
        ),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )

    assert run.final_state.status is SimulationStatus.BLOCKED
    assert run.blockers[0].id == "cw:blocker:threshold-SCD-002"
    assert run.final_state.tableau["SCD-002"] == 4
    assert run.events[-1].event_type == "simulation.blocked"


def test_provisional_ruleset_runs_player_turn_without_hidden_overrides() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        deck_order=("SCD-005",),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )

    assert run.blockers == ()
    assert run.final_state.status is SimulationStatus.WON
    assert run.metrics.player_actions == 1
    assert run.final_state.players["CHR-001"].elements == {"Earth": 1}
    assert run.evidence_claim().evidence[0].assumptions == (
        "ruleset: cw:rules:provisional-core-v0.2",
    )


def test_provisional_rest_recovers_discard_only_after_hand_and_deck_empty() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        deck_order=("SCD-011", "SCD-011"),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )

    assert run.final_state.status is SimulationStatus.WON
    assert run.metrics.player_actions == 1
    assert run.metrics.rests == 1
    assert run.final_state.players["CHR-001"].rests == 1


def test_docker_runner_is_pinned_networkless_and_read_only(tmp_path) -> None:
    runner = DockerSimulationRunner(
        "playtrace-sim", image_digest="sha256:" + "a" * 64
    )

    command = runner.command(tmp_path / "input", tmp_path / "output")

    assert command[:4] == ["docker", "run", "--rm", "--network"]
    assert "none" in command
    assert "--read-only" in command
    assert "playtrace-sim@sha256:" + "a" * 64 in command


def _config(**updates: object) -> SimulationConfig:
    base = SimulationConfig(
        id="cw:simulation:test",
        scenario_id="SCN-001",
        character_ids=("CHR-001",),
        seed=7,
        max_turns=10,
    )
    return base.model_copy(update=updates)


def _snapshot() -> DesignSnapshot:
    def entity(entity_id: str, kind: str, name: str, fields: dict[str, object]) -> DesignEntity:
        return DesignEntity(
            id=entity_id,
            kind=kind,
            name=name,
            attributes={"fields": fields},
        )

    entities = (
        entity(
            "SCN-001",
            "scenario",
            "The Flooded Vale",
            {
                "Back: Starting Tableau": "Swollen River; Bridge Under Strain; Families Cut Off",
                "Front: Escalation": "Rising → Flooding → Emergency → Catastrophic. Exact effects remain PLACEHOLDER.",
            },
        ),
        entity("CHR-001", "character", "Orra", {}),
        entity(
            "CCD-T01",
            "character_card",
            "Test Action",
            {
                "Character ID": "CHR-001",
                "Qty": "1",
                "Status": "WORKING",
                "AP": "1",
                "Elements": "Earth",
                "Assist?": "No",
                "Timing / Cost": "1 AP",
                "Rules Text": "Improve a Hazard.",
            },
        ),
        entity(
            "SCD-001",
            "scenario_card",
            "Swollen River",
            {
                "Scenario ID": "SCN-001",
                "Status": "WORKING",
                "Card Kind": "Tableau",
                "Keywords": "Wild; Hazard",
                "Cube Meaning": "Flood",
                "Rules Text": "At 4+ Flood, when this card is Worsened, Worsen a Structure.",
            },
        ),
        entity(
            "SCD-002",
            "scenario_card",
            "Bridge Under Strain",
            {
                "Scenario ID": "SCN-001",
                "Status": "WORKING",
                "Card Kind": "Tableau",
                "Keywords": "Structure; Hazard",
                "Cube Meaning": "Damage",
                "Rules Text": "At 4 Damage, replace this with Collapsed Bridge [PLACEHOLDER].",
            },
        ),
        entity(
            "SCD-003",
            "scenario_card",
            "Families Cut Off",
            {
                "Scenario ID": "SCN-001",
                "Status": "WORKING",
                "Card Kind": "Tableau",
                "Keywords": "People; Hazard",
                "Cube Meaning": "Risk",
                "Rules Text": "At 4 Risk, create Panicked Evacuation [PLACEHOLDER].",
            },
        ),
        entity(
            "SCD-005",
            "scenario_card",
            "Heavy Rain",
            {
                "Scenario ID": "SCN-001",
                "Status": "WORKING",
                "Card Kind": "Event",
                "Keywords": "Hazard",
                "Rules Text": "Worsen the Hazard with the most cubes. If tied, players choose.",
            },
        ),
        entity(
            "SCD-006",
            "scenario_card",
            "River Breaks the Bank",
            {
                "Scenario ID": "SCN-001",
                "Status": "WORKING",
                "Card Kind": "Event",
                "Keywords": "Hazard; Structure",
                "Rules Text": "Worsen Swollen River. Then Worsen one Structure.",
            },
        ),
        entity(
            "SCD-011",
            "scenario_card",
            "Sudden Calm",
            {
                "Scenario ID": "SCN-001",
                "Status": "PLACEHOLDER",
                "Card Kind": "Event",
                "Keywords": "Hazard",
                "Rules Text": "Improve one Hazard.",
            },
        ),
    )
    return DesignSnapshot(
        id="crisis-wardens:snapshot:test",
        project_id="crisis-wardens",
        adapter_id="test",
        source_revisions=(
            SourceRevision(
                id="source@1",
                source_id="source",
                medium=SourceMedium.DRIVE,
                locator="fixture",
                revision="1",
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
        entities=entities,
        assertions=(),
        capabilities=frozenset({"simulation_generated", "replay_deterministic"}),
    )
