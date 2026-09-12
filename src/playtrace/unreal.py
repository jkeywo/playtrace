"""Read-only local Unreal/Git sources and versioned server telemetry evidence.

Native binaries are inventoried, never interpreted as executable semantics.
No game-specific IDs, rule values, or fixtures belong in this module.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import tomllib
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from playtrace.canonical import canonical_digest
from playtrace.evidence import (
    DependencyFingerprint, ExperimentArtifactProvenance, ExperimentEvidence,
    ExperimentEvidenceBundle, ExperimentEvidenceKind, HypothesisEvidenceLink,
)
from playtrace.models import (
    Assertion, AuthoredStatus, DesignEntity, DesignSnapshot, EvidenceRelation,
    Hypothesis, ProvenanceRef, SourceMedium, SourceRevision, StrictModel,
)

ADAPTER_VERSION = "unreal-local-v1"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_BYTES = 64 * 1024 * 1024


class LocalSource(StrictModel):
    id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    kind: Literal["markdown", "unreal_project", "text", "binary"]
    role: str = "reference"
    required: bool = True

    @model_validator(mode="after")
    def safe_relative_path(self) -> "LocalSource":
        if (self.id in ("repository", "engine") or "\\" in self.path or PureWindowsPath(self.path).drive
                or PurePosixPath(self.path).is_absolute() or ".." in PurePosixPath(self.path).parts):
            raise ValueError("source needs a repository-relative POSIX path and a nonreserved ID")
        return self


class LocalProjectManifest(StrictModel):
    manifest_version: Literal[2]
    adapter: Literal["unreal-local"]
    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    repository: str
    engine_version: str
    capabilities: frozenset[str] = frozenset()
    sources: tuple[LocalSource, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_sources(self) -> "LocalProjectManifest":
        if len({s.id for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate local source IDs")
        if len({s.path.casefold() for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate local source paths")
        if sum(s.role == "design" for s in self.sources) > 1:
            raise ValueError("v1 telemetry binds one primary design source; declare other sources as reference")
        return self


def load_local_manifest(path: Path) -> LocalProjectManifest:
    return LocalProjectManifest.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


def _git(root: Path, *args: str) -> bytes:
    process = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root), *args],
        capture_output=True, check=False, timeout=30,
    )
    if process.returncode:
        raise ValueError(f"Cannot inspect game Git repository: {process.stderr.decode(errors='replace').strip()}")
    return process.stdout


def repository_fingerprint(root: Path) -> tuple[str, str, bool]:
    """Hash ordinal-sorted path-NUL-SHA256 rows of tracked and nonignored files.

    Matches the game launcher contract; dirty inputs must still be retained by
    the producer. Symlink escapes are rejected, not read through.
    """
    root = root.resolve()
    revision = _git(root, "rev-parse", "HEAD").decode().strip()
    dirty = bool(_git(root, "status", "--porcelain"))
    names = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode("utf-8").split("\0")
    rows = []
    for name in sorted(set(filter(None, names))):
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Repository input escapes project root: {name}")
        if not path.is_file():
            continue  # Deletions are represented by absence, plus Git revision/dirty status.
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        rows.append(f"{name}\0{digest}")
    return revision, hashlib.sha256("\n".join(rows).encode()).hexdigest(), dirty


def _authored_status(text: str) -> AuthoredStatus:
    labels = []
    for marker, status in (("LOCKED", AuthoredStatus.CONFIRMED), ("PROVISIONAL", AuthoredStatus.WORKING),
                           ("OPEN", AuthoredStatus.OPEN_QUESTION), ("CONTENT TBD", AuthoredStatus.PLACEHOLDER)):
        if re.search(rf"(?<!\w){marker}(?!\w)", text):
            labels.append(status)
    # A mixed-status table/legend is not a single confirmed statement.
    return labels[0] if len(labels) == 1 else AuthoredStatus.UNSPECIFIED


class UnrealLocalAdapter:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.manifest = load_local_manifest(self.root / "playtrace.toml")

    def snapshot(self) -> DesignSnapshot:
        revision, repo_digest, dirty = repository_fingerprint(self.root)
        revisions = [SourceRevision(id=f"revision:repository:{repo_digest}", source_id="repository",
            medium=SourceMedium.GIT, locator=".", revision=revision, content_digest=repo_digest),
            SourceRevision(id=f"revision:engine:{self.manifest.engine_version}", source_id="engine",
                medium=SourceMedium.GIT, locator="playtrace.toml#engine_version", revision=self.manifest.engine_version)]
        entities = [DesignEntity(id="repository", kind="source_inventory", name=self.manifest.name,
            attributes={"working_tree_dirty": dirty, "engine_version": self.manifest.engine_version})]
        assertions = []
        omissions = ["C++/config are inventoried, not compiled into gameplay semantics.",
                     "Blueprints, maps and other native binary semantics are unsupported.",
                     "Verbatim Markdown fragments preserve explicit status; unmarked text has unspecified status."]
        for source in self.manifest.sources:
            path = (self.root / source.path).resolve()
            if not path.is_relative_to(self.root):
                raise ValueError(f"Source escapes project root: {source.path}")
            if not path.is_file():
                if source.required:
                    raise ValueError(f"Required source missing: {source.path}")
                omissions.append(f"Optional source missing: {source.path}")
                continue
            if source.kind == "binary":
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                raw = b""
            else:
                with path.open("rb") as stream:
                    raw = stream.read(MAX_SOURCE_BYTES + 1)
                if len(raw) > MAX_SOURCE_BYTES:
                    raise ValueError(f"Text source exceeds {MAX_SOURCE_BYTES} bytes: {source.path}")
                digest = hashlib.sha256(raw).hexdigest()
            rev_id = f"revision:{source.id}:{digest}"
            revisions.append(SourceRevision(id=rev_id, source_id=source.id, medium=SourceMedium.GIT,
                locator=source.path, revision=revision, content_digest=digest))
            entities.append(DesignEntity(id=source.id, kind="canonical_source", name=source.path,
                attributes={"source_kind": source.kind, "role": source.role, "semantics": "verbatim" if source.kind == "markdown" else "inventory_only"}))
            if source.kind == "markdown":
                occurrences: Counter[str] = Counter()
                for line, fragment in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
                    if not fragment.strip() or fragment.lstrip().startswith("#"):
                        continue
                    stable = hashlib.sha256(fragment.encode()).hexdigest()[:24]
                    occurrences[stable] += 1
                    assertions.append(Assertion(id=f"{source.id}:{stable}:{occurrences[stable]}", subject_id=source.id,
                        predicate="verbatim_source_fragment", value=fragment, authored_status=_authored_status(fragment),
                        provenance=ProvenanceRef(source_revision_id=rev_id, location=f"{source.path}#L{line}", extractor=ADAPTER_VERSION)))
            elif source.kind == "unreal_project":
                descriptor = json.loads(raw)
                if not isinstance(descriptor, dict) or not isinstance(descriptor.get("FileVersion"), int):
                    raise ValueError("Invalid Unreal project descriptor")
        if repository_fingerprint(self.root) != (revision, repo_digest, dirty):
            raise ValueError("Game sources changed during snapshot; retry from stable inputs")
        return DesignSnapshot(id=f"snapshot:{self.manifest.project_id}:{repo_digest}", project_id=self.manifest.project_id,
            adapter_id=ADAPTER_VERSION, source_revisions=tuple(revisions), entities=tuple(entities), assertions=tuple(assertions),
            capabilities=self.manifest.capabilities, omissions=tuple(omissions))


class UnrealEvent(StrictModel):
    schema_version: Literal[1]
    run_id: str
    sequence: int = Field(strict=True, ge=0)
    event_type: str = Field(min_length=1)
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    authority: Literal["server"]
    visibility: Literal["developer"]
    data: dict[str, Any]


class UnrealCapture(StrictModel):
    events: tuple[UnrealEvent, ...]
    artifact_digest: str
    artifact_path: str


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _number(value: Any, name: str) -> float:
    _require(type(value) in (int, float), f"{name} must be numeric")
    try:
        _require(math.isfinite(value) and value >= 0, f"{name} must be finite and nonnegative")
    except OverflowError as error:
        raise ValueError(f"{name} is out of range") from error
    return value


def load_unreal_capture(path: Path) -> UnrealCapture:
    with path.open("rb") as stream:
        raw = stream.read(MAX_CAPTURE_BYTES + 1)
    _require(len(raw) <= MAX_CAPTURE_BYTES, "Capture exceeds size limit")
    events = []
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        _require(bool(line.strip()), f"Blank event line {number}")
        data = json.loads(line, object_pairs_hook=unique_object)
        _require(isinstance(data, dict) and type(data.get("schema_version")) is int, "Invalid capture envelope")
        _require(type(data.get("elapsed_seconds")) in (float, int), "Invalid elapsed time")
        events.append(UnrealEvent.model_validate(data))
    _require(len(events) >= 2, "Empty or incomplete capture")
    run_id = str(UUID(events[0].run_id))
    elapsed = -1.0
    for index, event in enumerate(events):
        _require(event.sequence == index and str(UUID(event.run_id)) == run_id, "Sequence gap or mixed run IDs")
        _require(event.elapsed_seconds >= elapsed, "Non-monotonic event timing")
        elapsed = event.elapsed_seconds
    _require(events[0].event_type == "run.started" and events[-1].event_type == "run.ended", "Incomplete run lifecycle")
    _require(not any(e.event_type in ("run.started", "run.ended") for e in events[1:-1]), "Repeated lifecycle events")
    meta = events[0].data
    for key in ("project_id", "scenario_id", "run_kind", "engine_version", "capture_version"):
        _require(isinstance(meta.get(key), str) and bool(meta[key]), f"Missing {key}")
    _require(type(meta.get("seed")) is int and -(2**31) <= meta["seed"] < 2**31, "Invalid seed")
    _require(type(meta.get("working_tree_dirty")) is bool, "Missing dirty status")
    for key, pattern in (("GameRevision", r"[a-f0-9]{40}|[a-f0-9]{64}"),
                         ("SourceDigest", r"[a-f0-9]{64}"), ("GDDDigest", r"[a-f0-9]{64}")):
        value = meta.get(key)
        _require(isinstance(value, str) and (value == "unrecorded" or bool(re.fullmatch(pattern, value))), f"Invalid {key}")
    _require(events[-1].data.get("outcome") in ("victory", "defeat", "aborted"), "Invalid outcome")
    # Shared health-transition contract, independent of a game's damage tuning/GDD.
    actors: dict[str, dict] = {}
    tick = -1
    for event in events[1:-1]:
        data = event.data
        if "tick" in data:
            _require(type(data["tick"]) is int and data["tick"] >= tick, "Invalid/non-monotonic logical tick")
            tick = data["tick"]
        if event.event_type == "combat.spawned":
            entity = data.get("entity_id")
            _require(isinstance(entity, str) and bool(entity) and entity not in actors, "Duplicate/invalid combat entity")
            _require(data.get("team") in ("investigator", "enemy"), "Unknown combat team")
            health = _number(data.get("health"), "health")
            shield = _number(data.get("shield"), "shield")
            _require(health > 0, "Combatant must spawn alive")
            actors[entity] = {"health": health, "max_health": health, "shield": shield, "team": data["team"]}
        elif event.event_type == "combat.shield_gained":
            target = actors.get(data.get("entity_id"))
            _require(target is not None, "Shield gain references unknown combatant")
            before = _number(data.get("shield_before"), "shield_before")
            after = _number(data.get("shield_after"), "shield_after")
            amount = _number(data.get("amount"), "amount")
            _require(before == target["shield"], "Shield gain contradicts prior state")
            _require(amount > 0 and after > before and math.isclose(after, before + amount, abs_tol=1e-4),
                     "Shield gain arithmetic mismatch")
            target["shield"] = after
        elif event.event_type == "combat.damage":
            actor, target = actors.get(data.get("actor_id")), actors.get(data.get("target_id"))
            _require(actor is not None and target is not None, "Damage references unknown combatant")
            _require(actor["health"] > 0 and target["health"] > 0 and actor["team"] != target["team"], "Illegal damage participants")
            damage = _number(data.get("damage"), "damage")
            before, after = _number(data.get("health_before"), "health_before"), _number(data.get("health_after"), "health_after")
            shield_before, shield_after = _number(data.get("shield_before"), "shield_before"), _number(data.get("shield_after"), "shield_after")
            _require(before == target["health"] and shield_before == target["shield"], "Damage contradicts prior state")
            _require(math.isclose(shield_after, max(0, shield_before - damage), abs_tol=1e-4)
                     and math.isclose(after, max(0, before - max(0, damage - shield_before)), abs_tol=1e-4), "Damage arithmetic mismatch")
            target.update(health=after, shield=shield_after)
        elif event.event_type in ("combat.downed", "combat.killed"):
            target = actors.get(data.get("entity_id"))
            _require(target is not None and target["health"] == 0, "Down/kill without zero health")
            _require(target["team"] == ("enemy" if event.event_type == "combat.killed" else "investigator"), "Wrong defeat state for team")
        elif event.event_type == "combat.revived":
            actor, target = actors.get(data.get("actor_id")), actors.get(data.get("target_id"))
            _require(actor is not None and target is not None and actor is not target, "Unknown revive participants")
            after = _number(data.get("health_after"), "health_after")
            _require(actor["team"] == target["team"] == "investigator" and actor["health"] > 0 and target["health"] == 0
                     and 0 < after <= target["max_health"], "Illegal revive")
            target["health"] = after
        elif event.event_type == "control.changed":
            _require(data.get("entity_id") in actors and data.get("control") in ("human", "bot"), "Invalid control transfer")
        elif event.event_type.startswith("combat."):
            raise ValueError(f"Unsupported combat semantics: {event.event_type}")
    if actors and events[-1].data["outcome"] != "aborted":
        outcome = events[-1].data["outcome"]
        players = [a for a in actors.values() if a["team"] == "investigator"]
        enemies = [a for a in actors.values() if a["team"] == "enemy"]
        _require(bool(players) and bool(enemies), "Missing combat teams")
        if "investigator_slots" in meta:
            _require(type(meta["investigator_slots"]) is int and len(players) == meta["investigator_slots"], "Incomplete investigator roster")
        _require((outcome == "victory" and any(a["health"] > 0 for a in players) and all(a["health"] == 0 for a in enemies))
                 or (outcome == "defeat" and all(a["health"] == 0 for a in players)), "Outcome contradicts combat state")
    return UnrealCapture(events=tuple(events), artifact_digest=hashlib.sha256(raw).hexdigest(), artifact_path=str(path.resolve()))


def unreal_evidence(snapshot: DesignSnapshot, capture: UnrealCapture, hypothesis: Hypothesis,
                    relation: EvidenceRelation, rationale: str) -> ExperimentEvidenceBundle:
    meta = capture.events[0].data
    _require(snapshot.adapter_id == ADAPTER_VERSION, "Unreal telemetry requires a local Unreal snapshot")
    _require(meta["project_id"] == snapshot.project_id, "Capture belongs to a different project")
    _require(bool(rationale.strip()), "An explicit evidence rationale is required")
    outcome = capture.events[-1].data["outcome"]
    counts = Counter(e.event_type for e in capture.events)
    configuration_observations = tuple(f"{key}={meta[key]}" for key in
        ("test_profile", "bot_policy", "initial_bot_count", "investigator_slots", "logical_step_seconds") if key in meta)
    controls = Counter(e.data.get("control", "unrecorded") for e in capture.events if e.event_type == "control.changed")
    omissions = meta.get("omissions", [])
    _require(isinstance(omissions, list) and all(isinstance(s, str) for s in omissions), "Invalid omissions")
    evidence_id = f"evidence:telemetry:{capture.artifact_digest}"
    repository = next(r for r in snapshot.source_revisions if r.source_id == "repository")
    dependencies = [DependencyFingerprint(dependency_id=f"source:{repository.source_id}", revision=meta["GameRevision"],
        digest=meta["SourceDigest"], role="source_revision"),
        DependencyFingerprint(dependency_id="source:engine", revision=meta["engine_version"].split("-")[0], role="source_revision")]
    for entity in snapshot.entities:
        if entity.attributes.get("role") == "design":
            dependencies.append(DependencyFingerprint(dependency_id=f"source:{entity.id}", revision=meta["GameRevision"],
                digest=meta["GDDDigest"], role="source_revision"))
    assumptions = (
        f"run_kind={meta['run_kind']}; this observation does not establish whole-game balance or simulation fidelity.",
        "Unreal timestamps measure wall time, not deterministic replay time.",
        "Source digests do not reconstruct uncommitted inputs; retain the matching source/patch.",
        *(f"Unsupported semantics: {item}" for item in omissions),
    )
    record = ExperimentEvidence(id=evidence_id, project_id=snapshot.project_id, snapshot_id=snapshot.id,
        kind=ExperimentEvidenceKind.TELEMETRY_RUN,
        summary=f"Recorded {meta['run_kind']} run {capture.events[0].run_id}: {outcome}, {len(capture.events)} server events.",
        observations=(f"seed={meta['seed']}", f"engine={meta['engine_version']}",
                      f"working_tree_dirty={meta['working_tree_dirty']}",
                      *configuration_observations,
                      *(f"control_transfers_to_{key}={value}" for key, value in sorted(controls.items())),
                      *(f"{key}={value}" for key, value in sorted(counts.items()))),
        scope=(meta["scenario_id"],), assumptions=assumptions,
        provenance=ExperimentArtifactProvenance(artifact_digest=capture.artifact_digest, artifact_path=capture.artifact_path,
            source_result_ids=(capture.events[0].run_id,), config_digests=(canonical_digest(meta, domain="unreal-run-config"),),
            plugin_versions=(meta["capture_version"],), runners=(meta["engine_version"],), ruleset_ids=(meta["run_kind"],)),
        dependencies=tuple(dependencies),
        hypothesis_link=HypothesisEvidenceLink(hypothesis_id=hypothesis.id, evidence_id=evidence_id,
            relation=relation, rationale=rationale),
    ).assess(snapshot)
    return ExperimentEvidenceBundle(hypothesis=hypothesis, evidence=record, claim=record.to_claim())
