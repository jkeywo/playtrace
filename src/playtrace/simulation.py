"""Deterministic simulation contracts shared by generated and native runners."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, JsonValue, field_validator, model_validator
from pydantic_core import PydanticCustomError

from playtrace.canonical import canonical_digest
from playtrace.models import (
    DesignSnapshot,
    EvidenceClaim,
    EvidenceLink,
    EvidenceRelation,
    StrictModel,
)


SIMULATION_SCHEMA_VERSION = 1

# Override values cross Python, SQLite, CLI, and container JSON boundaries.  Keep
# integers inside IEEE-754's exactly representable range so a value has the same
# meaning in JavaScript and other common JSON consumers.  The structural limits
# make validation total and prevent an untrusted override from consuming
# unbounded recursion, traversal, or serialization work.
JSON_SAFE_INTEGER_MAX = 9_007_199_254_740_991
OVERRIDE_JSON_MAX_DEPTH = 32
OVERRIDE_JSON_MAX_NODES = 10_000
OVERRIDE_JSON_MAX_BYTES = 262_144
SIMULATION_INPUT_MAX_DEPTH = 64
SIMULATION_INPUT_MAX_NODES = 250_000
SIMULATION_INPUT_MAX_BYTES = 16_777_216

_INVALID_OVERRIDE_REASONS = {
    "unsupported_type": "Simulation override values may contain only JSON null, booleans, numbers, strings, arrays, and string-keyed objects.",
    "non_string_key": "Simulation override JSON objects must use string keys.",
    "non_finite_float": "Simulation override numbers must be finite.",
    "integer_out_of_range": (
        "Simulation override integers must be within the interoperable JSON-safe "
        f"range -{JSON_SAFE_INTEGER_MAX} to {JSON_SAFE_INTEGER_MAX}."
    ),
    "invalid_unicode": "Simulation override strings and object keys must be valid UTF-8 without unpaired surrogates.",
    "cycle": "Simulation override arrays and objects must not contain cycles.",
    "maximum_depth": f"Simulation override values may be at most {OVERRIDE_JSON_MAX_DEPTH} levels deep.",
    "maximum_nodes": f"Simulation override values may contain at most {OVERRIDE_JSON_MAX_NODES} JSON nodes.",
    "maximum_size": f"A serialized simulation override may be at most {OVERRIDE_JSON_MAX_BYTES} UTF-8 bytes.",
}


class SimulationJsonInputError(ValueError):
    """A bounded JSON file failed before domain-model validation."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        self.message = message
        super().__init__(f"{category}: {message}")


class PlayerPolicyKind(StrEnum):
    PASSIVE = "passive"
    GREEDY = "greedy"
    RISK_AVERSE = "risk_averse"
    RANDOM_CONSTRAINED = "random_constrained"
    SCRIPTED = "scripted"


class PlayerActionKind(StrEnum):
    ACTIVATE_CARD = "activate_card"
    REST = "rest"
    PASS = "pass"


class ScriptedPolicyStep(StrictModel):
    """One exact player decision in a deterministic replay script."""

    turn: int
    actor_id: str
    action: PlayerActionKind
    card_id: str | None = None
    target_id: str | None = None

    @model_validator(mode="after")
    def require_action_fields(self) -> "ScriptedPolicyStep":
        if self.turn < 1:
            raise ValueError("scripted step turn must be positive")
        if self.action is PlayerActionKind.ACTIVATE_CARD and not self.card_id:
            raise ValueError("activate_card scripted steps require card_id")
        if self.action is not PlayerActionKind.ACTIVATE_CARD and self.card_id:
            raise ValueError("only activate_card scripted steps may name card_id")
        return self


class SimulationStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    WON = "won"
    LOST = "lost"
    MAX_TURNS = "max_turns"
    INVALID = "invalid"


class BlockerKind(StrEnum):
    MISSING_SOURCE_FACT = "missing_source_fact"
    AMBIGUOUS_SOURCE_RULE = "ambiguous_source_rule"
    UNSUPPORTED_RULE = "unsupported_rule"
    INVALID_CONFIGURATION = "invalid_configuration"
    INVARIANT_FAILURE = "invariant_failure"


