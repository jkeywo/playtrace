from __future__ import annotations

import pytest

from playtrace.adapters import SyntheticProjectAdapter
from playtrace.knowledge import promote_applied_proposal
from playtrace.models import (
    ChangeOperation,
    ChangeSet,
    DecisionState,
    SourceMedium,
)
from playtrace.patches import DocumentTextPatch, SemanticPatchExecutor, SemanticPatchSet
from playtrace.proposals import (
    AttemptStatus,
    ProposalBasis,
    ProposalOrchestrator,
    ProposalPostcondition,
    ProposalTransition,
    ProposalState,
    RegressionOutcome,
    create_proposal,
    transition_proposal,
)
from playtrace.storage import PlayTraceStore
from playtrace.workspace import FixtureWorkspaceGateway


class WritableTestGateway(FixtureWorkspaceGateway):
    """Unit-test double for the live-writable gateway contract."""

    supports_canonical_writes = True


def test_proposal_binds_full_basis_and_history_is_append_only(tmp_path) -> None:
    record, _gateway, snapshot = _record()
    submitted = transition_proposal(
        record, ProposalState.PENDING, actor="designer", rationale="Ready for review."
    )
    approved = transition_proposal(
        submitted, ProposalState.APPROVED, actor="owner", rationale="Evidence is sufficient."
    )

    with PlayTraceStore(tmp_path / "m5.db") as store:
        store.save_snapshot(snapshot)
        store.save_proposal(record)
        store.save_proposal(submitted)
        store.save_proposal(approved)
        loaded = store.load_proposal(record.proposal.id)
        with pytest.raises(ValueError, match="append-only"):
            store.save_proposal(record)

    assert loaded == approved
    assert [item.to_state for item in loaded.history] == [
        ProposalState.DRAFT,
        ProposalState.PENDING,
        ProposalState.APPROVED,
    ]


def test_only_approved_proposal_can_preflight_or_apply_and_dry_run_never_writes() -> None:
    record, gateway, snapshot = _record()
    orchestrator = ProposalOrchestrator(SemanticPatchExecutor(gateway), lambda: snapshot)
    refused = orchestrator.execute(record, snapshot, dry_run=True, actor="operator")
    assert refused.attempts[-1].status is AttemptStatus.PREFLIGHT_FAILED
    assert "only an approved" in refused.attempts[-1].message

    approved = _approve(record)
    checked = orchestrator.execute(approved, snapshot, dry_run=True, actor="operator")

    assert gateway.writes == []
    assert checked.proposal.state is ProposalState.APPROVED
    assert checked.attempts[-1].status is AttemptStatus.PREFLIGHT_PASSED
    assert checked.attempts[-1].applied_operations == 0


def test_preflight_failure_is_persistable_and_performs_no_write() -> None:
    record, gateway, snapshot = _record()
    stale_patch = record.proposal.patch_set.document_patches[0].model_copy(
        update={"required_revision_id": "stale"}
    )
    patch_set = record.proposal.patch_set.model_copy(
        update={"document_patches": (stale_patch,)}
    )
    # Recreate the proposal so the changed patch is genuinely part of its approval.
    proposal = create_proposal(
        id="proposal:stale",
        project_id=record.proposal.project_id,
        title=record.proposal.title,
        question=record.proposal.question,
        rationale=record.proposal.rationale,
        alternatives=record.proposal.alternatives,
        criteria=record.proposal.criteria,
        change_set=record.proposal.change_set,
        patch_set=patch_set,
        basis=record.proposal.basis,
        created_by="designer",
        evidence_ids=record.proposal.evidence_ids,
    )
    checked = ProposalOrchestrator(
        SemanticPatchExecutor(gateway), lambda: snapshot
    ).execute(_approve(proposal), snapshot, dry_run=False, actor="operator")

    assert gateway.writes == []
    assert checked.proposal.state is ProposalState.APPROVED
    assert checked.attempts[-1].status is AttemptStatus.PREFLIGHT_FAILED
    assert "before any canonical write" in checked.attempts[-1].message


def test_apply_rechecks_basis_writes_then_validates() -> None:
    record, gateway, snapshot = _record()
    approved = _approve(record)
    applied = ProposalOrchestrator(
        SemanticPatchExecutor(gateway), lambda: snapshot
    ).execute(approved, snapshot, dry_run=False, actor="operator")

    assert len(gateway.writes) == 1
    assert applied.proposal.state is ProposalState.APPLIED
    assert applied.attempts[-1].status is AttemptStatus.APPLIED
    assert applied.attempts[-1].applied_operations == 1

    changed = snapshot.model_copy(update={"id": "another-snapshot"})
    stale = ProposalOrchestrator(
        SemanticPatchExecutor(gateway), lambda: snapshot
    ).execute(approved, changed, dry_run=True, actor="operator")
    assert stale.attempts[-1].status is AttemptStatus.PREFLIGHT_FAILED
    assert "snapshot ID" in stale.attempts[-1].message

    tampered_proposal = approved.proposal.model_copy(update={"title": "Unapproved title"})
    tampered = approved.model_copy(update={"proposal": tampered_proposal})
    refused = ProposalOrchestrator(
        SemanticPatchExecutor(gateway), lambda: snapshot
    ).execute(tampered, snapshot, dry_run=True, actor="operator")
    assert refused.attempts[-1].status is AttemptStatus.PREFLIGHT_FAILED
    assert "approval digest" in refused.attempts[-1].message


