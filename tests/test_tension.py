from playtrace.plugins.crisis_wardens import CrisisWardensSimulationPlugin
from playtrace.simulation import (
    InProcessSimulationRunner,
    PlayerPolicyKind,
    SimulationConfig,
    SimulationOverride,
    SimulationStatus,
    TabletopSimulationState,
)
from playtrace.tension import build_batch, compare_batches, danger_for_state

from test_simulation import _snapshot


def test_danger_exposes_state_components_and_increases_near_loss() -> None:
    safe = TabletopSimulationState(
        escalation="Rising",
        deck=("SCD-012",),
        tableau={"SCD-001": 0, "SCD-002": 0},
        cube_meanings={"SCD-001": "Flood", "SCD-002": "Damage"},
    )
    danger_safe, safe_parts = danger_for_state(safe)
    danger_crisis, crisis_parts = danger_for_state(
        safe.model_copy(
            update={"escalation": "Emergency", "tableau": {"SCD-001": 2, "SCD-002": 3}}
        )
    )

    assert danger_crisis > danger_safe
    assert crisis_parts.escalation > safe_parts.escalation
    assert crisis_parts.thresholds > safe_parts.thresholds


def test_paired_ab_comparison_covers_each_character_combination() -> None:
    runner = InProcessSimulationRunner()
    plugin = CrisisWardensSimulationPlugin()
    combinations = (("CHR-001",), ("CHR-002",), ("CHR-001", "CHR-002"))
    baseline_runs = []
    candidate_runs = []
    for characters in combinations:
        base = SimulationConfig(
            id="test",
            scenario_id="SCN-001",
            character_ids=characters,
            ruleset_id=plugin.provisional_ruleset,
            seed=1,
            max_turns=10,
            player_policy=PlayerPolicyKind.GREEDY,
            deck_order=("SCD-005",),
        )
        baseline_runs.append(runner.run(plugin, _two_character_snapshot(), base))
        candidate_runs.append(
            runner.run(
                plugin,
                _two_character_snapshot(),
                base.model_copy(
                    update={
                        "overrides": (
                            SimulationOverride(
                                key="end_turn_pressure",
                                value={"rising": 1},
                                rationale="Paired experiment.",
                            ),
                        )
                    }
                ),
            )
        )

    comparison = compare_batches(
        build_batch("baseline", baseline_runs), build_batch("candidate", candidate_runs)
    )

    assert comparison.paired_runs == 3
    assert {item.combination for item in comparison.deltas} == {
        "CHR-001",
        "CHR-002",
        "CHR-001+CHR-002",
    }


def _two_character_snapshot():
    snapshot = _snapshot()
    orra = next(entity for entity in snapshot.entities if entity.id == "CHR-001")
    orra_card = next(entity for entity in snapshot.entities if entity.id == "CCD-T01")
    return snapshot.model_copy(
        update={
            "entities": snapshot.entities
            + (
                orra.model_copy(update={"id": "CHR-002", "name": "Senn"}),
                orra_card.model_copy(
                    update={
                        "id": "CCD-T02",
                        "name": "Test Senn Action",
                        "attributes": {
                            "fields": {
                                **orra_card.attributes["fields"],
                                "Character ID": "CHR-002",
                            }
                        },
                    }
                ),
            )
        }
    )
