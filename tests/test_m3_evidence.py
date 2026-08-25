from datetime import UTC, datetime
import json
import sys

import pytest

from playtrace.evidence import (
    DependencyFingerprint,
    ExperimentApplicability,
    ExperimentEvidenceBundle,
    ab_comparison_evidence,
    simulation_evidence,
    tension_batch_evidence,
)
from playtrace.cli import main
from playtrace.models import (
    DecisionState,
    EvidenceRelation,
    Hypothesis,
    Intent,
    IntentKind,
)
from playtrace.plugins.crisis_wardens import CrisisWardensSimulationPlugin
from playtrace.reviewing import (
    HumanReviewDecision,
    Phase2ReviewEngine,
    ReviewDecisionState,
)
from playtrace.simulation import InProcessSimulationRunner, PlayerPolicyKind, SimulationConfig
from playtrace.storage import PlayTraceStore
from playtrace.tension import build_batch, compare_batches
from test_simulation import _snapshot


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        id="crisis-wardens:hypothesis:tension",
        statement="The current pressure curve produces a rising sawtooth climax.",
        scope=("SCN-001",),
        test_method="Run paired seeded simulations for each character combination.",
        supporting_outcomes=("Positive trend with rises, recoveries, and a late peak.",),
        contradicting_outcomes=("Flat or falling tension.",),
        evidence_requirements=("reproducible simulation provenance",),
    )


def _run():
    snapshot = _snapshot()
    run = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(),
        snapshot,
        SimulationConfig(
            id="cw:experiment:m3",
            scenario_id="SCN-001",
            character_ids=("CHR-001",),
            ruleset_id="cw:rules:provisional-core-v0.2",
            player_policy=PlayerPolicyKind.GREEDY,
            seed=7,
            deck_order=("SCD-005",),
        ),
    )
    return snapshot, run


@pytest.mark.parametrize("relation", list(EvidenceRelation))
def test_simulation_evidence_preserves_explicit_hypothesis_relation_and_provenance(
    relation,
) -> None:
    snapshot, run = _run()
    bundle = simulation_evidence(
        project_id=snapshot.project_id,
        snapshot=snapshot,
        hypothesis=_hypothesis(),
        run=run,
        relation=relation,
        rationale="This result is interpreted against the declared outcome bands.",
        assumptions=("The scripted policy approximates intentional play.",),
    )
    restored = ExperimentEvidenceBundle.model_validate_json(bundle.model_dump_json())

    assert restored.evidence.hypothesis_link.hypothesis_id == _hypothesis().id
    assert restored.evidence.hypothesis_link.relation is relation
    assert restored.claim.evidence[0].relation is relation
    assert restored.claim.evidence[0].evidence_id == restored.evidence.id
    assert "The scripted policy approximates intentional play." in restored.claim.evidence[0].assumptions
    assert restored.evidence.provenance.snapshot_digests == (run.provenance.snapshot_digest,)
    assert restored.evidence.provenance.config_digests == (run.provenance.config_digest,)
    assert restored.evidence.provenance.plugin_versions == (
        f"{run.provenance.plugin_id}@{run.provenance.plugin_version}",
    )
    assert restored.evidence.applicability is ExperimentApplicability.CURRENT


def test_applicability_is_deterministic_for_revision_digest_and_missing_dependencies() -> None:
    snapshot, run = _run()
    evidence = simulation_evidence(
        project_id=snapshot.project_id,
        snapshot=snapshot,
        hypothesis=_hypothesis(),
        run=run,
        relation=EvidenceRelation.CONTEXT,
        rationale="A calibration run.",
    ).evidence

    assert evidence.assess(snapshot).applicability is ExperimentApplicability.CURRENT

    changed_revision = snapshot.source_revisions[0].model_copy(update={"revision": "changed"})
    changed_snapshot = snapshot.model_copy(
        update={"source_revisions": (changed_revision, *snapshot.source_revisions[1:])}
    )
    stale = evidence.assess(changed_snapshot)
    assert stale.applicability is ExperimentApplicability.STALE
    assert any("source:" in reason or "snapshot:" in reason for reason in stale.applicability_reasons)

    config_id = f"config:{run.config.id}"
    stale_config = evidence.assess(
        snapshot,
        (
            DependencyFingerprint(
                dependency_id=config_id,
                revision=run.config.id,
                digest="different",
                role="simulation_config",
            ),
        ),
    )
    assert stale_config.applicability is ExperimentApplicability.STALE
    assert any(config_id in reason for reason in stale_config.applicability_reasons)

    required_missing = evidence.model_copy(
        update={
            "dependencies": evidence.dependencies
            + (
                DependencyFingerprint(
                    dependency_id="ruleset:missing", revision="1", required=True
                ),
            )
        }
    ).assess(snapshot)
    assert required_missing.applicability is ExperimentApplicability.STALE
    assert "ruleset:missing" in " ".join(required_missing.applicability_reasons)