class SimulationOverride(StrictModel):
    key: str
    value: JsonValue
    rationale: str
    canonical: bool = False

    @field_validator("value", mode="before")
    @classmethod
    def validate_json_value(cls, value: Any) -> JsonValue:
        """Accept only bounded, interoperable JSON data.

        The validator deliberately handles exact built-in JSON types only. It never
        asks an unsupported object for a representation, iterator, mapping view, or
        type metadata. Out-of-contract input is an ordinary structured Pydantic error;
        only valid JSON values proceed to game-specific semantic validation.
        """

        try:
            return _validate_json_value(
                value,
                max_depth=OVERRIDE_JSON_MAX_DEPTH,
                max_nodes=OVERRIDE_JSON_MAX_NODES,
                max_bytes=OVERRIDE_JSON_MAX_BYTES,
            )
        except _InvalidJsonValue as exc:
            raise PydanticCustomError(
                "simulation_override_json",
                _INVALID_OVERRIDE_REASONS[exc.category],
                {"category": exc.category},
            ) from None


class SimulationConfig(StrictModel):
    id: str
    scenario_id: str
    character_ids: tuple[str, ...]
    ruleset_id: str | None = None
    seed: int = 0
    max_turns: int = 100
    player_policy: PlayerPolicyKind = PlayerPolicyKind.PASSIVE
    deck_order: tuple[str, ...] = ()
    overrides: tuple[SimulationOverride, ...] = ()
    scripted_steps: tuple[ScriptedPolicyStep, ...] = ()
    strict_semantics: bool = False

    @model_validator(mode="after")
    def require_valid_limits_and_unique_overrides(self) -> "SimulationConfig":
        if self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        keys = [item.key for item in self.overrides]
        if len(keys) != len(set(keys)):
            raise ValueError("simulation override keys must be unique")
        if self.player_policy is PlayerPolicyKind.SCRIPTED and not self.scripted_steps:
            raise ValueError("scripted player policy requires at least one scripted step")
        if self.player_policy is not PlayerPolicyKind.SCRIPTED and self.scripted_steps:
            raise ValueError("scripted steps require the scripted player policy")
        positions = [(item.turn, item.actor_id) for item in self.scripted_steps]
        if len(positions) != len(set(positions)):
            raise ValueError("only one scripted step is allowed per turn and actor")
        return self

    def override_map(self) -> dict[str, Any]:
        return {item.key: item.value for item in self.overrides}


class SimulationBlocker(StrictModel):
    id: str
    kind: BlockerKind
    message: str
    entity_ids: tuple[str, ...] = ()
    assertion_ids: tuple[str, ...] = ()
    required_input: str | None = None
    overridable: bool = False


class SimulationEvent(StrictModel):
    sequence: int
    turn: int
    event_type: str
    actor_id: str | None = None
    entity_ids: tuple[str, ...] = ()
    data: dict[str, Any] = Field(default_factory=dict)
    assertion_ids: tuple[str, ...] = ()
    semantic_ids: tuple[str, ...] = ()


class SimulationCheckpoint(StrictModel):
    sequence: int
    state: "TabletopSimulationState"


class TabletopSimulationState(StrictModel):
    turn: int = 0
    active_player_index: int = 0
    escalation: str
    deck: tuple[str, ...]
    discard: tuple[str, ...] = ()
    tableau: dict[str, int]
    cube_meanings: dict[str, str]
    players: dict[str, "PlayerSimulationState"] = Field(default_factory=dict)
    status: SimulationStatus = SimulationStatus.READY

    @model_validator(mode="after")
    def cubes_are_non_negative_and_local(self) -> "TabletopSimulationState":
        negative = {entity_id: count for entity_id, count in self.tableau.items() if count < 0}
        if negative:
            raise ValueError(f"cube counts cannot be negative: {negative}")
        missing_meaning = set(self.tableau).difference(self.cube_meanings)
        if missing_meaning:
            raise ValueError(f"tableau cards need local cube meanings: {sorted(missing_meaning)}")
        return self


