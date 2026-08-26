"""Deterministic, game-neutral behavioral regression cases."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import Field, JsonValue, model_validator

from playtrace.models import DecisionState, DesignSnapshot, StrictModel
from playtrace.simulation import (
    InProcessSimulationRunner,
    SimulationConfig,
    SimulationRun,
    content_digest,
    snapshot_content_digest,
)


class RegressionCaseKind(StrEnum):
    APPROVED_BEHAVIOR = "approved_behavior"
    ADVERSARIAL = "adversarial"


class RegressionExpectation(StrictModel):
    status: str | None = None
    blocker_ids: tuple[str, ...] | None = None
    event_type_counts: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, int] = Field(default_factory=dict)
    final_state: dict[str, JsonValue] = Field(default_factory=dict)


class RegressionCaseApproval(StrictModel):
    actor: str
    rationale: str
    source_application_attempt_id: str | None = None
    source_basis_digest: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def require_attribution(self) -> "RegressionCaseApproval":
        if not self.actor.strip() or not self.rationale.strip():
            raise ValueError("regression case approval requires actor and rationale")
        return self


class BehavioralRegressionCase(StrictModel):
    id: str
    title: str
    project_id: str
    plugin_id: str
    snapshot_id: str
    snapshot_digest: str
    config: SimulationConfig
    expectation: RegressionExpectation
    kind: RegressionCaseKind
    rationale: str
    source_proposal_id: str | None = None
    state: DecisionState = DecisionState.PROPOSED
    approval: RegressionCaseApproval | None = None

    @model_validator(mode="after")
    def accepted_cases_have_an_explicit_decision(self) -> "BehavioralRegressionCase":
        if self.kind is RegressionCaseKind.ADVERSARIAL and not self.rationale.strip():
            raise ValueError("adversarial regression cases require a nonblank rationale")
        if self.state is DecisionState.ACCEPTED and self.approval is None:
            raise ValueError("accepted regression cases require explicit approval")
        if self.state is not DecisionState.ACCEPTED and self.approval is not None:
            raise ValueError("only accepted regression cases may carry approval metadata")
        if self.kind is RegressionCaseKind.APPROVED_BEHAVIOR:
            if self.state is DecisionState.ACCEPTED and (
                not self.source_proposal_id
                or self.approval is None
                or not self.approval.source_application_attempt_id
                or not self.approval.source_basis_digest
            ):
                raise ValueError(
                    "approved behavior cases require a verified applied proposal decision"
                )
        elif self.source_proposal_id is not None:
            raise ValueError("adversarial cases cannot claim an applied proposal as their origin")
        return self

    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))

    def content_digest(self) -> str:
        """Digest immutable case content independently of its approval decision."""

        return content_digest(
            self.model_dump(
                mode="json",
                exclude={"state", "approval", "source_proposal_id"},
            )
        )


def accept_regression_case(
    case: BehavioralRegressionCase,
    *,
    actor: str,
    rationale: str,
    applied_proposal: Any | None = None,
) -> BehavioralRegressionCase:
    """Explicitly accept a case, verifying approved behavior against application history."""

    from playtrace.proposals import AttemptStatus, ProposalRecord, ProposalState

    if case.state is not DecisionState.PROPOSED:
        raise ValueError("only a proposed regression case may be accepted")
    source_proposal_id = None
    attempt_id = None
    basis_digest = None
    if case.kind is RegressionCaseKind.APPROVED_BEHAVIOR:
        if not isinstance(applied_proposal, ProposalRecord):
            raise ValueError("approved behavior promotion requires a loaded proposal record")
        proposal = applied_proposal.proposal
        if proposal.state is not ProposalState.APPLIED:
            raise ValueError("approved behavior promotion requires an applied proposal")
        if proposal.project_id != case.project_id:
            raise ValueError("regression case and applied proposal target different projects")
        applied_attempts = [
            attempt
            for attempt in applied_proposal.attempts
            if attempt.status is AttemptStatus.APPLIED and not attempt.dry_run
        ]
        if not applied_attempts:
            raise ValueError("applied proposal has no successful application attempt")
        source_proposal_id = proposal.id
        attempt_id = applied_attempts[-1].id
        basis_digest = proposal.basis_digest
    elif applied_proposal is not None:
        raise ValueError("adversarial cases are accepted without a proposal origin")

    approval = RegressionCaseApproval(
        actor=actor,
        rationale=rationale,
        source_application_attempt_id=attempt_id,
        source_basis_digest=basis_digest,
    )
    payload = case.model_dump(mode="json")
    payload.update(
        {
            "source_proposal_id": source_proposal_id,
            "state": DecisionState.ACCEPTED,
            "approval": approval,
        }
    )
    return BehavioralRegressionCase.model_validate(payload)


class BehavioralRegressionSuite(StrictModel):
    id: str
    project_id: str
    cases: tuple[BehavioralRegressionCase, ...]

    @model_validator(mode="after")
    def require_unique_accepted_cases(self) -> "BehavioralRegressionSuite":
        if not self.cases:
            raise ValueError("a regression suite needs at least one case")
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("regression case IDs must be unique")
        wrong_project = [case.id for case in self.cases if case.project_id != self.project_id]
        if wrong_project:
            raise ValueError(f"regression cases target another project: {wrong_project}")
        rejected = [case.id for case in self.cases if case.state is not DecisionState.ACCEPTED]
        if rejected:
            raise ValueError(f"only accepted regression cases may enter a suite: {rejected}")
        return self

    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class RegressionCaseResult(StrictModel):
    case_id: str
    passed: bool
    failures: tuple[str, ...]
    run_id: str | None = None
    run_digest: str | None = None


class RegressionSuiteRun(StrictModel):
    id: str
    suite_id: str
    project_id: str
    snapshot_id: str
    passed: bool
    case_results: tuple[RegressionCaseResult, ...]
    digest: str

    @model_validator(mode="after")
    def digest_matches_payload(self) -> "RegressionSuiteRun":
        payload = self.model_dump(mode="json", exclude={"digest"})
        if self.digest != content_digest(payload):
            raise ValueError("regression suite run digest does not match its results")
        return self


class PluginRegistry(Protocol):
    def create(self, plugin_id: str) -> Any: ...


class SimulationRunner(Protocol):
    def run(self, plugin: Any, snapshot: DesignSnapshot, config: SimulationConfig) -> SimulationRun: ...


class BehavioralRegressionRunner:
    def __init__(
        self,
        registry: PluginRegistry,
        runner: SimulationRunner | None = None,
    ) -> None:
        self.registry = registry
        self.runner = runner or InProcessSimulationRunner()

    def run(
        self, suite: BehavioralRegressionSuite, snapshot: DesignSnapshot
    ) -> RegressionSuiteRun:
        if snapshot.project_id != suite.project_id:
            raise ValueError("regression suite project does not match the snapshot")
        current_digest = snapshot_content_digest(snapshot)
        results: list[RegressionCaseResult] = []
        for case in suite.cases:
            failures: list[str] = []
            try:
                plugin = self.registry.create(case.plugin_id)
                run = self.runner.run(plugin, snapshot, case.config)
                failures.extend(_compare_run(run, case.expectation))
                results.append(
                    RegressionCaseResult(
                        case_id=case.id,
                        passed=not failures,
                        failures=tuple(failures),
                        run_id=run.id,
                        run_digest=stable_run_digest(run),
                    )
                )
            except Exception as error:
                results.append(
                    RegressionCaseResult(
                        case_id=case.id,
                        passed=False,
                        failures=(f"regression execution failed: {error}",),
                    )
                )
        base = {
            "id": f"{suite.id}:run:{current_digest[:12]}:{suite.digest()[:12]}",
            "suite_id": suite.id,
            "project_id": suite.project_id,
            "snapshot_id": snapshot.id,
            "passed": all(item.passed for item in results),
            "case_results": tuple(results),
        }
        return RegressionSuiteRun(**base, digest=content_digest(_json(base)))


def stable_run_digest(run: SimulationRun) -> str:
    """Digest deterministic simulation meaning while excluding observation clocks."""

    payload = run.model_dump(mode="json")
    payload["provenance"].pop("observed_at", None)
    return content_digest(payload)


def _compare_run(run: SimulationRun, expected: RegressionExpectation) -> tuple[str, ...]:
    failures: list[str] = []
    status = run.final_state.status.value if run.final_state else None
    if expected.status is not None and status != expected.status:
        failures.append(f"status expected {expected.status!r}, got {status!r}")
    blockers = tuple(item.id for item in run.blockers)
    if expected.blocker_ids is not None and blockers != expected.blocker_ids:
        failures.append(f"blocker IDs expected {expected.blocker_ids!r}, got {blockers!r}")
    counts = Counter(item.event_type for item in run.events)
    for event_type, count in expected.event_type_counts.items():
        if counts[event_type] != count:
            failures.append(
                f"event {event_type!r} count expected {count}, got {counts[event_type]}"
            )
    metrics = run.metrics.model_dump(mode="json")
    for name, value in expected.metrics.items():
        if metrics.get(name) != value:
            failures.append(f"metric {name!r} expected {value!r}, got {metrics.get(name)!r}")
    state = run.final_state.model_dump(mode="json") if run.final_state else {}
    for selector, value in expected.final_state.items():
        actual = _select(state, selector)
        if actual != value:
            failures.append(f"state {selector!r} expected {value!r}, got {actual!r}")
    return tuple(failures)


def _select(value: Any, selector: str) -> Any:
    current = value
    for part in selector.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value
