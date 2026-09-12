"""Typed experiment evidence, hypothesis links, and deterministic applicability."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from pydantic import Field, model_validator

from playtrace.canonical import canonical_digest
from playtrace.models import (
    DesignSnapshot,
    EvidenceClaim,
    EvidenceLink,
    EvidenceRelation,
    Hypothesis,
    StrictModel,
)
from playtrace.simulation import (
    SimulationRun,
    snapshot_content_digest,
)
from playtrace.tension import ABComparison, TensionBatch, compare_batches


class ExperimentEvidenceKind(StrEnum):
    TELEMETRY_RUN = "telemetry_run"
    SIMULATION_RUN = "simulation_run"
    TENSION_BATCH = "tension_batch"
    AB_COMPARISON = "ab_comparison"


class ExperimentApplicability(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class DependencyFingerprint(StrictModel):
    dependency_id: str
    revision: str
    digest: str | None = None
    role: str = "depends_on"
    required: bool = True


class HypothesisEvidenceLink(StrictModel):
    hypothesis_id: str
    evidence_id: str
    relation: EvidenceRelation
    rationale: str
    assumptions: tuple[str, ...] = ()


class ExperimentArtifactProvenance(StrictModel):
    artifact_digest: str
    artifact_path: str | None = None
    source_result_ids: tuple[str, ...] = ()
    source_artifact_digests: dict[str, str] = Field(default_factory=dict)
    snapshot_ids: tuple[str, ...] = ()
    snapshot_digests: tuple[str, ...] = ()
    config_digests: tuple[str, ...] = ()
    plugin_versions: tuple[str, ...] = ()
    runners: tuple[str, ...] = ()
    ruleset_ids: tuple[str, ...] = ()
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ApplicabilityAssessment(StrictModel):
    applicability: ExperimentApplicability
    reasons: tuple[str, ...] = ()
    missing_dependency_ids: tuple[str, ...] = ()
    mismatched_dependency_ids: tuple[str, ...] = ()


class ExperimentEvidence(StrictModel):
    id: str
    project_id: str
    snapshot_id: str
    kind: ExperimentEvidenceKind
    summary: str
    observations: tuple[str, ...]
    scope: tuple[str, ...]
    assumptions: tuple[str, ...]
    provenance: ExperimentArtifactProvenance
    dependencies: tuple[DependencyFingerprint, ...]
    hypothesis_link: HypothesisEvidenceLink
    applicability: ExperimentApplicability = ExperimentApplicability.CURRENT
    applicability_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def link_targets_this_record(self) -> "ExperimentEvidence":
        if self.hypothesis_link.evidence_id != self.id:
            raise ValueError("hypothesis evidence link must target this evidence record")
        return self

    def to_claim(self) -> EvidenceClaim:
        link = self.hypothesis_link
        return EvidenceClaim(
            id=f"{self.id}:claim",
            statement=self.summary,
            scope=self.scope,
            evidence=(
                EvidenceLink(
                    evidence_id=self.id,
                    relation=link.relation,
                    rationale=link.rationale,
                    assumptions=tuple(dict.fromkeys((*self.assumptions, *link.assumptions))),
                ),
            ),
            applicability=self.applicability.value,
        )

    def assess(
        self,
        snapshot: DesignSnapshot,
        current_dependencies: Iterable[DependencyFingerprint] = (),
    ) -> "ExperimentEvidence":
        assessment = assess_applicability(self, snapshot, current_dependencies)
        return self.model_copy(
            update={
                "applicability": assessment.applicability,
                "applicability_reasons": assessment.reasons,
            }
        )


class ExperimentEvidenceBundle(StrictModel):
    hypothesis: Hypothesis
    evidence: ExperimentEvidence
    claim: EvidenceClaim

    @model_validator(mode="after")
    def records_are_consistent(self) -> "ExperimentEvidenceBundle":
        link = self.evidence.hypothesis_link
        if link.hypothesis_id != self.hypothesis.id:
            raise ValueError("bundle evidence must link to the bundled hypothesis")
        if len(self.claim.evidence) != 1:
            raise ValueError("experiment claim must contain exactly one evidence link")
        claim_link = self.claim.evidence[0]
        if claim_link.evidence_id != self.evidence.id or claim_link.relation is not link.relation:
            raise ValueError("experiment claim must preserve its evidence ID and relation")
        return self


def snapshot_dependencies(snapshot: DesignSnapshot) -> tuple[DependencyFingerprint, ...]:
    items = [
        DependencyFingerprint(
            dependency_id=f"snapshot:{snapshot.project_id}",
            revision=snapshot.id,
            digest=snapshot_content_digest(snapshot),
            role="design_snapshot",
        )
    ]
    items.extend(
        DependencyFingerprint(
            dependency_id=f"source:{revision.source_id}",
            revision=revision.revision,
            digest=revision.content_digest,
            role="source_revision",
        )
        for revision in snapshot.source_revisions
    )
    return tuple(items)


def assess_applicability(
    evidence: ExperimentEvidence,
    snapshot: DesignSnapshot,
    current_dependencies: Iterable[DependencyFingerprint] = (),
) -> ApplicabilityAssessment:
    current_items = (*snapshot_dependencies(snapshot), *tuple(current_dependencies))
    current = {(item.role, item.dependency_id): item for item in current_items}
    missing: list[str] = []
    mismatched: list[str] = []
    reasons: list[str] = []
    for recorded in evidence.dependencies:
        actual = current.get((recorded.role, recorded.dependency_id))
        # A comparison gives each arm its own dependency role so two configs with
        # the same ID cannot overwrite one another. Snapshot/source facts are
        # different: both arms deliberately assess against the one shared current
        # design snapshot, so those roles may fall back to its unqualified role.
        shared_role = _shared_snapshot_role(recorded.role)
        if actual is None and shared_role is not None:
            actual = current.get((shared_role, recorded.dependency_id))
        if actual is None:
            if recorded.required:
                missing.append(recorded.dependency_id)
                reasons.append(f"required dependency {recorded.dependency_id} is missing")
            continue
        differences = []
        if recorded.revision != actual.revision:
            differences.append(f"revision {recorded.revision!r} -> {actual.revision!r}")
        if recorded.digest is not None and recorded.digest != actual.digest:
            differences.append(f"digest {recorded.digest!r} -> {actual.digest!r}")
        if differences:
            mismatched.append(recorded.dependency_id)
            reasons.append(f"dependency {recorded.dependency_id} changed: {', '.join(differences)}")
    applicability = (
        ExperimentApplicability.STALE
        if missing or mismatched
        else ExperimentApplicability.CURRENT
    )
    return ApplicabilityAssessment(
        applicability=applicability,
        reasons=tuple(reasons),
        missing_dependency_ids=tuple(missing),
        mismatched_dependency_ids=tuple(mismatched),
    )


def _shared_snapshot_role(role: str) -> str | None:
    for shared_role in ("design_snapshot", "source_revision"):
        if role == shared_role or role.endswith(f"_{shared_role}"):
            return shared_role
    return None


def simulation_evidence(
    *,
    project_id: str,
    snapshot: DesignSnapshot,
    hypothesis: Hypothesis,
    run: SimulationRun,
    relation: EvidenceRelation,
    rationale: str,
    assumptions: Iterable[str] = (),
    artifact_path: str | Path | None = None,
) -> ExperimentEvidenceBundle:
    explicit_assumptions = tuple(assumptions)
    run_assumptions = tuple(
        f"{item.key}: {item.rationale}" for item in run.config.overrides
    ) + ((f"ruleset: {run.config.ruleset_id}",) if run.config.ruleset_id else ())
    all_assumptions = tuple(dict.fromkeys((*explicit_assumptions, *run_assumptions)))
    evidence_id = f"evidence:{run.id}"
    plugin_dependency = DependencyFingerprint(
        dependency_id=f"plugin:{run.provenance.plugin_id}",
        revision=run.provenance.plugin_version,
        role="simulation_plugin",
        required=False,
    )
    config_dependency = DependencyFingerprint(
        dependency_id=f"config:{run.config.id}",
        revision=run.config.id,
        digest=run.provenance.config_digest,
        role="simulation_config",
        required=False,
    )
    design_dependencies = list(snapshot_dependencies(snapshot))
    design_dependencies[0] = design_dependencies[0].model_copy(
        update={
            "revision": run.provenance.snapshot_id,
            "digest": run.provenance.snapshot_digest,
        }
    )
    record = ExperimentEvidence(
        id=evidence_id,
        project_id=project_id,
        snapshot_id=run.provenance.snapshot_id,
        kind=ExperimentEvidenceKind.SIMULATION_RUN,
        summary=run.evidence_claim().statement,
        observations=(
            f"status={run.final_state.status.value if run.final_state else 'blocked'}",
            f"turns={run.metrics.turns_completed}",
            f"blockers={len(run.blockers)}",
        ),
        scope=(run.config.scenario_id, *run.config.character_ids),
        assumptions=all_assumptions,
        provenance=ExperimentArtifactProvenance(
            artifact_digest=canonical_digest(run, domain="simulation-run-artifact"),
            artifact_path=str(artifact_path) if artifact_path else None,
            source_result_ids=(run.id,),
            source_artifact_digests={
                run.id: canonical_digest(run, domain="simulation-run-artifact")
            },
            snapshot_ids=(run.provenance.snapshot_id,),
            snapshot_digests=(run.provenance.snapshot_digest,),
            config_digests=(run.provenance.config_digest,),
            plugin_versions=(
                f"{run.provenance.plugin_id}@{run.provenance.plugin_version}",
            ),
            runners=(run.provenance.runner,),
            ruleset_ids=(run.config.ruleset_id,) if run.config.ruleset_id else (),
        ),
        dependencies=(*design_dependencies, plugin_dependency, config_dependency),
        hypothesis_link=HypothesisEvidenceLink(
            hypothesis_id=hypothesis.id,
            evidence_id=evidence_id,
            relation=relation,
            rationale=rationale,
            assumptions=explicit_assumptions,
        ),
    ).assess(snapshot)
    return ExperimentEvidenceBundle(
        hypothesis=hypothesis, evidence=record, claim=record.to_claim()
    )


def tension_batch_evidence(
    *,
    project_id: str,
    snapshot: DesignSnapshot,
    hypothesis: Hypothesis,
    batch: TensionBatch,
    relation: EvidenceRelation,
    rationale: str,
    assumptions: Iterable[str] = (),
    artifact_path: str | Path | None = None,
) -> ExperimentEvidenceBundle:
    _ensure_single_snapshot_batch(batch, role="batch")
    evidence_id = f"evidence:{batch.id}"
    assumptions = tuple(assumptions)
    record = ExperimentEvidence(
        id=evidence_id,
        project_id=project_id,
        snapshot_id=snapshot.id,
        kind=ExperimentEvidenceKind.TENSION_BATCH,
        summary=(
            f"Tension batch {batch.id} measured mean excitement {batch.mean_excitement:.2f} "
            f"with a {batch.combination_excitement_spread:.2f}-point combination spread."
        ),
        observations=tuple(
            f"{item.combination}: excitement={item.mean_excitement:.2f}, "
            f"target_pass_rate={item.target_pass_rate:.4f}"
            for item in batch.combinations
        ),
        scope=tuple(item.combination for item in batch.combinations),
        assumptions=assumptions,
        provenance=_batch_provenance(batch, artifact_path),
        dependencies=_batch_dependencies(snapshot, batch),
        hypothesis_link=HypothesisEvidenceLink(
            hypothesis_id=hypothesis.id,
            evidence_id=evidence_id,
            relation=relation,
            rationale=rationale,
            assumptions=assumptions,
        ),
    ).assess(snapshot)
    record = _mark_incomplete_batch_provenance(record, batch, role="batch")
    return ExperimentEvidenceBundle(hypothesis=hypothesis, evidence=record, claim=record.to_claim())


def ab_comparison_evidence(
    *,
    project_id: str,
    snapshot: DesignSnapshot,
    hypothesis: Hypothesis,
    baseline: TensionBatch,
    candidate: TensionBatch,
    comparison: ABComparison,
    relation: EvidenceRelation,
    rationale: str,
    assumptions: Iterable[str] = (),
    artifact_path: str | Path | None = None,
) -> ExperimentEvidenceBundle:
    _ensure_single_snapshot_batch(baseline, role="baseline")
    _ensure_single_snapshot_batch(candidate, role="candidate")
    if comparison.baseline_id != baseline.id:
        raise ValueError(
            "comparison baseline_id must match the supplied baseline batch"
        )
    if comparison.candidate_id != candidate.id:
        raise ValueError(
            "comparison candidate_id must match the supplied candidate batch"
        )
    expected_comparison = compare_batches(baseline, candidate)
    if comparison != expected_comparison:
        raise ValueError(
            "comparison metrics do not match the supplied baseline and candidate batches"
        )
    evidence_id = f"evidence:{comparison.baseline_id}:vs:{comparison.candidate_id}"
    assumptions = tuple(assumptions)
    record = ExperimentEvidence(
        id=evidence_id,
        project_id=project_id,
        snapshot_id=snapshot.id,
        kind=ExperimentEvidenceKind.AB_COMPARISON,
        summary=(
            f"Paired A/B comparison {comparison.baseline_id} vs {comparison.candidate_id} "
            f"measured a {comparison.mean_excitement_delta:+.2f} excitement delta across "
            f"{comparison.paired_runs} matched runs."
        ),
        observations=(
            f"candidate_better_rate={comparison.candidate_better_rate:.4f}",
            *(f"{item.combination}: excitement_delta={item.excitement_delta:+.2f}" for item in comparison.deltas),
        ),
        scope=tuple(item.combination for item in comparison.deltas),
        assumptions=assumptions,
        provenance=ExperimentArtifactProvenance(
            artifact_digest=canonical_digest(
                comparison, domain="tension-comparison-artifact"
            ),
            artifact_path=str(artifact_path) if artifact_path else None,
            source_result_ids=(baseline.id, candidate.id),
            source_artifact_digests={
                baseline.id: canonical_digest(
                    baseline, domain="tension-batch-artifact"
                ),
                candidate.id: canonical_digest(
                    candidate, domain="tension-batch-artifact"
                ),
            },
            snapshot_ids=tuple(dict.fromkeys((*baseline.snapshot_ids, *candidate.snapshot_ids))),
            snapshot_digests=tuple(dict.fromkeys((*baseline.snapshot_digests, *candidate.snapshot_digests))),
            config_digests=tuple(dict.fromkeys((*baseline.config_digests, *candidate.config_digests))),
            plugin_versions=tuple(dict.fromkeys((*baseline.plugin_versions, *candidate.plugin_versions))),
            runners=tuple(dict.fromkeys((*baseline.runners, *candidate.runners))),
            ruleset_ids=tuple(
                item for item in (baseline.ruleset_id, candidate.ruleset_id) if item
            ),
        ),
        dependencies=_comparison_dependencies(snapshot, baseline, candidate),
        hypothesis_link=HypothesisEvidenceLink(
            hypothesis_id=hypothesis.id,
            evidence_id=evidence_id,
            relation=relation,
            rationale=rationale,
            assumptions=assumptions,
        ),
    ).assess(snapshot)
    record = _mark_incomplete_batch_provenance(record, baseline, role="baseline")
    record = _mark_incomplete_batch_provenance(record, candidate, role="candidate")
    return ExperimentEvidenceBundle(hypothesis=hypothesis, evidence=record, claim=record.to_claim())


def _batch_provenance(
    batch: TensionBatch, artifact_path: str | Path | None
) -> ExperimentArtifactProvenance:
    return ExperimentArtifactProvenance(
        artifact_digest=canonical_digest(batch, domain="tension-batch-artifact"),
        artifact_path=str(artifact_path) if artifact_path else None,
        source_result_ids=batch.source_run_ids,
        snapshot_ids=batch.snapshot_ids,
        snapshot_digests=batch.snapshot_digests,
        config_digests=batch.config_digests,
        plugin_versions=batch.plugin_versions,
        runners=batch.runners,
        ruleset_ids=(batch.ruleset_id,) if batch.ruleset_id else (),
    )


def _batch_dependencies(
    snapshot: DesignSnapshot, batch: TensionBatch
) -> tuple[DependencyFingerprint, ...]:
    dependencies = list(snapshot_dependencies(snapshot))
    if len(batch.snapshot_digests) == 1:
        dependencies[0] = dependencies[0].model_copy(
            update={
                "revision": batch.snapshot_ids[0] if len(batch.snapshot_ids) == 1 else snapshot.id,
                "digest": batch.snapshot_digests[0],
            }
        )
    if batch.config_fingerprints:
        dependencies.extend(
            DependencyFingerprint(
                dependency_id=f"config:{item.rsplit('@', 1)[0]}",
                revision=item.rsplit("@", 1)[0],
                digest=item.rsplit("@", 1)[-1],
                role="simulation_config",
                required=False,
            )
            for item in batch.config_fingerprints
        )
    dependencies.extend(
        DependencyFingerprint(
            dependency_id=f"plugin:{plugin.rsplit('@', 1)[0]}",
            revision=plugin.rsplit("@", 1)[-1],
            role="simulation_plugin",
            required=False,
        )
        for plugin in batch.plugin_versions
    )
    return tuple(dependencies)


def _comparison_dependencies(
    snapshot: DesignSnapshot,
    baseline: TensionBatch,
    candidate: TensionBatch,
) -> tuple[DependencyFingerprint, ...]:
    """Keep both arms when they use the same dependency at different revisions."""

    items = []
    for arm, batch in (("baseline", baseline), ("candidate", candidate)):
        for item in _batch_dependencies(snapshot, batch):
            role = f"{arm}_{item.role}"
            items.append(item.model_copy(update={"role": role}))
    return tuple(
        {
            (item.role, item.dependency_id, item.revision, item.digest): item
            for item in items
        }.values()
    )


def _batch_provenance_gaps(batch: TensionBatch) -> tuple[str, ...]:
    gaps = []
    if not batch.source_run_ids:
        gaps.append("source run IDs")
    if not batch.snapshot_ids or not batch.snapshot_digests:
        gaps.append("snapshot IDs and digests")
    if len(batch.snapshot_ids) != len(batch.snapshot_digests):
        gaps.append("one digest for each snapshot ID")
    if not batch.config_fingerprints:
        gaps.append("configuration fingerprints")
    if not batch.plugin_versions:
        gaps.append("plugin versions")
    if not batch.runners:
        gaps.append("runner identities")
    return tuple(gaps)


def _ensure_single_snapshot_batch(batch: TensionBatch, *, role: str) -> None:
    if len(batch.snapshot_ids) > 1 or len(batch.snapshot_digests) > 1:
        raise ValueError(
            f"{role} batch {batch.id!r} mixes multiple design snapshots; "
            "build one tension batch per snapshot"
        )


def _mark_incomplete_batch_provenance(
    evidence: ExperimentEvidence,
    batch: TensionBatch,
    *,
    role: str,
) -> ExperimentEvidence:
    gaps = _batch_provenance_gaps(batch)
    if not gaps:
        return evidence
    reason = (
        f"{role} batch {batch.id!r} has incomplete provenance: "
        + ", ".join(gaps)
    )
    return evidence.model_copy(
        update={
            "dependencies": evidence.dependencies
            + (
                DependencyFingerprint(
                    dependency_id=f"provenance:{role}:{batch.id}",
                    revision="incomplete",
                    role=f"{role}_artifact_provenance",
                    required=True,
                ),
            ),
            "applicability": ExperimentApplicability.STALE,
            "applicability_reasons": tuple(
                dict.fromkeys((*evidence.applicability_reasons, reason))
            ),
        }
    )