class PlayerSimulationState(StrictModel):
    character_id: str
    deck: tuple[str, ...]
    hand: tuple[str, ...]
    discard: tuple[str, ...] = ()
    elements: dict[str, int] = Field(default_factory=dict)
    fatigue_discarded: int = 0
    rests: int = 0


class SimulationMetrics(StrictModel):
    turns_completed: int
    cards_resolved: int
    escalation_steps: int
    improves: int
    worsens: int
    maximum_tableau_cubes: int
    blocker_count: int
    player_actions: int = 0
    rests: int = 0
    assists: int = 0
    fatigue_discarded: int = 0


class SimulationProvenance(StrictModel):
    schema_version: int = SIMULATION_SCHEMA_VERSION
    snapshot_id: str
    snapshot_digest: str
    backend_version: str
    plugin_id: str
    plugin_version: str
    config_digest: str
    player_policy: PlayerPolicyKind
    seeds: tuple[int, ...]
    runner: str
    container_image_digest: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SimulationRun(StrictModel):
    id: str
    config: SimulationConfig
    provenance: SimulationProvenance
    initial_state: TabletopSimulationState | None = None
    final_state: TabletopSimulationState | None = None
    events: tuple[SimulationEvent, ...] = ()
    state_trace: tuple[SimulationCheckpoint, ...] = ()
    blockers: tuple[SimulationBlocker, ...] = ()
    metrics: SimulationMetrics
    semantic_audit: "SemanticAudit | None" = None

    def evidence_claim(self) -> EvidenceClaim:
        ruleset_note = (
            f" using provisional ruleset {self.config.ruleset_id}"
            if self.config.ruleset_id
            else ""
        )
        if self.blockers:
            statement = (
                f"Simulation {self.id} stopped with {len(self.blockers)} explicit semantic blocker(s); "
                "the affected rules cannot yet produce an assumption-free trace."
            )
            scope = tuple(sorted({entity for blocker in self.blockers for entity in blocker.entity_ids}))
        else:
            statement = (
                f"Simulation {self.id}{ruleset_note} completed with status {self.final_state.status.value if self.final_state else 'invalid'} "
                f"after {self.metrics.turns_completed} turn(s)."
            )
            scope = (self.config.scenario_id, *self.config.character_ids)
        return EvidenceClaim(
            id=f"{self.id}:claim",
            statement=statement,
            scope=scope,
            evidence=(
                EvidenceLink(
                    evidence_id=self.id,
                    relation=EvidenceRelation.SUPPORTS,
                    rationale="Deterministic simulation result with reconstructable provenance.",
                    assumptions=tuple(
                        f"{item.key}: {item.rationale}" for item in self.config.overrides
                    )
                    + ((f"ruleset: {self.config.ruleset_id}",) if self.config.ruleset_id else ()),
                ),
            ),
        )


class SimulationPlugin(Protocol):
    id: str
    version: str

    def run(
        self, snapshot: DesignSnapshot, config: SimulationConfig, *, runner: str
    ) -> SimulationRun: ...


class InProcessSimulationRunner:
    id = "in_process"

    def run(
        self, plugin: SimulationPlugin, snapshot: DesignSnapshot, config: SimulationConfig
    ) -> SimulationRun:
        _require_simulation_config(config)
        return plugin.run(snapshot, config, runner=self.id)