def test_tension_batch_and_comparison_evidence_preserve_source_artifacts() -> None:
    snapshot, run = _run()
    baseline = build_batch("baseline", (run,))
    candidate = baseline.model_copy(update={"id": "candidate"})
    comparison = compare_batches(baseline, candidate)

    batch_bundle = tension_batch_evidence(
        project_id=snapshot.project_id,
        snapshot=snapshot,
        hypothesis=_hypothesis(),
        batch=baseline,
        relation=EvidenceRelation.SUPPORTS,
        rationale="The baseline meets the declared flow targets.",
    )
    comparison_bundle = ab_comparison_evidence(
        project_id=snapshot.project_id,
        snapshot=snapshot,
        hypothesis=_hypothesis(),
        baseline=baseline,
        candidate=candidate,
        comparison=comparison,
        relation=EvidenceRelation.JUDGMENT,
        rationale="The measured delta is too small to justify a canonical change.",
    )

    assert batch_bundle.evidence.provenance.source_result_ids == (run.id,)
    assert run.provenance.snapshot_digest in batch_bundle.evidence.provenance.snapshot_digests
    assert comparison_bundle.evidence.provenance.source_result_ids == (
        "baseline",
        "candidate",
    )
    assert set(comparison_bundle.evidence.provenance.source_artifact_digests) == {
        "baseline",
        "candidate",
    }
    assert comparison_bundle.claim.evidence[0].relation is EvidenceRelation.JUDGMENT


def test_ab_evidence_preserves_role_qualified_mixed_config_revisions() -> None:
    snapshot, run = _run()
    baseline = build_batch("baseline", (run,))
    candidate_run = run.model_copy(
        update={
            "provenance": run.provenance.model_copy(
                update={"config_digest": "candidate-config-digest"}
            )
        }
    )
    candidate = build_batch("candidate", (candidate_run,))
    bundle = ab_comparison_evidence(
        project_id=snapshot.project_id,
        snapshot=snapshot,
        hypothesis=_hypothesis(),
        baseline=baseline,
        candidate=candidate,
        comparison=compare_batches(baseline, candidate),
        relation=EvidenceRelation.JUDGMENT,
        rationale="Compare role-qualified inputs.",
    )

    config_dependencies = [
        item
        for item in bundle.evidence.dependencies
        if item.dependency_id == f"config:{run.config.id}"
    ]
    assert {(item.role, item.digest) for item in config_dependencies} == {
        ("baseline_simulation_config", run.provenance.config_digest),
        ("candidate_simulation_config", "candidate-config-digest"),
    }

    assessed = bundle.evidence.assess(
        snapshot,
        (
            DependencyFingerprint(
                dependency_id=f"config:{run.config.id}",
                revision=run.config.id,
                digest=run.provenance.config_digest,
                role="baseline_simulation_config",
            ),
            DependencyFingerprint(
                dependency_id=f"config:{run.config.id}",
                revision=run.config.id,
                digest="candidate-config-digest",
                role="candidate_simulation_config",
            ),
        ),
    )
    assert assessed.applicability is ExperimentApplicability.CURRENT

    stale_candidate = bundle.evidence.assess(
        snapshot,
        (
            DependencyFingerprint(
                dependency_id=f"config:{run.config.id}",
                revision=run.config.id,
                digest=run.provenance.config_digest,
                role="baseline_simulation_config",
            ),
            DependencyFingerprint(
                dependency_id=f"config:{run.config.id}",
                revision=run.config.id,
                digest="wrong-candidate-digest",
                role="candidate_simulation_config",
            ),
        ),
    )
    assert stale_candidate.applicability is ExperimentApplicability.STALE
    assert "candidate-config-digest" in " ".join(
        stale_candidate.applicability_reasons
    )


