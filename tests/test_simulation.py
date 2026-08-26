import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import playtrace.cli as cli
import playtrace.plugins.crisis_wardens as crisis_wardens_module
import playtrace.simulation as simulation_module
from playtrace.cnl import RuleIR, RuleKind
from playtrace.models import (
    Assertion,
    AuthoredStatus,
    DesignEntity,
    DesignSnapshot,
    ProvenanceRef,
    SourceMedium,
    SourceRevision,
)
from playtrace.plugins.crisis_wardens import CrisisWardensSimulationPlugin
from playtrace.simulation import (
    DockerSimulationRunner,
    InProcessSimulationRunner,
    JSON_SAFE_INTEGER_MAX,
    OVERRIDE_JSON_MAX_BYTES,
    OVERRIDE_JSON_MAX_DEPTH,
    OVERRIDE_JSON_MAX_NODES,
    SIMULATION_INPUT_MAX_DEPTH,
    PlayerPolicyKind,
    PlayerActionKind,
    ScriptedPolicyStep,
    SimulationConfig,
    SimulationJsonInputError,
    SimulationOverride,
    SimulationRun,
    SimulationStatus,
    decode_bounded_json,
    load_bounded_json_file,
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


def test_provisional_starting_cube_override_is_audited_when_setup_consumes_it() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key="starting_cubes",
                value={"SCD-001": 0, "SCD-002": 0, "SCD-003": 0},
                rationale="Exercise explicit setup inside the provisional loop.",
            ),
        ),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )
    usage = next(
        item
        for item in run.semantic_audit.usages
        if item.id == "override:starting_cubes"
    )

    assert any(event.event_type == "scenario.setup" for event in run.events)
    assert usage.executed
    assert usage.execution_count == 1


def test_provisional_default_setup_does_not_invent_starting_cube_override_usage() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        deck_order=("SCD-005",),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )

    assert all(
        item.id != "override:starting_cubes"
        for item in run.semantic_audit.usages
    )


def test_blocked_starting_cube_override_is_not_audited_as_consumed() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key="starting_cubes",
                value={"SCD-001": 0},
                rationale="An incomplete map must block before setup.",
            ),
        ),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )
    usage = next(
        item
        for item in run.semantic_audit.usages
        if item.id == "override:starting_cubes"
    )

    assert run.final_state is None
    assert not any(event.event_type == "scenario.setup" for event in run.events)
    assert not usage.executed
    assert usage.execution_count == 0


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


def test_scripted_policy_replays_exact_legal_step() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.SCRIPTED,
        scripted_steps=(
            ScriptedPolicyStep(
                turn=1,
                actor_id="CHR-001",
                action=PlayerActionKind.ACTIVATE_CARD,
                card_id="CCD-T01",
            ),
        ),
        deck_order=("SCD-005",),
    )
    runner = InProcessSimulationRunner()

    first = runner.run(CrisisWardensSimulationPlugin(), _snapshot(), config)
    second = runner.run(CrisisWardensSimulationPlugin(), _snapshot(), config)

    assert first.events == second.events
    assert first.final_state.status is SimulationStatus.WON
    assert first.metrics.player_actions == 1


def test_scripted_policy_blocks_illegal_step_without_fallback() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.SCRIPTED,
        scripted_steps=(
            ScriptedPolicyStep(
                turn=1,
                actor_id="CHR-001",
                action=PlayerActionKind.ACTIVATE_CARD,
                card_id="not-in-hand",
            ),
        ),
        deck_order=("SCD-005",),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )

    assert run.final_state.status is SimulationStatus.BLOCKED
    assert run.blockers[0].id == "policy:blocker:turn-1:CHR-001"
    assert not any(event.event_type == "player.card_activated" for event in run.events)


def test_strict_semantics_blocks_provisional_ruleset_before_play() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        strict_semantics=True,
        deck_order=("SCD-005",),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )

    assert run.final_state is None
    assert not run.semantic_audit.passed
    assert "cw:rules:provisional-core-v0.2" in run.semantic_audit.blocking_usage_ids