class ScriptedPlayerPolicy:
    """Consumes exact declared actions and never substitutes a fallback choice."""

    def __init__(self, steps: tuple[ScriptedPolicyStep, ...]) -> None:
        self._steps = steps
        self._position = 0

    def consume(
        self,
        *,
        turn: int,
        actor_id: str,
        legal_steps: tuple[ScriptedPolicyStep, ...],
    ) -> tuple[ScriptedPolicyStep | None, SimulationBlocker | None]:
        if self._position >= len(self._steps):
            return None, self._blocker(turn, actor_id, "The scripted policy has no next step.")
        step = self._steps[self._position]
        if (step.turn, step.actor_id) != (turn, actor_id):
            return None, self._blocker(
                turn,
                actor_id,
                f"The next scripted step is for turn {step.turn}, actor {step.actor_id!r}.",
            )
        if step not in legal_steps:
            return None, self._blocker(
                turn,
                actor_id,
                f"Scripted action {step.action.value!r} is not legal in the current state.",
                entity_ids=tuple(item for item in (step.card_id, step.target_id) if item),
            )
        self._position += 1
        return step, None

    @staticmethod
    def _blocker(
        turn: int,
        actor_id: str,
        message: str,
        *,
        entity_ids: tuple[str, ...] = (),
    ) -> SimulationBlocker:
        return SimulationBlocker(
            id=f"policy:blocker:turn-{turn}:{actor_id}",
            kind=BlockerKind.INVALID_CONFIGURATION,
            message=message,
            entity_ids=entity_ids,
            required_input="Supply the exact next legal scripted step; no fallback policy is used.",
        )


