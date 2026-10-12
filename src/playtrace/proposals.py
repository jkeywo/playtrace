"""Approval-gated design proposals and honest canonical application records."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from playtrace.canonical import (
    CANONICAL_IDENTITY_SCHEMA,
    CANONICAL_IDENTITY_VERSION,
    canonical_digest,
)
from playtrace.models import ChangeSet, DecisionState, DesignSnapshot, StrictModel
from playtrace.patches import (
    SagaExecutionError,
    SagaStatus,
    SemanticPatchExecutor,
    SemanticPatchSet,
    WriteSaga,
)
from playtrace.simulation import snapshot_content_digest


class ProposalState(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISE = "revise"
    APPLIED = "applied"
    WRITE_FAILED = "write_failed"
    VALIDATION_FAILED = "validation_failed"


class AttemptStatus(StrEnum):
    PREFLIGHT_FAILED = "preflight_failed"
    PREFLIGHT_PASSED = "preflight_passed"
    APPLIED = "applied"
    WRITE_FAILED = "write_failed"
    VALIDATION_FAILED = "validation_failed"


class ProposalBasis(StrictModel):
    """The complete, immutable material on which a proposal was judged."""

    snapshot: DesignSnapshot
    supporting_records: dict[str, JsonValue] = Field(default_factory=dict)
    policy_records: dict[str, JsonValue] = Field(default_factory=dict)
    regression_records: dict[str, JsonValue] = Field(default_factory=dict)

    def digest(self) -> str:
        return canonical_digest(self, domain="proposal-basis")


class ProposalPostcondition(StrictModel):
    id: str
    description: str
    kind: str
    target_id: str | None = None
    predicate: str | None = None
    expected: JsonValue = None
    required: bool = True


class DesignProposal(StrictModel):
    id: str
    project_id: str
    title: str
    question: str
    rationale: str
    alternatives: tuple[str, ...]
    criteria: tuple[str, ...]
    change_set: ChangeSet
    patch_set: SemanticPatchSet
    basis: ProposalBasis
    basis_digest: str
    digest_schema: str = "legacy-json-sha256-v0"
    canonical_seal: str | None = None
    evidence_ids: tuple[str, ...] = ()
    policy_ids: tuple[str, ...] = ()
    regression_case_ids: tuple[str, ...] = ()
    postconditions: tuple[ProposalPostcondition, ...] = ()
    state: ProposalState = ProposalState.DRAFT
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def bind_every_input(self) -> "DesignProposal":
        if self.digest_schema == canonical_digest_schema():
            if self.basis_digest != self.approval_digest():
                raise ValueError(
                    "basis_digest does not match the complete proposal and review basis"
                )
            if self.canonical_seal != self.expected_canonical_seal():
                raise ValueError("proposal canonical integrity seal does not match its content")
        elif self.digest_schema == "legacy-json-sha256-v0":
            if (
                self.canonical_seal is not None
                and self.canonical_seal != self.expected_canonical_seal()
            ):
                raise ValueError("proposal canonical integrity seal does not match its content")
        else:
            raise ValueError(f"unsupported proposal digest schema {self.digest_schema!r}")
        snapshot_id = self.basis.snapshot.id
        if self.project_id != self.basis.snapshot.project_id:
            raise ValueError("proposal project must match the basis snapshot")
        if self.change_set.base_snapshot_id != snapshot_id:
            raise ValueError("change set must target the basis snapshot")
        if self.patch_set.base_snapshot_id != snapshot_id:
            raise ValueError("semantic patch set must target the basis snapshot")
        if self.change_set.state is not DecisionState.PROPOSED:
            raise ValueError("the proposal lifecycle owns approval; embedded changes remain proposed")
        if len(self.alternatives) < 2:
            raise ValueError("a design proposal must preserve at least two alternatives")
        for label, values in (
            ("evidence", self.evidence_ids),
            ("policy", self.policy_ids),
            ("regression case", self.regression_case_ids),
            ("postcondition", tuple(item.id for item in self.postconditions)),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} IDs must be unique")
        missing_evidence = set(self.evidence_ids).difference(self.basis.supporting_records)
        missing_rationale = set(self.change_set.rationale_claim_ids).difference(
            self.evidence_ids
        )
        missing_policy = set(self.policy_ids).difference(self.basis.policy_records)
        missing_regressions = set(self.regression_case_ids).difference(
            self.basis.regression_records
        )
        if missing_evidence:
            raise ValueError(f"basis is missing evidence records: {sorted(missing_evidence)}")
        if missing_rationale:
            raise ValueError(
                f"proposal evidence omits ChangeSet rationale claims: {sorted(missing_rationale)}"
            )
        if missing_policy:
            raise ValueError(f"basis is missing policy records: {sorted(missing_policy)}")
        if missing_regressions:
            raise ValueError(
                f"basis is missing regression records: {sorted(missing_regressions)}"
            )
        return self

    def approval_digest(self) -> str:
        """Bind everything a human approves, excluding lifecycle metadata."""

        return canonical_digest(
            self.approval_payload(),
            domain="proposal-approval",
        )

    def approval_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="python",
            exclude={"basis_digest", "canonical_seal", "state", "created_at"},
        )

    def expected_canonical_seal(self) -> str:
        return canonical_digest(
            {
                "historical_basis_digest": self.basis_digest,
                "approval": self.approval_payload(),
            },
            domain="proposal-approval-integrity",
        )


def canonical_digest_schema() -> str:
    return f"{CANONICAL_IDENTITY_SCHEMA}/{CANONICAL_IDENTITY_VERSION}"


def approval_integrity_matches(proposal: DesignProposal) -> bool:
    """Central approval-integrity check for application, storage and promotion."""

    if proposal.canonical_seal != proposal.expected_canonical_seal():
        return False
    if proposal.digest_schema == canonical_digest_schema():
        return proposal.basis_digest == proposal.approval_digest()
    return proposal.digest_schema == "legacy-json-sha256-v0"


def adopt_legacy_proposal(proposal: DesignProposal) -> DesignProposal:
    """Seal one unsealed historical proposal without rewriting its approved digest."""

    if proposal.digest_schema != "legacy-json-sha256-v0":
        return proposal
    if proposal.canonical_seal is not None:
        return proposal
    return DesignProposal.model_validate(
        proposal.model_dump(mode="python")
        | {"canonical_seal": proposal.expected_canonical_seal()}
    )


class ProposalTransition(StrictModel):
    sequence: int
    from_state: ProposalState | None
    to_state: ProposalState
    actor: str
    rationale: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def require_attribution(self) -> "ProposalTransition":
        if not self.actor.strip() or not self.rationale.strip():
            raise ValueError("proposal transitions require actor and rationale")
        return self


class RegressionOutcome(StrictModel):
    suite_id: str
    passed: bool
    case_ids: tuple[str, ...]
    failed_case_ids: tuple[str, ...] = ()
    digest: str

    @model_validator(mode="after")
    def failures_are_covered(self) -> "RegressionOutcome":
        unknown = set(self.failed_case_ids).difference(self.case_ids)
        if unknown:
            raise ValueError(f"failed regression cases are absent from the suite: {sorted(unknown)}")
        if self.passed == bool(self.failed_case_ids):
            raise ValueError("regression passed flag must agree with failed case IDs")
        return self


class ApplicationAttempt(StrictModel):
    id: str
    proposal_id: str
    basis_digest: str
    actor: str
    dry_run: bool
    status: AttemptStatus
    checked_operations: int = 0
    applied_operations: int = 0
    rebuilt_snapshot_id: str | None = None
    rebuilt_snapshot_digest: str | None = None
    failed_postconditions: tuple[str, ...] = ()
    regressions: tuple[RegressionOutcome, ...] = ()
    message: str | None = None
    write_saga_id: str | None = None
    recovery_required: bool = False
    partial_failures: tuple[str, ...] = ()
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def require_actor_and_consistent_counts(self) -> "ApplicationAttempt":
        if not self.actor.strip():
            raise ValueError("application attempts require an actor")
        if self.checked_operations < 0 or self.applied_operations < 0:
            raise ValueError("application operation counts cannot be negative")
        if self.applied_operations > self.checked_operations:
            raise ValueError("applied operations cannot exceed checked operations")
        if self.dry_run and self.applied_operations:
            raise ValueError("a dry-run cannot report applied operations")
        return self


class ProposalRecord(StrictModel):
    proposal: DesignProposal
    history: tuple[ProposalTransition, ...]
    attempts: tuple[ApplicationAttempt, ...] = ()

    @model_validator(mode="after")
    def history_matches_current_state(self) -> "ProposalRecord":
        if not self.history:
            raise ValueError("proposal history cannot be empty")
        for index, transition in enumerate(self.history, start=1):
            if transition.sequence != index:
                raise ValueError("proposal history sequence must be contiguous")
            previous = None if index == 1 else self.history[index - 2].to_state
            if transition.from_state is not previous:
                raise ValueError("proposal history has a broken state chain")
            if not _history_transition_allowed(previous, transition.to_state):
                raise ValueError("proposal history contains an illegal state transition")
        if self.history[-1].to_state is not self.proposal.state:
            raise ValueError("proposal state must match its immutable history")
        if any(item.proposal_id != self.proposal.id for item in self.attempts):
            raise ValueError("application attempts must target this proposal")
        if any(item.basis_digest != self.proposal.basis_digest for item in self.attempts):
            raise ValueError("application attempts must retain the approved basis digest")
        attempt_ids = [item.id for item in self.attempts]
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("application attempt IDs must be unique")
        terminal_attempt = {
            ProposalState.APPLIED: AttemptStatus.APPLIED,
            ProposalState.WRITE_FAILED: AttemptStatus.WRITE_FAILED,
            ProposalState.VALIDATION_FAILED: AttemptStatus.VALIDATION_FAILED,
        }.get(self.proposal.state)
        if terminal_attempt is not None and (
            not self.attempts or self.attempts[-1].status is not terminal_attempt
        ):
            raise ValueError("terminal proposal state requires its matching application attempt")
        return self


def create_proposal(
    *,
    id: str,
    project_id: str,
    title: str,
    question: str,
    rationale: str,
    alternatives: Iterable[str],
    criteria: Iterable[str],
    change_set: ChangeSet,
    patch_set: SemanticPatchSet,
    basis: ProposalBasis,
    created_by: str,
    evidence_ids: Iterable[str] = (),
    policy_ids: Iterable[str] = (),
    regression_case_ids: Iterable[str] = (),
    postconditions: Iterable[ProposalPostcondition] = (),
) -> ProposalRecord:
    values = dict(
        id=id,
        project_id=project_id,
        title=title,
        question=question,
        rationale=rationale,
        alternatives=tuple(alternatives),
        criteria=tuple(criteria),
        change_set=change_set,
        patch_set=patch_set,
        basis=basis,
        basis_digest="",
        digest_schema=canonical_digest_schema(),
        canonical_seal=None,
        evidence_ids=tuple(evidence_ids),
        policy_ids=tuple(policy_ids),
        regression_case_ids=tuple(regression_case_ids),
        postconditions=tuple(postconditions),
        created_by=created_by,
    )
    approval_payload = {
        key: value
        for key, value in values.items()
        if key not in {"basis_digest", "canonical_seal", "state", "created_at"}
    }
    approval_digest = canonical_digest(
        TypeAdapter(Any).dump_python(approval_payload, mode="python"),
        domain="proposal-approval",
    )
    canonical_seal = canonical_digest(
        {
            "historical_basis_digest": approval_digest,
            "approval": TypeAdapter(Any).dump_python(approval_payload, mode="python"),
        },
        domain="proposal-approval-integrity",
    )
    proposal = DesignProposal(
        **(
            values
            | {"basis_digest": approval_digest, "canonical_seal": canonical_seal}
        )
    )
    return ProposalRecord(
        proposal=proposal,
        history=(
            ProposalTransition(
                sequence=1,
                from_state=None,
                to_state=ProposalState.DRAFT,
                actor=created_by,
                rationale="Proposal created; canonical sources remain unchanged.",
            ),
        ),
    )


_ALLOWED_TRANSITIONS = {
    ProposalState.DRAFT: {ProposalState.PENDING},
    ProposalState.PENDING: {
        ProposalState.APPROVED,
        ProposalState.REJECTED,
        ProposalState.REVISE,
    },
    ProposalState.REVISE: {ProposalState.PENDING},
    ProposalState.APPROVED: set(),
    ProposalState.REJECTED: set(),
    ProposalState.APPLIED: set(),
    ProposalState.WRITE_FAILED: set(),
    ProposalState.VALIDATION_FAILED: set(),
}


def transition_proposal(
    record: ProposalRecord,
    to_state: ProposalState,
    *,
    actor: str,
    rationale: str,
) -> ProposalRecord:
    current = record.proposal.state
    if to_state not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"proposal cannot transition from {current.value} to {to_state.value}")
    return _append_state(record, to_state, actor=actor, rationale=rationale)


class ProposalApplicationError(RuntimeError):
    pass


PostconditionValidator = Callable[[DesignProposal, DesignSnapshot], Iterable[str]]
RegressionValidator = Callable[[DesignProposal, DesignSnapshot], Iterable[RegressionOutcome]]


class ProposalOrchestrator:
    """The only M5 component authorised to turn an approval into native writes."""

    def __init__(
        self,
        patch_executor: SemanticPatchExecutor,
        rebuild_snapshot: Callable[[], DesignSnapshot],
        *,
        validate_postconditions: PostconditionValidator | None = None,
        run_regressions: RegressionValidator | None = None,
    ) -> None:
        self.patch_executor = patch_executor
        self.rebuild_snapshot = rebuild_snapshot
        self.validate_postconditions = validate_postconditions or _validate_postconditions
        self.run_regressions = run_regressions or (lambda _proposal, _snapshot: ())

    def execute(
        self,
        record: ProposalRecord,
        current_snapshot: DesignSnapshot,
        *,
        dry_run: bool,
        actor: str,
    ) -> ProposalRecord:
        proposal = record.proposal
        if proposal.state is not ProposalState.APPROVED:
            return _append_preflight_failure(
                record,
                actor=actor,
                dry_run=dry_run,
                message="Preflight failed: only an approved proposal may be dry-run or applied.",
            )
        if not approval_integrity_matches(proposal):
            return _append_preflight_failure(
                record,
                actor=actor,
                dry_run=dry_run,
                message="Preflight failed: proposal content no longer matches its approval digest.",
            )
        attempt_id = f"{proposal.id}:attempt:{len(record.attempts) + 1}"
        if not dry_run and self.patch_executor.saga_journal is not None:
            existing = self.patch_executor.saga_journal.load(f"{attempt_id}:saga")
            if existing is not None:
                return self._reconcile_durable_saga(
                    record, existing, attempt_id=attempt_id, actor=actor
                )
        if current_snapshot.id != proposal.basis.snapshot.id:
            return _append_preflight_failure(
                record,
                actor=actor,
                dry_run=dry_run,
                message="Preflight failed: current snapshot ID differs from the approved basis.",
            )
        if snapshot_content_digest(current_snapshot) != snapshot_content_digest(proposal.basis.snapshot):
            return _append_preflight_failure(
                record,
                actor=actor,
                dry_run=dry_run,
                message="Preflight failed: current snapshot content differs from the approved basis.",
            )
        if not dry_run and not getattr(
            self.patch_executor.gateway, "supports_canonical_writes", False
        ):
            return _append_preflight_failure(
                record,
                actor=actor,
                dry_run=False,
                message=(
                    "Preflight failed: canonical application requires a live writable "
                    "workspace gateway; fixture gateways are dry-run only."
                ),
            )

        try:
            preflight = self.patch_executor.execute(proposal.patch_set, dry_run=True)
        except Exception as error:
            attempt = ApplicationAttempt(
                id=attempt_id,
                proposal_id=proposal.id,
                basis_digest=proposal.basis_digest,
                actor=actor,
                dry_run=dry_run,
                status=AttemptStatus.PREFLIGHT_FAILED,
                message=f"Preflight failed before any canonical write: {error}",
            )
            return record.model_copy(update={"attempts": record.attempts + (attempt,)})
        if dry_run:
            return record.model_copy(
                update={
                    "attempts": record.attempts
                    + (
                        ApplicationAttempt(
                            id=attempt_id,
                            proposal_id=proposal.id,
                            basis_digest=proposal.basis_digest,
                            actor=actor,
                            dry_run=True,
                            status=AttemptStatus.PREFLIGHT_PASSED,
                            checked_operations=preflight.checked_operations,
                            message="Preflight passed; no canonical write was attempted.",
                        ),
                    )
                }
            )

        try:
            write_result = self.patch_executor.execute_transactional(
                proposal.patch_set,
                proposal_id=proposal.id,
                attempt_id=attempt_id,
            )
        except SagaExecutionError as error:
            saga = error.saga
            attempt = ApplicationAttempt(
                id=attempt_id,
                proposal_id=proposal.id,
                basis_digest=proposal.basis_digest,
                actor=actor,
                dry_run=False,
                status=AttemptStatus.WRITE_FAILED,
                checked_operations=preflight.checked_operations,
                applied_operations=self.patch_executor.applied_semantic_operation_count(
                    saga.operations
                ),
                write_saga_id=saga.id,
                recovery_required=saga.status.value == "recovery_failed",
                partial_failures=saga.failures,
                message=(
                    "Canonical write saga failed. Reverse compensation was verified."
                    if saga.status.value == "recovered"
                    else "Canonical write saga failed and exact recovery remains required."
                ),
            )
            return _append_attempt_state(
                record, attempt, ProposalState.WRITE_FAILED, actor, "Canonical write failed."
            )
        except Exception as error:
            attempt = ApplicationAttempt(
                id=attempt_id,
                proposal_id=proposal.id,
                basis_digest=proposal.basis_digest,
                actor=actor,
                dry_run=False,
                status=AttemptStatus.WRITE_FAILED,
                checked_operations=preflight.checked_operations,
                message=(
                    f"Canonical write failed: {error}. A sequential patch executor may have "
                    "partially written; inspect the workspace before recovery."
                ),
            )
            return _append_attempt_state(
                record, attempt, ProposalState.WRITE_FAILED, actor, "Canonical write failed."
            )

        try:
            if write_result.write_saga_id:
                self.patch_executor.begin_validation(write_result.write_saga_id)
            rebuilt = self.rebuild_snapshot()
            failures = tuple(self.validate_postconditions(proposal, rebuilt))
            regressions = tuple(self.run_regressions(proposal, rebuilt))
            referenced = set(proposal.regression_case_ids)
            covered = {
                case_id
                for result in regressions
                for case_id in result.case_ids
            }
            # A suite digest proves a suite ran; the injected runner is responsible
            # for rejecting missing referenced cases and reporting them as failures.
            regression_failures = [
                f"regression suite {result.suite_id} failed: {', '.join(result.failed_case_ids)}"
                for result in regressions
                if not result.passed
            ]
            if referenced and not regressions:
                regression_failures.append("referenced regression cases were not run")
            elif missing := referenced.difference(covered):
                regression_failures.append(
                    f"referenced regression cases were not run: {', '.join(sorted(missing))}"
                )
            failures = (*failures, *regression_failures)
        except Exception as error:
            rebuilt = None
            regressions = ()
            failures = (f"post-write validation crashed: {error}",)

        recovery_required = False
        partial_failures: tuple[str, ...] = ()
        if failures and write_result.write_saga_id:
            try:
                rollback = self.patch_executor.rollback(write_result.write_saga_id)
                recovery_required = rollback.status.value == "recovery_failed"
                partial_failures = rollback.failures
            except Exception as error:
                recovery_required = True
                partial_failures = (f"validation rollback crashed: {error}",)
        elif failures:
            recovery_required = True
            partial_failures = ("write executor did not provide a durable saga",)
        elif write_result.write_saga_id:
            try:
                self.patch_executor.commit(write_result.write_saga_id)
            except Exception as error:
                failures = (f"write saga commit failed: {error}",)
                try:
                    rollback = self.patch_executor.rollback(write_result.write_saga_id)
                    recovery_required = rollback.status.value == "recovery_failed"
                    partial_failures = rollback.failures
                except Exception as rollback_error:
                    recovery_required = True
                    partial_failures = (f"commit rollback crashed: {rollback_error}",)
        status = AttemptStatus.VALIDATION_FAILED if failures else AttemptStatus.APPLIED
        state = ProposalState.VALIDATION_FAILED if failures else ProposalState.APPLIED
        attempt = ApplicationAttempt(
            id=attempt_id,
            proposal_id=proposal.id,
            basis_digest=proposal.basis_digest,
            actor=actor,
            dry_run=False,
            status=status,
            checked_operations=write_result.checked_operations,
            applied_operations=write_result.applied_operations,
            rebuilt_snapshot_id=rebuilt.id if rebuilt else None,
            rebuilt_snapshot_digest=snapshot_content_digest(rebuilt) if rebuilt else None,
            failed_postconditions=tuple(failures),
            regressions=regressions,
            write_saga_id=write_result.write_saga_id,
            recovery_required=recovery_required,
            partial_failures=partial_failures,
            message=(
                (
                    "Canonical writes completed, but validation failed; canon was not rolled back."
                    if not write_result.write_saga_id
                    else (
                        "Canonical writes completed, validation failed, and reverse compensation was verified."
                        if not recovery_required
                        else "Canonical writes completed, validation failed, and exact recovery remains required."
                    )
                )
                if failures
                else "Canonical writes and all post-apply validation completed."
            ),
        )
        rationale = "Post-write validation failed." if failures else "Applied and validated."
        return _append_attempt_state(record, attempt, state, actor, rationale)

    def _reconcile_durable_saga(
        self,
        record: ProposalRecord,
        saga: WriteSaga,
        *,
        attempt_id: str,
        actor: str,
    ) -> ProposalRecord:
        """Project a durable saga whose proposal attempt was lost in a crash."""

        proposal = record.proposal
        if (
            saga.id != f"{attempt_id}:saga"
            or saga.attempt_id != attempt_id
            or saga.proposal_id != proposal.id
            or saga.patch_set_id != proposal.patch_set.id
        ):
            return _append_preflight_failure(
                record,
                actor=actor,
                dry_run=False,
                message="Preflight failed: durable write saga identity does not match the proposal.",
            )

        checked = len(proposal.patch_set.document_patches) + len(
            proposal.patch_set.sheet_patches
        )
        applied = self.patch_executor.applied_semantic_operation_count(saga.operations)
        if saga.status is SagaStatus.COMMITTED:
            attempt = ApplicationAttempt(
                id=attempt_id,
                proposal_id=proposal.id,
                basis_digest=proposal.basis_digest,
                actor=actor,
                dry_run=False,
                status=AttemptStatus.APPLIED,
                checked_operations=checked,
                applied_operations=applied,
                write_saga_id=saga.id,
                message=(
                    "Reconciled a durably committed canonical write whose proposal "
                    "attempt record was interrupted."
                ),
            )
            return _append_attempt_state(
                record, attempt, ProposalState.APPLIED, actor, "Reconciled durable commit."
            )

        if saga.status is not SagaStatus.RECOVERED:
            try:
                saga = self.patch_executor.recover(saga.id)
            except Exception as error:
                saga = saga.model_copy(
                    update={
                        "status": SagaStatus.RECOVERY_FAILED,
                        "failures": (*saga.failures, f"reconciliation recovery crashed: {error}"),
                    }
                )
        recovery_required = saga.status is not SagaStatus.RECOVERED
        attempt = ApplicationAttempt(
            id=attempt_id,
            proposal_id=proposal.id,
            basis_digest=proposal.basis_digest,
            actor=actor,
            dry_run=False,
            status=AttemptStatus.WRITE_FAILED,
            checked_operations=checked,
            applied_operations=applied,
            write_saga_id=saga.id,
            recovery_required=recovery_required,
            partial_failures=saga.failures,
            message=(
                "Reconciled an interrupted canonical write and verified reverse compensation."
                if not recovery_required
                else "Reconciled an interrupted canonical write; exact recovery remains required."
            ),
        )
        return _append_attempt_state(
            record, attempt, ProposalState.WRITE_FAILED, actor, "Reconciled interrupted write."
        )


def _append_preflight_failure(
    record: ProposalRecord,
    *,
    actor: str,
    dry_run: bool,
    message: str,
) -> ProposalRecord:
    """Append an auditable failure for every rejection before canonical writes."""

    proposal = record.proposal
    attempt = ApplicationAttempt(
        id=f"{proposal.id}:attempt:{len(record.attempts) + 1}",
        proposal_id=proposal.id,
        basis_digest=proposal.basis_digest,
        actor=actor,
        dry_run=dry_run,
        status=AttemptStatus.PREFLIGHT_FAILED,
        message=message,
    )
    return record.model_copy(update={"attempts": record.attempts + (attempt,)})


def _append_attempt_state(
    record: ProposalRecord,
    attempt: ApplicationAttempt,
    state: ProposalState,
    actor: str,
    rationale: str,
) -> ProposalRecord:
    transition = ProposalTransition(
        sequence=len(record.history) + 1,
        from_state=record.proposal.state,
        to_state=state,
        actor=actor,
        rationale=rationale,
    )
    proposal = record.proposal.model_copy(update={"state": state})
    return ProposalRecord(
        proposal=proposal,
        history=record.history + (transition,),
        attempts=record.attempts + (attempt,),
    )


def _append_state(
    record: ProposalRecord,
    state: ProposalState,
    *,
    actor: str,
    rationale: str,
) -> ProposalRecord:
    if not rationale.strip() or not actor.strip():
        raise ValueError("proposal transitions require actor and rationale")
    transition = ProposalTransition(
        sequence=len(record.history) + 1,
        from_state=record.proposal.state,
        to_state=state,
        actor=actor,
        rationale=rationale,
    )
    proposal = record.proposal.model_copy(update={"state": state})
    return ProposalRecord(
        proposal=proposal,
        history=record.history + (transition,),
        attempts=record.attempts,
    )


def _history_transition_allowed(
    previous: ProposalState | None, current: ProposalState
) -> bool:
    if previous is None:
        return current is ProposalState.DRAFT
    if previous is ProposalState.APPROVED:
        return current in {
            ProposalState.APPLIED,
            ProposalState.WRITE_FAILED,
            ProposalState.VALIDATION_FAILED,
        }
    return current in _ALLOWED_TRANSITIONS[previous]


def _validate_postconditions(
    proposal: DesignProposal, snapshot: DesignSnapshot
) -> tuple[str, ...]:
    failures: list[str] = []
    entities = {item.id: item for item in snapshot.entities}
    assertions = {(item.subject_id, item.predicate): item.value for item in snapshot.assertions}
    for condition in proposal.postconditions:
        passed = False
        if condition.kind == "entity_exists":
            passed = condition.target_id in entities
        elif condition.kind == "assertion_equals":
            passed = assertions.get((condition.target_id, condition.predicate)) == condition.expected
        elif condition.kind == "snapshot_digest_changed":
            passed = snapshot_content_digest(snapshot) != snapshot_content_digest(proposal.basis.snapshot)
        else:
            failures.append(f"{condition.id}: unsupported postcondition kind {condition.kind!r}")
            continue
        if condition.required and not passed:
            failures.append(f"{condition.id}: {condition.description}")
    return tuple(failures)
