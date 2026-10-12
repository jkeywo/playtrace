from playtrace.plugins.crisis_wardens import CrisisWardensSimulationPlugin
from playtrace.simulation import InProcessSimulationRunner, PlayerPolicyKind, SimulationConfig
from playtrace.tension import build_batch, compare_batches
from playtrace.tension_report import write_tension_report

from test_simulation import _snapshot


def test_tension_report_is_self_contained(tmp_path) -> None:
    config = SimulationConfig(
        id="tension-report-test", scenario_id="SCN-001",
        character_ids=("CHR-001",), ruleset_id="cw:rules:provisional-core-v0.2",
        seed=1, max_turns=10, player_policy=PlayerPolicyKind.GREEDY,
        deck_order=("SCD-005",),
    )
    run = InProcessSimulationRunner().run(CrisisWardensSimulationPlugin(), _snapshot(), config)
    baseline = build_batch("baseline", (run,))
    candidate = build_batch("candidate", (run,))
    output = tmp_path / "tension.html"

    write_tension_report(baseline, candidate, compare_batches(baseline, candidate), output)

    report = output.read_text(encoding="utf-8")
    assert "Tension A/B Report" in report
    assert "Mean danger curves" in report
    assert "<script>" in report
    assert "https://" not in report