def test_ab_evidence_rejects_comparison_for_different_batches() -> None:
    snapshot, run = _run()
    baseline = build_batch("baseline", (run,))
    candidate = baseline.model_copy(update={"id": "candidate"})
    comparison = compare_batches(baseline, candidate).model_copy(
        update={"baseline_id": "not-the-baseline"}
    )

    with pytest.raises(ValueError, match="baseline_id"):
        ab_comparison_evidence(
            project_id=snapshot.project_id,
            snapshot=snapshot,
            hypothesis=_hypothesis(),
            baseline=baseline,
            candidate=candidate,
            comparison=comparison,
            relation=EvidenceRelation.CONTEXT,
            rationale="This comparison is mismatched.",
        )


def test_ab_evidence_rejects_tampered_comparison_metrics() -> None:
    snapshot, run = _run()
    baseline = build_batch("baseline", (run,))
    candidate = baseline.model_copy(update={"id": "candidate"})
    comparison = compare_batches(baseline, candidate).model_copy(
        update={"mean_excitement_delta": 99.0}
    )

    with pytest.raises(ValueError, match="comparison metrics"):
        ab_comparison_evidence(
            project_id=snapshot.project_id,
            snapshot=snapshot,
            hypothesis=_hypothesis(),
            baseline=baseline,
            candidate=candidate,
            comparison=comparison,
            relation=EvidenceRelation.CONTEXT,
            rationale="This comparison has been tampered with.",
        )


def test_tension_batch_rejects_runs_from_multiple_snapshots() -> None:
    snapshot, run = _run()
    other_snapshot_run = run.model_copy(
        update={
            "id": f"{run.id}:other-snapshot",
            "provenance": run.provenance.model_copy(
                update={
                    "snapshot_id": "crisis-wardens:snapshot:other",
                    "snapshot_digest": "other-snapshot-digest",
                }
            ),
        }
    )

    with pytest.raises(ValueError, match="cannot mix multiple design snapshots"):
        build_batch("mixed", (run, other_snapshot_run))

    # model_copy intentionally bypasses validation; the evidence assembler must
    # still refuse a forged or legacy mixed-snapshot artifact.
    mixed = build_batch("single", (run,)).model_copy(
        update={
            "snapshot_ids": (
                run.provenance.snapshot_id,
                other_snapshot_run.provenance.snapshot_id,
            ),
            "snapshot_digests": (
                run.provenance.snapshot_digest,
                other_snapshot_run.provenance.snapshot_digest,
            ),
        }
    )
    with pytest.raises(ValueError, match="mixes multiple design snapshots"):
        tension_batch_evidence(
            project_id=snapshot.project_id,
            snapshot=snapshot,
            hypothesis=_hypothesis(),
            batch=mixed,
            relation=EvidenceRelation.CONTEXT,
            rationale="Mixed snapshots are not assessable as one batch.",
        )


def test_legacy_batch_without_reconstructable_provenance_is_stale() -> None:
    snapshot, run = _run()
    partial = build_batch("legacy", (run,)).model_copy(
        update={
            "source_run_ids": (),
            "snapshot_ids": (),
            "snapshot_digests": (),
            "config_fingerprints": (),
            "plugin_versions": (),
            "runners": (),
        }
    )
    bundle = tension_batch_evidence(
        project_id=snapshot.project_id,
        snapshot=snapshot,
        hypothesis=_hypothesis(),
        batch=partial,
        relation=EvidenceRelation.CONTEXT,
        rationale="Legacy context only.",
    )

    assert bundle.evidence.applicability is ExperimentApplicability.STALE
    assert "incomplete provenance" in " ".join(bundle.evidence.applicability_reasons)
    assert bundle.evidence.assess(snapshot).applicability is ExperimentApplicability.STALE