def test_strict_semantics_allows_compiled_contract_and_registered_extensions() -> None:
    config = _config(
        strict_semantics=True,
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key="starting_cubes",
                value={"SCD-001": 0, "SCD-002": 0, "SCD-003": 0},
                rationale="Canonical setup counts supplied by the game.",
                canonical=True,
            ),
            SimulationOverride(
                key="allow_pass",
                value=True,
                rationale="Canonical passive test policy permission.",
                canonical=True,
            ),
        ),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )

    assert run.final_state.status is SimulationStatus.WON
    assert run.semantic_audit.passed
    assert any(
        usage.origin.value == "plugin_extension" and usage.executed
        for usage in run.semantic_audit.usages
    )


def test_unknown_override_is_rejected_even_when_caller_marks_it_canonical() -> None:
    config = _config(
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key="invented_semantics",
                value=True,
                rationale="A caller cannot register semantics.",
                canonical=True,
            ),
        ),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )

    assert run.final_state is None
    assert "cw:blocker:unknown-overrides" in {item.id for item in run.blockers}
    assert not any(
        usage.id == "override:invented_semantics"
        and usage.origin.value == "plugin_extension"
        for usage in run.semantic_audit.usages
    )


def test_supported_but_unused_override_is_not_reported_as_executed() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key="end_turn_pressure",
                value={"emergency": 1},
                rationale="This run never reaches Emergency.",
            ),
        ),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )
    usage = next(
        item
        for item in run.semantic_audit.usages
        if item.id == "override:end_turn_pressure"
    )

    assert run.events
    assert not usage.executed
    assert usage.execution_count == 0


def test_canonical_flag_cannot_promote_experimental_override_origin() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key="end_turn_pressure",
                value={"rising": 1},
                rationale="Still an experiment despite the caller flag.",
                canonical=True,
            ),
        ),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )
    usage = next(
        item
        for item in run.semantic_audit.usages
        if item.id == "override:end_turn_pressure"
    )

    assert usage.origin.value == "provisional_override"
    assert usage.executed


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("end_turn_pressure_target", []),
        ("starting_cubes", {"SCD-001": "three"}),
        ("starting_cubes", {"SCD-001": -1}),
        ("scenario_deck_card_ids", {"SCD-005": 1}),
        ("scenario_deck_card_ids", [1]),
        ("allow_pass", "yes"),
        ("end_turn_pressure", []),
        ("end_turn_pressure", {"rising": -1}),
        ("end_turn_pressure", {"rising": "1"}),
        ("end_turn_pressure", {"unknown": 1}),
    ),
)
def test_registered_override_values_are_blocked_before_use(
    key: str, value: object
) -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key=key,
                value=value,
                rationale="Malformed input must become a typed blocker, not an exception.",
            ),
        ),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )

    assert run.final_state is None
    assert run.blockers
    assert all(item.kind.value == "invalid_configuration" for item in run.blockers)
    assert run.blockers[0].id == f"cw:blocker:override-{key.replace('_', '-')}"


@pytest.mark.parametrize("value", (object(), {"unserializable"}, ("tuple",)))
def test_python_only_override_values_raise_structured_validation_errors(
    value: object,
) -> None:
    with pytest.raises(ValidationError) as captured:
        SimulationOverride(
            key="end_turn_pressure_target",
            value=value,
            rationale="Only strict JSON crosses this boundary.",
        )

    error = captured.value.errors(include_input=False)[0]
    assert error["type"] == "simulation_override_json"
    assert error["ctx"]["category"] == "unsupported_type"


def test_rejected_object_is_not_asked_for_representation_or_type_metadata() -> None:
    class HostileMetadata(type):
        def __getattribute__(cls, name: str) -> object:
            if name in {"__module__", "__qualname__", "__name__"}:
                raise AssertionError("type metadata must not be inspected")
            return super().__getattribute__(name)

    class HostileValue(metaclass=HostileMetadata):
        def __repr__(self) -> str:
            raise AssertionError("representation must not be inspected")

    with pytest.raises(ValidationError) as captured:
        SimulationOverride(
            key="end_turn_pressure_target",
            value=HostileValue(),
            rationale="Reject without interrogating the object.",
        )

    assert captured.value.errors(include_input=False)[0]["ctx"]["category"] == (
        "unsupported_type"
    )


def test_non_string_json_object_keys_raise_structured_validation_error() -> None:
    with pytest.raises(ValidationError) as captured:
        SimulationOverride(
            key="starting_cubes",
            value={3: "zeta"},
            rationale="JSON objects require string keys.",
        )

    error = captured.value.errors(include_input=False)[0]
    assert error["ctx"]["category"] == "non_string_key"