def test_fixture_gateway_is_dry_run_only() -> None:
    record, writable, snapshot = _record()
    fixture = FixtureWorkspaceGateway(writable.payload)
    refused = ProposalOrchestrator(
        SemanticPatchExecutor(fixture), lambda: snapshot
    ).execute(_approve(record), snapshot, dry_run=False, actor="operator")

    assert fixture.writes == []
    assert refused.proposal.state is ProposalState.APPROVED
    assert refused.attempts[-1].status is AttemptStatus.PREFLIGHT_FAILED
    assert "live writable" in refused.attempts[-1].message


def test_snapshot_preflight_failure_round_trips_as_append_only_attempt(tmp_path) -> None:
    record, gateway, snapshot = _record()
    approved = _approve(record)
    changed = snapshot.model_copy(update={"id": "another-snapshot"})
    refused = ProposalOrchestrator(
        SemanticPatchExecutor(gateway), lambda: snapshot
    ).execute(approved, changed, dry_run=True, actor="operator")

    with PlayTraceStore(tmp_path / "preflight.db") as store:
        store.save_proposal(record)
        store.save_proposal(approved)
        store.save_proposal(refused)
        loaded = store.load_proposal(record.proposal.id)

    assert loaded == refused
    assert loaded.attempts[-1].status is AttemptStatus.PREFLIGHT_FAILED


def test_approval_digest_preflight_failure_persists_without_replacing_proposal(tmp_path) -> None:
    record, gateway, snapshot = _record()
    approved = _approve(record)
    tampered_proposal = approved.proposal.model_copy(update={"title": "Unapproved title"})
    tampered = approved.model_copy(update={"proposal": tampered_proposal})
    refused = ProposalOrchestrator(
        SemanticPatchExecutor(gateway), lambda: snapshot
    ).execute(tampered, snapshot, dry_run=True, actor="operator")

    with PlayTraceStore(tmp_path / "approval-preflight.db") as store:
        store.save_proposal(record)
        store.save_proposal(approved)
        store.save_proposal(refused)
        loaded = store.load_proposal(record.proposal.id)

    assert loaded.proposal == approved.proposal
    assert loaded.attempts[-1].status is AttemptStatus.PREFLIGHT_FAILED
    assert "approval digest" in loaded.attempts[-1].message


def test_post_write_validation_failure_is_not_reported_as_write_failure() -> None:
    record, gateway, snapshot = _record(
        postconditions=(
            ProposalPostcondition(
                id="post:missing",
                description="Expected generated entity exists.",
                kind="entity_exists",
                target_id="missing:entity",
            ),
        )
    )
    failed = ProposalOrchestrator(
        SemanticPatchExecutor(gateway), lambda: snapshot
    ).execute(_approve(record), snapshot, dry_run=False, actor="operator")

    assert len(gateway.writes) == 1
    assert failed.proposal.state is ProposalState.VALIDATION_FAILED
    assert failed.attempts[-1].status is AttemptStatus.VALIDATION_FAILED
    assert "not rolled back" in failed.attempts[-1].message


def test_write_failure_is_recorded_honestly_and_validation_does_not_run() -> None:
    record, gateway, snapshot = _record()
    gateway.batch_update_document = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("provider unavailable")
    )
    rebuilt = []
    failed = ProposalOrchestrator(
        SemanticPatchExecutor(gateway), lambda: rebuilt.append(True) or snapshot
    ).execute(_approve(record), snapshot, dry_run=False, actor="operator")

    assert failed.proposal.state is ProposalState.WRITE_FAILED
    assert failed.attempts[-1].status is AttemptStatus.WRITE_FAILED
    assert "partially written" in failed.attempts[-1].message
    assert rebuilt == []


def test_every_referenced_regression_must_run_and_pass() -> None:
    record, gateway, snapshot = _record(
        regression_case_ids=("case:known",),
        regression_records={"case:known": {"id": "case:known", "expected": "won"}},
    )
    failed = ProposalOrchestrator(
        SemanticPatchExecutor(gateway),
        lambda: snapshot,
        run_regressions=lambda _proposal, _snapshot: (
            RegressionOutcome(
                suite_id="suite:wrong",
                passed=True,
                case_ids=("case:other",),
                digest="digest",
            ),
        ),
    ).execute(_approve(record), snapshot, dry_run=False, actor="operator")

    assert failed.proposal.state is ProposalState.VALIDATION_FAILED
    assert "case:known" in failed.attempts[-1].failed_postconditions[-1]