def test_review_requires_every_experiment_hypothesis_link_to_be_included(tmp_path) -> None:
    snapshot, run = _run()
    evidence = simulation_evidence(
        project_id=snapshot.project_id,
        snapshot=snapshot,
        hypothesis=_hypothesis(),
        run=run,
        relation=EvidenceRelation.CONTEXT,
        rationale="Standalone evidence.",
    ).evidence

    with pytest.raises(ValueError, match="not included in the review"):
        Phase2ReviewEngine(tmp_path, tmp_path / "game").review(
            snapshot,
            Intent(
                id="intent:orphan-evidence",
                project_id=snapshot.project_id,
                kind=IntentKind.DIAGNOSIS,
                original_prompt="Assess an incomplete evidence set.",
            ),
            experiment_evidence=(evidence,),
        )


def test_review_and_store_keep_evidence_decision_and_authority_separate(tmp_path) -> None:
    snapshot, run = _run()
    hypothesis = _hypothesis()
    bundle = simulation_evidence(
        project_id=snapshot.project_id,
        snapshot=snapshot,
        hypothesis=hypothesis,
        run=run,
        relation=EvidenceRelation.SUPPORTS,
        rationale="The run exhibits the declared outcome.",
    )
    review = Phase2ReviewEngine(tmp_path, tmp_path / "game").review(
        snapshot,
        Intent(
            id="crisis-wardens:intent:m3",
            project_id=snapshot.project_id,
            kind=IntentKind.EXPERIMENT,
            original_prompt="Evaluate the tension hypothesis.",
            scope=("SCN-001",),
        ),
        experiment_evidence=(bundle.evidence,),
        experiment_hypotheses=(hypothesis,),
    )
    assert review.decision.state is ReviewDecisionState.PENDING
    assert review.experiment_evidence[0].applicability is ExperimentApplicability.CURRENT
    assert next(
        item for item in review.coverage.items if item.target_id == bundle.evidence.id
    ).covered_by == (bundle.claim.id,)

    accepted = review.model_copy(
        update={
            "decision": HumanReviewDecision(
                state=ReviewDecisionState.ACCEPTED,
                rationale="Retain this as current evidence.",
                decided_by="designer",
                decided_at=datetime(2026, 8, 26, tzinfo=UTC),
            )
        }
    )
    with PlayTraceStore(tmp_path / "m3.db") as store:
        store.save_snapshot(snapshot)
        store.save_hypothesis(snapshot.id, hypothesis)
        store.save_experiment_evidence(bundle.evidence)
        store.save_claim(snapshot.id, bundle.claim)
        store.save_review(accepted)
        assert store.load_hypothesis(hypothesis.id) == hypothesis
        assert store.load_experiment_evidence(bundle.evidence.id) == bundle.evidence
        assert store.load_claim(bundle.claim.id) == bundle.claim
        assert store.load_review(review.id) == accepted
        assert store.export_counts()["experiment_evidence"] == 1

    # Evidence informs a decision; it does not approve a design hypothesis or create a patch.
    assert hypothesis.status is DecisionState.PROPOSED
    assert accepted.hypotheses[-1].status is DecisionState.PROPOSED
    assert not hasattr(accepted, "change_set")


def test_stale_experiment_remains_visible_but_is_not_a_current_coverage_target(
    tmp_path,
) -> None:
    snapshot, run = _run()
    hypothesis = _hypothesis()
    evidence = simulation_evidence(
        project_id=snapshot.project_id,
        snapshot=snapshot,
        hypothesis=hypothesis,
        run=run,
        relation=EvidenceRelation.CONTEXT,
        rationale="Historical calibration context.",
    ).evidence
    changed = snapshot.model_copy(
        update={
            "source_revisions": (
                snapshot.source_revisions[0].model_copy(update={"revision": "new"}),
                *snapshot.source_revisions[1:],
            )
        }
    )
    review = Phase2ReviewEngine(tmp_path, tmp_path / "game").review(
        changed,
        Intent(
            id="crisis-wardens:intent:stale",
            project_id=changed.project_id,
            kind=IntentKind.DIAGNOSIS,
            original_prompt="Review historical tension evidence.",
            scope=("SCN-001",),
        ),
        experiment_evidence=(evidence,),
        experiment_hypotheses=(hypothesis,),
    )

    assert review.experiment_evidence[0].applicability is ExperimentApplicability.STALE
    assert evidence.id not in {item.target_id for item in review.coverage.items}
    assert next(claim for claim in review.claims if claim.id == f"{evidence.id}:claim").applicability == "stale"