def _deep_override_value() -> list[object]:
    root: list[object] = []
    cursor = root
    for _ in range(OVERRIDE_JSON_MAX_DEPTH + 1):
        child: list[object] = []
        cursor.append(child)
        cursor = child
    return root


def _cyclic_override_value() -> list[object]:
    value: list[object] = []
    value.append(value)
    return value


@pytest.mark.parametrize(
    ("value_factory", "expected_category"),
    (
        (lambda: JSON_SAFE_INTEGER_MAX + 1, "integer_out_of_range"),
        (lambda: 10 ** 100_000, "integer_out_of_range"),
        (lambda: "\ud800", "invalid_unicode"),
        (_deep_override_value, "maximum_depth"),
        (_cyclic_override_value, "cycle"),
        (lambda: [0] * OVERRIDE_JSON_MAX_NODES, "maximum_nodes"),
        (lambda: "x" * OVERRIDE_JSON_MAX_BYTES, "maximum_size"),
    ),
)
def test_out_of_contract_override_values_raise_bounded_validation_errors(
    value_factory: object,
    expected_category: str,
) -> None:
    with pytest.raises(ValidationError) as captured:
        SimulationOverride(
            key="end_turn_pressure_target",
            value=value_factory(),
            rationale="Exercise the bounded JSON contract.",
        )

    error = captured.value.errors(include_input=False)[0]
    assert error["type"] == "simulation_override_json"
    assert error["ctx"]["category"] == expected_category