def test_only_applied_proposal_can_be_promoted_to_game_owned_precedent(tmp_path) -> None:
    record, gateway, snapshot = _record()
    with pytest.raises(ValueError, match="only an applied"):
        promote_applied_proposal(
            _approve(record),
            game_root=tmp_path,
            decision="Use it.",
            rationale="No application occurred.",
        )

    approved = _approve(record)
    fake_applied = approved.model_copy(
        update={
            "proposal": approved.proposal.model_copy(
                update={"state": ProposalState.APPLIED}
            ),
            "history": approved.history
            + (
                ProposalTransition(
                    sequence=len(approved.history) + 1,
                    from_state=ProposalState.APPROVED,
                    to_state=ProposalState.APPLIED,
                    actor="test",
                    rationale="No write actually occurred.",
                ),
            ),
        }
    )
    with pytest.raises(ValueError, match="successful canonical application"):
        promote_applied_proposal(
            fake_applied,
            game_root=tmp_path,
            decision="Use it.",
            rationale="This must not be promotable.",
        )
    with PlayTraceStore(tmp_path / "fake-applied.db") as store:
        store.save_proposal(record)
        store.save_proposal(approved)
        with pytest.raises(ValueError, match="matching application attempt"):
            store.save_proposal(fake_applied)

    applied = ProposalOrchestrator(
        SemanticPatchExecutor(gateway), lambda: snapshot
    ).execute(_approve(record), snapshot, dry_run=False, actor="operator")
    precedent, path = promote_applied_proposal(
        applied,
        game_root=tmp_path,
        decision="Use the revised wording.",
        rationale="Its postconditions and regressions passed.",
    )

    assert path.parent == tmp_path / "design" / "cases"
    assert precedent.state is DecisionState.ACCEPTED
    assert precedent.source_proposal_id == record.proposal.id


def _approve(record):
    return transition_proposal(
        transition_proposal(
            record, ProposalState.PENDING, actor="designer", rationale="Submit."
        ),
        ProposalState.APPROVED,
        actor="owner",
        rationale="Approve.",
    )


def _record(*, postconditions=(), regression_case_ids=(), regression_records=None):
    snapshot = SyntheticProjectAdapter(SourceMedium.SYNTHETIC).snapshot()
    change_set = ChangeSet(
        id="patch:m5",
        intent_id="intent:m5",
        base_snapshot_id=snapshot.id,
        operations=(
            ChangeOperation(
                target_id="source:rules",
                operation="replace_text",
                before="Old text",
                after="New text",
            ),
        ),
        rationale_claim_ids=("claim:m5",),
        validation=("postconditions",),
    )
    patch_set = SemanticPatchSet(
        id="patch:m5",
        base_snapshot_id=snapshot.id,
        document_patches=(
            DocumentTextPatch(
                source_id="source:rules",
                file_id="doc-1",
                required_revision_id="r1",
                tab_id="t.0",
                start_index=1,
                end_index=9,
                expected_text="Old text",
                replacement_text="New text",
            ),
        ),
    )
    basis = ProposalBasis(
        snapshot=snapshot,
        supporting_records={"claim:m5": {"statement": "Change is supported."}},
        regression_records=regression_records or {},
    )
    record = create_proposal(
        id="proposal:m5",
        project_id=snapshot.project_id,
        title="Revise the pressure rule",
        question="Should pressure wording be revised?",
        rationale="The evidence supports clearer wording.",
        alternatives=("Retain old wording", "Use revised wording"),
        criteria=("Preserve behavior", "Improve clarity"),
        change_set=change_set,
        patch_set=patch_set,
        basis=basis,
        created_by="designer",
        evidence_ids=("claim:m5",),
        regression_case_ids=regression_case_ids,
        postconditions=postconditions,
    )
    gateway = WritableTestGateway(
        {
            "files": {"doc-1": {"id": "doc-1", "version": "1"}},
            "documents": {
                "doc-1": {
                    "documentId": "doc-1",
                    "revisionId": "r1",
                    "tabs": [
                        {
                            "tabProperties": {"tabId": "t.0", "title": "Rules"},
                            "body": {
                                "content": [
                                    {
                                        "startIndex": 1,
                                        "endIndex": 10,
                                        "paragraph": {
                                            "elements": [
                                                {
                                                    "startIndex": 1,
                                                    "endIndex": 10,
                                                    "textRun": {"content": "Old text\n"},
                                                }
                                            ]
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                }
            },
            "spreadsheets": {},
        }
    )
    return record, gateway, snapshot
