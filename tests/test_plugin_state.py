"""Synthetic contracts for independently installed simulation plugins."""
import json
import pytest
from pydantic import ValidationError
from playtrace.simulation import (
    PluginSimulationState, TabletopSimulationState, SimulationCheckpoint,
    SimulationConfig, SimulationRun, SimulationProvenance, SimulationMetrics,
    SimulationEvent,
)
from playtrace.simulation_report import write_simulation_report, write_simulation_fragment
from playtrace.tension import danger_for_state


def test_plugin_state_and_legacy_checkpoint_roundtrip():
    generic = PluginSimulationState(plugin_id="synthetic", data={"locations": {"a": [1, True, None]}})
    legacy = TabletopSimulationState(escalation="ready", deck=(), tableau={}, cube_meanings={})
    for state in (generic, legacy):
        checkpoint = SimulationCheckpoint(sequence=0, state=state)
        restored = SimulationCheckpoint.model_validate_json(checkpoint.model_dump_json())
        assert type(restored.state) is type(state)
        assert restored == checkpoint


@pytest.mark.parametrize("value", [float("nan"), {1: "key"}, "x" * 262145, object()], ids=["nan", "key", "size", "object"])
def test_plugin_json_is_bounded(value):
    with pytest.raises(ValidationError):
        PluginSimulationState(plugin_id="synthetic", data={"value": value})
    with pytest.raises(ValidationError):
        SimulationConfig(id="config", scenario_id="scenario", character_ids=(), plugin_config={"value": value})


def test_plugin_script_requires_explicit_configuration():
    with pytest.raises(ValidationError, match="requires plugin_config"):
        SimulationConfig(id="config", scenario_id="scenario", character_ids=(), player_policy="plugin_scripted")
    state = PluginSimulationState(plugin_id="synthetic", data={})
    with pytest.raises(ValueError, match="requires tabletop"):
        danger_for_state(state)


def test_plugin_report_preserves_arbitrary_state_and_escapes_source_text(tmp_path):
    hostile = "</script><script>alert('bad')</script>"
    state = PluginSimulationState(plugin_id="synthetic", data={"inventory": [hostile]})
    config = SimulationConfig(id="script", scenario_id="scenario", character_ids=(), player_policy="plugin_scripted", plugin_config={"choices": []})
    run = SimulationRun(id="run", config=config,
        provenance=SimulationProvenance(snapshot_id="snapshot", snapshot_digest="x", backend_version="test", plugin_id="synthetic", plugin_version="1", config_digest="x", player_policy="plugin_scripted", seeds=(0,), runner="test"),
        initial_state=state, final_state=state,
        events=(SimulationEvent(sequence=0, turn=0, event_type="custom", data={"text": hostile}),),
        state_trace=(SimulationCheckpoint(sequence=0, state=state),),
        metrics=SimulationMetrics(turns_completed=0, cards_resolved=0, escalation_steps=0, improves=0, worsens=0, maximum_tableau_cubes=0, blocker_count=0))
    assert SimulationRun.model_validate_json(run.model_dump_json()) == run
    for renderer in (write_simulation_report, write_simulation_fragment):
        output = tmp_path / "report.html"
        renderer([run], output)
        html = output.read_text(encoding="utf8")
        assert "Complete state checkpoint" in html
        assert hostile not in html
        assert "\\u003c/script>" in html
        assert "textContent" in html