def test_valid_override_preserves_json_semantics_and_canonical_key_order() -> None:
    source = {
        "zeta": [None, True, JSON_SAFE_INTEGER_MAX, 1.25, "snowman ☃"],
        "alpha": {"second": 2, "first": 1},
    }

    override = SimulationOverride(
        key="starting_cubes",
        value=source,
        rationale="Verify the strict JSON success path.",
    )

    assert override.value == source
    assert list(override.value) == ["alpha", "zeta"]
    assert list(override.value["alpha"]) == ["first", "second"]
    encoded = json.dumps(
        override.value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    assert json.loads(encoded) == source


@pytest.mark.parametrize(
    ("payload_factory", "category"),
    (
        (lambda: "9" * 100_000, "integer_out_of_range"),
        (lambda: '{"value":"\\udfff"}', "invalid_unicode"),
        (
            lambda: "[" * (SIMULATION_INPUT_MAX_DEPTH + 1)
            + "]" * (SIMULATION_INPUT_MAX_DEPTH + 1),
            "maximum_depth",
        ),
    ),
)
def test_raw_json_rejects_hostile_tokens_before_model_validation(
    payload_factory: object,
    category: str,
) -> None:
    with pytest.raises(SimulationJsonInputError) as captured:
        decode_bounded_json(payload_factory())

    assert captured.value.category == category


def test_valid_json_with_unsupported_semantics_survives_worker_boundary(
    tmp_path: Path,
) -> None:
    plugin = CrisisWardensSimulationPlugin()
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key="end_turn_pressure_target",
                value="middle",
                rationale="Exercise the isolated-worker serialization boundary.",
            ),
        ),
    )
    runner = DockerSimulationRunner(
        "playtrace-sim", image_digest="sha256:" + "a" * 64
    )
    request = runner.request_payload(plugin, _snapshot(), config)
    encoded_request = json.dumps(request, sort_keys=True)
    assert '"value": "middle"' in encoded_request

    request_path = tmp_path / "request.json"
    output_path = tmp_path / "result.json"
    request_path.write_text(encoded_request, encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "playtrace.sim_worker", str(request_path), str(output_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    run = SimulationRun.model_validate_json(output_path.read_text(encoding="utf-8"))
    assert run.final_state is None
    assert run.blockers[0].id == "cw:blocker:override-end-turn-pressure-target"
    assert run.provenance.container_image_digest == "sha256:" + "a" * 64
    assert run.config.overrides[0].value == "middle"


def test_cli_reports_out_of_range_raw_json_as_an_input_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"id":"x","scenario_id":"SCN-001","character_ids":["CHR-001"],'
        '"overrides":[{"key":"end_turn_pressure","value":99999999999999999,'
        '"rationale":"reject before integer allocation"}]}',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="integer_out_of_range"):
        cli._load_simulation_config(config_path)


def test_worker_reports_raw_json_contract_failure_as_input_error(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "result.json"
    request_path.write_text(
        '{"plugin_id":"crisis_wardens","snapshot":{},"config":{'
        '"overrides":[{"key":"x","value":99999999999999999,'
        '"rationale":"reject"}]}}',
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, "-m", "playtrace.sim_worker", str(request_path), str(output_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "invalid simulation JSON input (integer_out_of_range)" in completed.stderr
    assert not output_path.exists()


def test_file_size_is_rejected_before_reading_or_decoding(tmp_path: Path) -> None:
    path = tmp_path / "large.json"
    path.write_bytes(b"{" + b" " * 32 + b"}")

    with pytest.raises(SimulationJsonInputError) as captured:
        load_bounded_json_file(path, max_bytes=16)

    assert captured.value.category == "maximum_size"


@pytest.mark.parametrize("runner_kind", ("in_process", "docker_request"))
def test_runners_reject_the_wrong_top_level_config_type(runner_kind: str) -> None:
    plugin = CrisisWardensSimulationPlugin()
    if runner_kind == "in_process":
        with pytest.raises(TypeError, match="validated SimulationConfig"):
            InProcessSimulationRunner().run(plugin, _snapshot(), object())  # type: ignore[arg-type]
    else:
        runner = DockerSimulationRunner(
            "playtrace-sim", image_digest="sha256:" + "a" * 64
        )
        with pytest.raises(TypeError, match="validated SimulationConfig"):
            runner.request_payload(plugin, _snapshot(), object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("constant", "limit", "category"),
    (
        ("SIMULATION_INPUT_MAX_BYTES", 5, "maximum_size"),
        ("SIMULATION_INPUT_MAX_DEPTH", 1, "maximum_depth"),
        ("SIMULATION_INPUT_MAX_NODES", 5, "maximum_nodes"),
    ),
)
def test_docker_request_applies_worker_aggregate_budgets(
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    limit: int,
    category: str,
) -> None:
    monkeypatch.setattr(simulation_module, constant, limit)
    runner = DockerSimulationRunner(
        "playtrace-sim", image_digest="sha256:" + "a" * 64
    )

    with pytest.raises(SimulationJsonInputError) as captured:
        runner.request_payload(
            CrisisWardensSimulationPlugin(), _snapshot(), _config()
        )

    assert captured.value.category == category


@pytest.mark.parametrize(
    ("text", "limits", "category"),
    (
        ("[0,0,0]", {"max_nodes": 3}, "maximum_nodes"),
        ("[[[0]]]", {"max_depth": 2}, "maximum_depth"),
    ),
)
def test_raw_structure_budgets_fail_before_decoder(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    limits: dict[str, int],
    category: str,
) -> None:
    def decoder_must_not_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("recursive decoder was entered before budget rejection")

    monkeypatch.setattr(simulation_module.json, "loads", decoder_must_not_run)

    with pytest.raises(SimulationJsonInputError) as captured:
        decode_bounded_json(text, **limits)

    assert captured.value.category == category


def _nested_arrays(depth: int) -> object:
    value: object = 0
    for _ in range(depth):
        value = [value]
    return value


def _container_depth(value: object) -> int:
    if type(value) is list:
        return 1 + max((_container_depth(item) for item in value), default=0)
    if type(value) is dict:
        return 1 + max((_container_depth(item) for item in value.values()), default=0)
    return 0


def test_override_and_raw_json_use_same_root_depth_boundary() -> None:
    at_limit = _nested_arrays(OVERRIDE_JSON_MAX_DEPTH)
    over_limit = _nested_arrays(OVERRIDE_JSON_MAX_DEPTH + 1)

    assert SimulationOverride(
        key="depth",
        value=at_limit,
        rationale="Exactly the documented container-depth limit.",
    ).value == at_limit
    assert decode_bounded_json(
        json.dumps(at_limit), max_depth=OVERRIDE_JSON_MAX_DEPTH
    ) == at_limit

    with pytest.raises(ValidationError) as model_error:
        SimulationOverride(
            key="depth",
            value=over_limit,
            rationale="One container beyond the documented limit.",
        )
    assert model_error.value.errors(include_input=False)[0]["ctx"]["category"] == (
        "maximum_depth"
    )
    with pytest.raises(SimulationJsonInputError) as raw_error:
        decode_bounded_json(
            json.dumps(over_limit), max_depth=OVERRIDE_JSON_MAX_DEPTH
        )
    assert raw_error.value.category == "maximum_depth"


def test_full_docker_request_and_worker_scanner_share_depth_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = DockerSimulationRunner(
        "playtrace-sim", image_digest="sha256:" + "a" * 64
    )

    def snapshot_with_nested_depth(depth: int) -> DesignSnapshot:
        snapshot = _snapshot()
        entity = DesignEntity(
            id="depth-probe",
            kind="test_fixture",
            name="Depth probe",
            attributes={"nested": _nested_arrays(depth)},
        )
        return snapshot.model_copy(
            update={"entities": snapshot.entities + (entity,)}
        )

    at_limit_snapshot = snapshot_with_nested_depth(20)
    over_limit_snapshot = snapshot_with_nested_depth(21)
    at_limit_payload = runner.request_payload(
        CrisisWardensSimulationPlugin(), at_limit_snapshot, _config()
    )
    over_limit_payload = runner.request_payload(
        CrisisWardensSimulationPlugin(), over_limit_snapshot, _config()
    )
    limit = _container_depth(at_limit_payload)
    assert _container_depth(over_limit_payload) == limit + 1

    monkeypatch.setattr(simulation_module, "SIMULATION_INPUT_MAX_DEPTH", limit)
    accepted = runner.request_payload(
        CrisisWardensSimulationPlugin(), at_limit_snapshot, _config()
    )
    assert decode_bounded_json(json.dumps(accepted), max_depth=limit) == accepted

    with pytest.raises(SimulationJsonInputError) as payload_error:
        runner.request_payload(
            CrisisWardensSimulationPlugin(), over_limit_snapshot, _config()
        )
    assert payload_error.value.category == "maximum_depth"
    with pytest.raises(SimulationJsonInputError) as worker_error:
        decode_bounded_json(json.dumps(over_limit_payload), max_depth=limit)
    assert worker_error.value.category == "maximum_depth"


def test_scenario_deck_override_rejects_well_shaped_unknown_card_ids() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        overrides=(
            SimulationOverride(
                key="scenario_deck_card_ids",
                value=["SCD-DOES-NOT-EXIST"],
                rationale="An unknown stable ID must not enter the runtime deck.",
            ),
        ),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )

    assert run.final_state is None
    blocker = next(item for item in run.blockers if item.id == "cw:blocker:deck-card")
    assert blocker.kind.value == "invalid_configuration"
    assert blocker.entity_ids == ("SCD-DOES-NOT-EXIST",)


@pytest.mark.parametrize(
    ("starting_cubes", "invalid_ids"),
    (
        (
            {
                "SCD-001": 0,
                "SCD-002": 0,
                "SCD-003": 0,
                "SCD-DOES-NOT-START": 0,
            },
            ("SCD-DOES-NOT-START",),
        ),
        ({"SCD-001": 0, "SCD-002": 0}, ("SCD-003",)),
    ),
)
def test_starting_cube_override_requires_exact_tableau_ids(
    starting_cubes: dict[str, int], invalid_ids: tuple[str, ...]
) -> None:
    config = _config(
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key="starting_cubes",
                value=starting_cubes,
                rationale="The setup map must exactly cover its stable IDs.",
            ),
            SimulationOverride(
                key="allow_pass", value=True, rationale="Exact passive fixture."
            ),
        ),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )

    blocker = next(
        item
        for item in run.blockers
        if item.id == "cw:blocker:override-starting-cubes-keys"
    )
    assert blocker.kind.value == "invalid_configuration"
    assert blocker.entity_ids == invalid_ids


def test_placeholder_audit_ignores_setup_mentions_when_no_effect_runs() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        deck_order=("SCD-005",),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )
    usage = next(
        item
        for item in run.semantic_audit.usages
        if item.id == "cw:provisional:placeholder-effects"
    )

    assert not usage.executed
    assert usage.execution_count == 0


