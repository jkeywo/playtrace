from playtrace.plugins.crisis_wardens import CrisisWardensSimulationPlugin
from playtrace.simulation import InProcessSimulationRunner, PlayerPolicyKind
from playtrace.simulation_report import write_simulation_report
from test_simulation import _config, _snapshot


def test_interactive_report_contains_scrubber_log_and_state_trace(tmp_path) -> None:
    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(),
        _snapshot(),
        _config(
            ruleset_id="cw:rules:provisional-core-v0.2",
            player_policy=PlayerPolicyKind.GREEDY,
            deck_order=("SCD-011",),
        ),
    )
    output = tmp_path / "report.html"

    write_simulation_report((run,), output)
    rendered = output.read_text(encoding="utf-8")

    assert '<input id="scrubber" type="range"' in rendered
    assert '<h2>Action log</h2>' in rendered
    assert '<h3>Tableau</h3>' in rendered
    assert "const traces=" in rendered
    assert len(run.state_trace) == len(run.events)
    assert "cw:rules:provisional-core-v0.2" in rendered
