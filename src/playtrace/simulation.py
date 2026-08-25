"""Deterministic simulation contracts shared by generated and native runners."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field, model_validator

from playtrace.models import (
    DesignSnapshot,
    EvidenceClaim,
    EvidenceLink,
    EvidenceRelation,
    StrictModel,
)


SIMULATION_SCHEMA_VERSION = 1


class PlayerPolicyKind(StrEnum):
    PASSIVE = "passive"
    GREEDY = "greedy"
    RISK_AVERSE = "risk_averse"
    RANDOM_CONSTRAINED = "random_constrained"
    SCRIPTED = "scripted"


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
    value: Any
    rationale: str
    canonical: bool = False


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

    @model_validator(mode="after")
    def require_valid_limits_and_unique_overrides(self) -> "SimulationConfig":
        if self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        keys = [item.key for item in self.overrides]
        if len(keys) != len(set(keys)):
            raise ValueError("simulation override keys must be unique")
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
        return plugin.run(snapshot, config, runner=self.id)


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
        if not image_digest.startswith("sha256:"):
            raise ValueError("a pinned container image digest is required")
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

    def run(
        self, plugin: SimulationPlugin, snapshot: DesignSnapshot, config: SimulationConfig
    ) -> SimulationRun:
        with tempfile.TemporaryDirectory(prefix="playtrace-sim-input-") as input_name, tempfile.TemporaryDirectory(
            prefix="playtrace-sim-output-"
        ) as output_name:
            input_dir, output_dir = Path(input_name), Path(output_name)
            request = {
                "plugin_id": plugin.id,
                "snapshot": snapshot.model_dump(mode="json"),
                "config": config.model_dump(mode="json"),
                "runner": self.id,
                "container_image_digest": self.image_digest,
            }
            (input_dir / "request.json").write_text(
                json.dumps(request, separators=(",", ":")), encoding="utf-8"
            )
            subprocess.run(
                self.command(input_dir.resolve(), output_dir.resolve()),
                check=True,
                timeout=self.timeout_seconds,
                capture_output=True,
                text=True,
            )
            result = SimulationRun.model_validate_json(
                (output_dir / "result.json").read_text(encoding="utf-8")
            )
            return result.model_copy(
                update={
                    "provenance": result.provenance.model_copy(
                        update={"container_image_digest": self.image_digest}
                    )
                }
            )


def content_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def snapshot_content_digest(snapshot: DesignSnapshot) -> str:
    """Digest design meaning while ignoring extraction clock timestamps."""

    payload = snapshot.model_dump(mode="json", exclude={"created_at"})
    payload["capabilities"] = sorted(payload["capabilities"])
    for revision in payload["source_revisions"]:
        revision.pop("observed_at", None)
    for assertion in payload["assertions"]:
        assertion["provenance"].pop("extracted_at", None)
    return content_digest(payload)