def test_placeholder_audit_counts_explicit_provisional_effect_event() -> None:
    config = _config(
        ruleset_id="cw:rules:provisional-core-v0.2",
        player_policy=PlayerPolicyKind.GREEDY,
        deck_order=("SCD-011",),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), _snapshot(), config
    )
    usage = next(
        item
        for item in run.semantic_audit.usages
        if item.id == "cw:provisional:placeholder-effects"
    )

    assert usage.executed
    assert usage.execution_count == 1
    assert any(
        item.event_type == "provisional.effect_applied"
        and item.semantic_ids == ("cw:provisional:placeholder-effects",)
        for item in run.events
    )


def test_compiled_audit_uses_only_real_assertions_and_exact_source_revisions() -> None:
    snapshot = _snapshot()
    revisions = snapshot.source_revisions + (
        SourceRevision(
            id="rules-b@9",
            source_id="rules-b",
            medium=SourceMedium.DRIVE,
            locator="rules-b",
            revision="9",
        ),
        SourceRevision(
            id="unrelated@4",
            source_id="unrelated",
            medium=SourceMedium.DRIVE,
            locator="unrelated",
            revision="4",
        ),
    )
    rule_a = RuleIR(
        id="rule:a",
        kind=RuleKind.STATE_CHANGE,
        subject="SCD-001",
        action="improve",
        source='RULE rule:a: IMPROVE "SCD-001".',
    ).model_dump(mode="json")
    rule_b = RuleIR(
        id="rule:b",
        kind=RuleKind.STATE_CHANGE,
        subject="SCD-002",
        action="worsen",
        source='RULE rule:b: WORSEN "SCD-002".',
    ).model_dump(mode="json")
    compiled_entities = (
        DesignEntity(
            id="rule:a", kind="compiled_rule", name="Rule A", attributes={"ir": rule_a}
        ),
        DesignEntity(
            id="rule:b", kind="compiled_rule", name="Rule B", attributes={"ir": rule_b}
        ),
        DesignEntity(id="rule:no-assertion", kind="compiled_rule", name="Unproven rule"),
    )
    compiled_assertions = (
        Assertion(
            id="compiled:a:actual",
            subject_id="rule:a",
            predicate="compiles_to",
            value=rule_a,
            authored_status=AuthoredStatus.CONFIRMED,
            provenance=ProvenanceRef(
                source_revision_id="source@1",
                location="cnl:rule:a",
                extractor="test:cnl",
            ),
        ),
        Assertion(
            id="compiled:b:actual",
            subject_id="rule:b",
            predicate="compiles_to",
            value=rule_b,
            authored_status=AuthoredStatus.CONFIRMED,
            provenance=ProvenanceRef(
                source_revision_id="rules-b@9",
                location="cnl:rule:b",
                extractor="test:cnl",
            ),
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "source_revisions": revisions,
            "entities": snapshot.entities + compiled_entities,
            "assertions": compiled_assertions,
        }
    )
    config = _config(
        deck_order=("SCD-005",),
        overrides=(
            SimulationOverride(
                key="starting_cubes",
                value={"SCD-001": 0, "SCD-002": 0, "SCD-003": 0},
                rationale="Exact setup fixture.",
            ),
            SimulationOverride(
                key="allow_pass", value=True, rationale="Exact passive fixture."
            ),
        ),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), snapshot, config
    )
    usage = next(
        item
        for item in run.semantic_audit.usages
        if item.id == "snapshot:compiled-ruleir"
    )

    assert usage.rule_ids == ("rule:a", "rule:b")
    assert usage.assertion_ids == ("compiled:a:actual", "compiled:b:actual")
    assert usage.source_revision_ids == ("source@1", "rules-b@9")
    assert "unrelated@4" not in usage.source_revision_ids
    assert "rule:no-assertion:compiled" not in usage.assertion_ids

    ambiguous = next(
        item
        for item in run.semantic_audit.usages
        if item.id == "snapshot:invalid-compiled-ruleir"
    )
    assert ambiguous.entity_ids == ("rule:no-assertion",)