class DockerSimulationRunner:
    """Run the immutable payload in a networkless, read-only container."""

    id = "docker"

    def __init__(
        self,
        image: str,
        *,
        image_digest: str,
        timeout_seconds: int = 60,
        memory: str = "256m",
        cpus: str = "1.0",
    ) -> None:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
            raise ValueError(
                "a pinned container image digest must be sha256:<64 lowercase hex>"
            )
        if not image or "@" in image or any(character.isspace() for character in image):
            raise ValueError("container image must be a non-empty unpinned image reference")
        if not 1 <= timeout_seconds <= 3600:
            raise ValueError("container timeout must be between 1 and 3600 seconds")
        self.image = image
        self.image_digest = image_digest
        self.timeout_seconds = timeout_seconds
        self.memory = memory
        self.cpus = cpus

    def command(self, input_dir: Path, output_dir: Path) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--pids-limit",
            "128",
            "--mount",
            f"type=bind,src={input_dir},dst=/input,readonly",
            "--mount",
            f"type=bind,src={output_dir},dst=/output",
            f"{self.image}@{self.image_digest}",
            "python",
            "-m",
            "playtrace.sim_worker",
            "/input/request.json",
            "/output/result.json",
        ]

    def request_payload(
        self,
        plugin: SimulationPlugin,
        snapshot: DesignSnapshot,
        config: SimulationConfig,
    ) -> dict[str, Any]:
        """Build the exact JSON-safe payload handed to the isolated worker."""

        _require_simulation_config(config)
        payload = {
            "plugin_id": plugin.id,
            "snapshot": snapshot.model_dump(mode="json"),
            "config": config.model_dump(mode="json"),
            "runner": self.id,
            "container_image_digest": self.image_digest,
        }
        try:
            bounded = _validate_json_value(
                payload,
                max_depth=SIMULATION_INPUT_MAX_DEPTH,
                max_nodes=SIMULATION_INPUT_MAX_NODES,
                max_bytes=SIMULATION_INPUT_MAX_BYTES,
            )
        except _InvalidJsonValue as exc:
            reason = _json_boundary_reason(
                exc.category,
                max_bytes=SIMULATION_INPUT_MAX_BYTES,
                max_depth=SIMULATION_INPUT_MAX_DEPTH,
                max_nodes=SIMULATION_INPUT_MAX_NODES,
            )
            raise SimulationJsonInputError(exc.category, reason) from None
        assert isinstance(bounded, dict)
        return bounded

    def run(
        self, plugin: SimulationPlugin, snapshot: DesignSnapshot, config: SimulationConfig
    ) -> SimulationRun:
        with tempfile.TemporaryDirectory(prefix="playtrace-sim-") as work_name:
            work_dir = Path(work_name)
            input_dir = work_dir / "input"
            output_dir = work_dir / "output"
            input_dir.mkdir(mode=0o755)
            output_dir.mkdir(mode=0o733)
            # Docker's non-root UID is unrelated to the host user. Bind only
            # these narrow child directories and grant the minimum traversal /
            # creation rights needed by the read-only input and writable output.
            os.chmod(input_dir, 0o755)
            os.chmod(output_dir, 0o733)
            request = self.request_payload(plugin, snapshot, config)
            request_path = input_dir / "request.json"
            request_path.write_text(
                json.dumps(
                    request,
                    allow_nan=False,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.chmod(request_path, 0o444)
            subprocess.run(
                self.command(input_dir.resolve(), output_dir.resolve()),
                check=True,
                timeout=self.timeout_seconds,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            result_path = output_dir / "result.json"
            result_info = result_path.lstat()
            if result_path.is_symlink() or not stat.S_ISREG(result_info.st_mode):
                raise ValueError("container result must be a regular non-symlink file")
            result = SimulationRun.model_validate(
                load_bounded_json_file(result_path)
            )
            return result.model_copy(
                update={
                    "provenance": result.provenance.model_copy(
                        update={"container_image_digest": self.image_digest}
                    )
                }
            )


def content_digest(value: Any) -> str:
    """Compatibility wrapper for generic, versioned Play Trace identities."""

    return canonical_digest(value, domain="generic-content")


def _require_simulation_config(config: object) -> None:
    """Reject the wrong top-level type at the runner's trusted Python boundary.

    Public callers construct ``SimulationConfig`` normally or use ``model_validate``.
    Pydantic's validation-bypassing ``model_construct`` API is deliberately a
    trusted-internal facility and is outside this runner contract; runners do not
    attempt to repair or sanitize arbitrary graphs created through it.
    """

    if not isinstance(config, SimulationConfig):
        raise TypeError("config must be a validated SimulationConfig")


def load_bounded_json_file(
    path: Path,
    *,
    max_bytes: int = SIMULATION_INPUT_MAX_BYTES,
    max_depth: int = SIMULATION_INPUT_MAX_DEPTH,
    max_nodes: int = SIMULATION_INPUT_MAX_NODES,
) -> JsonValue:
    """Read strict JSON only after byte-size preflight, then enforce budgets.

    This is the external file boundary used for simulation configuration and worker
    requests. It rejects pathological numeric tokens and nesting before Python creates
    large integers or the recursive JSON decoder is entered.
    """

    if path.stat().st_size > max_bytes:
        raise SimulationJsonInputError(
            "maximum_size", f"JSON input may be at most {max_bytes} bytes."
        )
    payload = path.read_bytes()
    if len(payload) > max_bytes:
        raise SimulationJsonInputError(
            "maximum_size", f"JSON input may be at most {max_bytes} bytes."
        )
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SimulationJsonInputError(
            "invalid_unicode", "JSON input must be valid UTF-8."
        ) from exc
    return decode_bounded_json(
        text,
        max_bytes=max_bytes,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )


def decode_bounded_json(
    text: str,
    *,
    max_bytes: int = SIMULATION_INPUT_MAX_BYTES,
    max_depth: int = SIMULATION_INPUT_MAX_DEPTH,
    max_nodes: int = SIMULATION_INPUT_MAX_NODES,
) -> JsonValue:
    """Decode a strict JSON document under size, depth, node, and number limits."""

    _preflight_raw_utf8_size(text, max_bytes=max_bytes)
    _preflight_json_structure(
        text, max_depth=max_depth, max_nodes=max_nodes
    )

    def parse_integer(token: str) -> int:
        digits = token[1:] if token.startswith("-") else token
        # All accepted values have at most 16 decimal digits. Avoid allocating an
        # arbitrary-precision integer for a hostile token.
        if len(digits) > 16:
            raise SimulationJsonInputError(
                "integer_out_of_range", _INVALID_OVERRIDE_REASONS["integer_out_of_range"]
            )
        value = int(token)
        if value < -JSON_SAFE_INTEGER_MAX or value > JSON_SAFE_INTEGER_MAX:
            raise SimulationJsonInputError(
                "integer_out_of_range", _INVALID_OVERRIDE_REASONS["integer_out_of_range"]
            )
        return value

    def parse_number(token: str) -> float:
        value = float(token)
        if not math.isfinite(value):
            raise SimulationJsonInputError(
                "non_finite_float", _INVALID_OVERRIDE_REASONS["non_finite_float"]
            )
        return value

    def reject_constant(token: str) -> None:
        raise SimulationJsonInputError(
            "non_finite_float", f"JSON constant {token!r} is not permitted."
        )

    try:
        value = json.loads(
            text,
            parse_int=parse_integer,
            parse_float=parse_number,
            parse_constant=reject_constant,
        )
    except SimulationJsonInputError:
        raise
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise SimulationJsonInputError("malformed_json", "Malformed JSON input.") from exc
    try:
        return _validate_json_value(
            value,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_bytes=max_bytes,
        )
    except _InvalidJsonValue as exc:
        reason = _json_boundary_reason(
            exc.category,
            max_bytes=max_bytes,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
        raise SimulationJsonInputError(exc.category, reason) from None


def _json_boundary_reason(
    category: str, *, max_bytes: int, max_depth: int, max_nodes: int
) -> str:
    if category == "maximum_size":
        return f"JSON input may be at most {max_bytes} UTF-8 bytes."
    if category == "maximum_depth":
        return f"JSON input may be at most {max_depth} levels deep."
    if category == "maximum_nodes":
        return f"JSON input may contain at most {max_nodes} nodes."
    return _INVALID_OVERRIDE_REASONS.get(
        category, "JSON input exceeds the configured boundary."
    )


def _preflight_json_structure(
    text: str, *, max_depth: int, max_nodes: int
) -> None:
    """Scan structural budgets before the recursive JSON decoder is entered.

    Each container, scalar string/value, and object key is one node, matching the
    post-decode validator. Container depth is the number of containers on a path: a
    root array or object has depth one, while a scalar document has depth zero. The
    scanner is intentionally not a second JSON parser: malformed syntax is still
    diagnosed by ``json.loads`` after budgets are known to be safe.
    """

    depth = 0
    nodes = 0
    in_string = False
    escaped = False
    in_atom = False

    def consume_node() -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise SimulationJsonInputError(
                "maximum_nodes",
                f"JSON input may contain at most {max_nodes} nodes.",
            )

    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            in_atom = False
            consume_node()
        elif character in "[{":
            in_atom = False
            consume_node()
            depth += 1
            if depth > max_depth:
                raise SimulationJsonInputError(
                    "maximum_depth", f"JSON input may be at most {max_depth} levels deep."
                )
        elif character in "]}":
            in_atom = False
            depth -= 1
        elif character in ",:":
            in_atom = False
        elif character.isspace():
            in_atom = False
        elif not in_atom:
            in_atom = True
            consume_node()


def _preflight_raw_utf8_size(text: str, *, max_bytes: int) -> None:
    """Count input bytes without allocating a second full encoded document."""

    total = 0
    for character in text:
        codepoint = ord(character)
        if codepoint < 0x80:
            total += 1
        elif codepoint < 0x800:
            total += 2
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise SimulationJsonInputError(
                "invalid_unicode", "JSON input must not contain unpaired surrogates."
            )
        elif codepoint < 0x10000:
            total += 3
        else:
            total += 4
        if total > max_bytes:
            raise SimulationJsonInputError(
                "maximum_size", f"JSON input may be at most {max_bytes} bytes."
            )


class _InvalidJsonValue(ValueError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


class _JsonBudget:
    def __init__(self, *, max_nodes: int, max_bytes: int) -> None:
        self.nodes = 0
        self.bytes = 0
        self.max_nodes = max_nodes
        self.max_bytes = max_bytes

    def consume(self) -> None:
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise _InvalidJsonValue("maximum_nodes")

    def consume_bytes(self, amount: int) -> None:
        self.bytes += amount
        if self.bytes > self.max_bytes:
            raise _InvalidJsonValue("maximum_size")


def _validate_json_value(
    value: Any,
    *,
    max_depth: int,
    max_nodes: int,
    max_bytes: int,
) -> JsonValue:
    """Validate and canonicalize exact JSON types with incremental budgets."""

    budget = _JsonBudget(max_nodes=max_nodes, max_bytes=max_bytes)
    return _validate_json_node(
        value,
        depth=0,
        max_depth=max_depth,
        active=set(),
        budget=budget,
    )


def _validate_json_node(
    value: Any,
    *,
    depth: int,
    max_depth: int,
    active: set[int],
    budget: _JsonBudget,
) -> Any:
    budget.consume()

    value_type = type(value)
    if value is None or value_type is bool:
        budget.consume_bytes(4 if value is None or value is True else 5)
        return value
    if value_type is int:
        if value < -JSON_SAFE_INTEGER_MAX or value > JSON_SAFE_INTEGER_MAX:
            raise _InvalidJsonValue("integer_out_of_range")
        budget.consume_bytes(len(str(value)))
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise _InvalidJsonValue("non_finite_float")
        budget.consume_bytes(len(json.dumps(value)))
        return value
    if value_type is str:
        _consume_json_string(value, budget)
        return value
    if value_type is list:
        container_depth = depth + 1
        if container_depth > max_depth:
            raise _InvalidJsonValue("maximum_depth")
        identity = id(value)
        if identity in active:
            raise _InvalidJsonValue("cycle")
        active.add(identity)
        try:
            budget.consume_bytes(2 + max(0, len(value) - 1))
            return [
                _validate_json_node(
                    item,
                    depth=container_depth,
                    max_depth=max_depth,
                    active=active,
                    budget=budget,
                )
                for item in value
            ]
        finally:
            active.remove(identity)
    if value_type is dict:
        container_depth = depth + 1
        if container_depth > max_depth:
            raise _InvalidJsonValue("maximum_depth")
        identity = id(value)
        if identity in active:
            raise _InvalidJsonValue("cycle")
        active.add(identity)
        try:
            budget.consume_bytes(2 + max(0, len(value) - 1) + len(value))
            for key in value:
                budget.consume()
                if type(key) is not str:
                    raise _InvalidJsonValue("non_string_key")
                _consume_json_string(key, budget)
            return {
                key: _validate_json_node(
                    value[key],
                    depth=container_depth,
                    max_depth=max_depth,
                    active=active,
                    budget=budget,
                )
                for key in sorted(value)
            }
        finally:
            active.remove(identity)
    raise _InvalidJsonValue("unsupported_type")


def _consume_json_string(value: str, budget: _JsonBudget) -> None:
    """Count exact compact JSON UTF-8 bytes without allocating an encoded copy."""

    budget.consume_bytes(2)
    # Every code point needs at least one byte. This rejects large strings before
    # traversing or encoding them.
    if len(value) > budget.max_bytes - budget.bytes:
        raise _InvalidJsonValue("maximum_size")
    for character in value:
        codepoint = ord(character)
        if codepoint in (0x22, 0x5C) or character in "\b\f\n\r\t":
            budget.consume_bytes(2)
        elif codepoint < 0x20:
            budget.consume_bytes(6)
        elif codepoint < 0x80:
            budget.consume_bytes(1)
        elif codepoint < 0x800:
            budget.consume_bytes(2)
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise _InvalidJsonValue("invalid_unicode")
        elif codepoint < 0x10000:
            budget.consume_bytes(3)
        else:
            budget.consume_bytes(4)


def snapshot_content_digest(snapshot: DesignSnapshot) -> str:
    """Digest design meaning while ignoring extraction clock timestamps."""

    payload = snapshot.model_dump(mode="json", exclude={"created_at"})
    payload["capabilities"] = sorted(payload["capabilities"])
    for revision in payload["source_revisions"]:
        revision.pop("observed_at", None)
    for assertion in payload["assertions"]:
        assertion["provenance"].pop("extracted_at", None)
    return canonical_digest(payload, domain="design-snapshot")


from playtrace.semantics import SemanticAudit  # noqa: E402  (forward model rebuild)

SimulationRun.model_rebuild()