def test_record_experiment_cli_persists_bundle_without_touching_canon(
    tmp_path, monkeypatch
) -> None:
    snapshot, run = _run()
    project = tmp_path / "game"
    canonical = project / "design" / "rules.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("Canonical rule text.\n", encoding="utf-8")
    before = canonical.read_bytes()
    snapshot_path = tmp_path / "snapshot.json"
    run_path = tmp_path / "run.json"
    hypothesis_path = tmp_path / "hypothesis.json"
    output_path = tmp_path / "evidence.json"
    db_path = tmp_path / "evidence.db"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    run_path.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    hypothesis_path.write_text(_hypothesis().model_dump_json(indent=2), encoding="utf-8")
    prior_review = Phase2ReviewEngine(tmp_path, project).review(
        snapshot,
        Intent(
            id="crisis-wardens:intent:prior-review",
            project_id=snapshot.project_id,
            kind=IntentKind.DIAGNOSIS,
            original_prompt="Preserve this completed review.",
        ),
    ).model_copy(
        update={
            "decision": HumanReviewDecision(
                state=ReviewDecisionState.REJECTED,
                rationale="Keep the previous decision.",
                decided_by="designer",
                decided_at=datetime(2026, 8, 26, tzinfo=UTC),
            )
        }
    )
    with PlayTraceStore(db_path) as store:
        store.save_snapshot(snapshot)
        store.save_review(prior_review)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "playtrace",
            "record-experiment",
            str(project),
            "--snapshot",
            str(snapshot_path),
            "--hypothesis",
            str(hypothesis_path),
            "--simulation",
            str(run_path),
            "--relation",
            "supports",
            "--rationale",
            "The run meets the declared outcome.",
            "--db",
            str(db_path),
            "--output",
            str(output_path),
        ],
    )

    main()

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["evidence"]["hypothesis_link"]["relation"] == "supports"
    with PlayTraceStore(db_path) as store:
        assert store.export_counts()["experiment_evidence"] == 1
        assert store.load_hypothesis(_hypothesis().id).status is DecisionState.PROPOSED
        assert store.load_review(prior_review.id) == prior_review
    assert canonical.read_bytes() == before


def test_decide_review_cli_persists_rejection_without_mutating_hypothesis_or_canon(
    tmp_path, monkeypatch
) -> None:
    snapshot, _ = _run()
    project = tmp_path / "game"
    canonical = project / "design" / "rules.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("Canonical rule text.\n", encoding="utf-8")
    before = canonical.read_bytes()
    hypothesis = _hypothesis()
    review = Phase2ReviewEngine(tmp_path, project).review(
        snapshot,
        Intent(
            id="crisis-wardens:intent:decision-cli",
            project_id=snapshot.project_id,
            kind=IntentKind.DIAGNOSIS,
            original_prompt="Decide this review.",
        ),
    )
    db_path = tmp_path / "decision.db"
    output_path = tmp_path / "decision.json"
    with PlayTraceStore(db_path) as store:
        store.save_snapshot(snapshot)
        store.save_hypothesis(snapshot.id, hypothesis)
        store.save_review(review)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "playtrace",
            "decide-review",
            "--review-id",
            review.id,
            "--db",
            str(db_path),
            "--state",
            "rejected",
            "--rationale",
            "The evidence does not justify a design change.",
            "--decided-by",
            "designer",
            "--output",
            str(output_path),
        ],
    )

    main()

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["canonical_mutation"] is False
    assert payload["review"]["decision"]["state"] == "rejected"
    with PlayTraceStore(db_path) as store:
        decided = store.load_review(review.id)
        assert decided is not None
        assert decided.decision.state is ReviewDecisionState.REJECTED
        assert store.load_hypothesis(hypothesis.id) == hypothesis
        assert store.load_snapshot(snapshot.id) == snapshot
    assert canonical.read_bytes() == before