def test_compiled_audit_rejects_mismatched_ruleir_and_excludes_its_provenance() -> None:
    snapshot = _snapshot()
    entity_ir = RuleIR(
        id="rule:a",
        kind=RuleKind.STATE_CHANGE,
        subject="SCD-001",
        action="improve",
        source='RULE rule:a: IMPROVE "SCD-001".',
    ).model_dump(mode="json")
    asserted_ir = {**entity_ir, "action": "worsen"}
    entity = DesignEntity(
        id="rule:a",
        kind="compiled_rule",
        name="Rule A",
        attributes={"ir": entity_ir},
    )
    assertion = Assertion(
        id="compiled:a:mismatch",
        subject_id="rule:a",
        predicate="compiles_to",
        value=asserted_ir,
        provenance=ProvenanceRef(
            source_revision_id="source@1",
            location="cnl:rule:a",
            extractor="test:cnl",
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "entities": snapshot.entities + (entity,),
            "assertions": (assertion,),
        }
    )
    config = _config(
        deck_order=("SCD-005",),
        strict_semantics=True,
        overrides=(
            SimulationOverride(
                key="starting_cubes",
                value={"SCD-001": 0, "SCD-002": 0, "SCD-003": 0},
                rationale="Exact setup fixture.",
            ),
            SimulationOverride(
                key="allow_pass", value=True, rationale="Exact passive fixture."
            ),
        ),
    )

    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), snapshot, config
    )

    assert all(
        item.id != "snapshot:compiled-ruleir" for item in run.semantic_audit.usages
    )
    invalid = next(
        item
        for item in run.semantic_audit.usages
        if item.id == "snapshot:invalid-compiled-ruleir"
    )
    assert invalid.entity_ids == ("rule:a",)
    assert invalid.assertion_ids == ("compiled:a:mismatch",)
    assert "snapshot:invalid-compiled-ruleir" in run.semantic_audit.blocking_usage_ids
    assert any(
        item.id == "cw:blocker:semantic-audit:snapshot:invalid-compiled-ruleir"
        for item in run.blockers
    )


def test_ruleir_subclasses_are_reconstructed_as_exact_validated_base_models() -> None:
    class UntrustedRule(RuleIR):
        def __getattribute__(self, name: str) -> object:
            if name == "__dict__":
                raise AssertionError("subclass attribute hooks must not run")
            return super().__getattribute__(name)

    valid = UntrustedRule.model_construct(
        id="rule:valid",
        kind=RuleKind.STATE_CHANGE,
        subject="SCD-001",
        action="improve",
        source='RULE rule:valid: IMPROVE "SCD-001".',
    )
    invalid_kind = UntrustedRule.model_construct(
        id="rule:kind",
        kind="invented",
        subject="SCD-001",
        action="improve",
        source='RULE rule:kind: IMPROVE "SCD-001".',
    )
    invalid_source = UntrustedRule.model_construct(
        id="rule:source",
        kind=RuleKind.STATE_CHANGE,
        subject="SCD-001",
        action="improve",
        source=object(),
    )

    reconstructed = crisis_wardens_module._validated_rule_ir(valid)
    assert type(reconstructed) is RuleIR
    assert crisis_wardens_module._validated_rule_ir(invalid_kind) is None
    assert crisis_wardens_module._validated_rule_ir(invalid_source) is None


def test_unchecked_invalid_ruleir_is_never_classified_as_compiled() -> None:
    class UntrustedRuleShape(BaseModel):
        id: object
        kind: object
        subject: object = None
        action: object = None
        source: object

    invalid_rule = UntrustedRuleShape(
        id="rule:unchecked",
        kind="invented",
        subject="SCD-001",
        action="improve",
        source="",
    )
    snapshot = _snapshot()
    entity = DesignEntity(
        id="rule:unchecked",
        kind="compiled_rule",
        name="Unchecked rule",
        attributes={"ir": invalid_rule},
    )
    assertion = Assertion(
        id="compiled:unchecked",
        subject_id="rule:unchecked",
        predicate="compiles_to",
        value=invalid_rule,
        provenance=ProvenanceRef(
            source_revision_id="source@1",
            location="cnl:rule:unchecked",
            extractor="test:unchecked",
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "entities": snapshot.entities + (entity,),
            "assertions": (assertion,),
        }
    )
    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), snapshot, _config()
    )

    assert all(
        item.id != "snapshot:compiled-ruleir" for item in run.semantic_audit.usages
    )
    invalid = next(
        item
        for item in run.semantic_audit.usages
        if item.id == "snapshot:invalid-compiled-ruleir"
    )
    assert invalid.entity_ids == ("rule:unchecked",)
    assert invalid.assertion_ids == ("compiled:unchecked",)


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
