"""Disposable SQLite and FTS5 working state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from playtrace.knowledge import KnowledgeBase
from playtrace.evidence import ExperimentEvidence
from playtrace.models import DecisionState, DesignSnapshot, EvidenceClaim, Hypothesis
from playtrace.reviewing import DesignReview
from playtrace.simulation import SimulationRun
from playtrace.workflow import WorkflowRun
from playtrace.extensions import ExtensionProposalState, GeneratedExtensionProposal
from playtrace.proposals import (
    ApplicationAttempt,
    AttemptStatus,
    DesignProposal,
    ProposalRecord,
    ProposalState,
    ProposalTransition,
)
from playtrace.regressions import (
    BehavioralRegressionCase,
    RegressionCaseKind,
    RegressionSuiteRun,
    accept_regression_case,
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS design_reviews (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS simulation_runs (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_evidence (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policies (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS precedents (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiential_evidence (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposal_records (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    basis_digest TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposal_transitions (
    proposal_id TEXT NOT NULL REFERENCES proposal_records(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (proposal_id, sequence)
);

CREATE TABLE IF NOT EXISTS application_attempts (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposal_records(id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS regression_cases (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS regression_runs (
    id TEXT PRIMARY KEY,
    suite_id TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extension_proposals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    record_id UNINDEXED,
    record_type UNINDEXED,
    title,
    body,
    tokenize = 'unicode61'
);
"""


class PlayTraceStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "PlayTraceStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def save_snapshot(self, snapshot: DesignSnapshot) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO snapshots(id, project_id, payload) VALUES (?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       project_id = excluded.project_id,
                       payload = excluded.payload""",
                (snapshot.id, snapshot.project_id, snapshot.model_dump_json()),
            )
            self.index(
                record_id=snapshot.id,
                record_type="snapshot",
                title=snapshot.project_id,
                body=" ".join(
                    [entity.name for entity in snapshot.entities]
                    + [assertion.predicate for assertion in snapshot.assertions]
                ),
            )

    def load_snapshot(self, snapshot_id: str) -> DesignSnapshot | None:
        row = self.connection.execute(
            "SELECT payload FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        return DesignSnapshot.model_validate_json(row["payload"]) if row else None

    def save_claim(self, snapshot_id: str, claim: EvidenceClaim) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO claims(id, snapshot_id, payload) VALUES (?, ?, ?)",
                (claim.id, snapshot_id, claim.model_dump_json()),
            )
            self.index(claim.id, "claim", claim.statement, " ".join(claim.scope))

    def load_claim(self, claim_id: str) -> EvidenceClaim | None:
        row = self.connection.execute(
            "SELECT payload FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        return EvidenceClaim.model_validate_json(row["payload"]) if row else None

    def save_hypothesis(self, snapshot_id: str, hypothesis: Hypothesis) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO hypotheses(id, snapshot_id, payload) VALUES (?, ?, ?)",
                (hypothesis.id, snapshot_id, hypothesis.model_dump_json()),
            )
            self.index(
                hypothesis.id,
                "hypothesis",
                hypothesis.statement,
                hypothesis.test_method,
            )

    def load_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        row = self.connection.execute(
            "SELECT payload FROM hypotheses WHERE id = ?", (hypothesis_id,)
        ).fetchone()
        return Hypothesis.model_validate_json(row["payload"]) if row else None

    def save_experiment_evidence(self, evidence: ExperimentEvidence) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO experiment_evidence
                   (id, project_id, snapshot_id, hypothesis_id, payload)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    evidence.id,
                    evidence.project_id,
                    evidence.snapshot_id,
                    evidence.hypothesis_link.hypothesis_id,
                    evidence.model_dump_json(),
                ),
            )
            self.index(
                evidence.id,
                "experiment_evidence",
                evidence.summary,
                " ".join((*evidence.observations, *evidence.scope)),
            )

    def load_experiment_evidence(self, evidence_id: str) -> ExperimentEvidence | None:
        row = self.connection.execute(
            "SELECT payload FROM experiment_evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        return ExperimentEvidence.model_validate_json(row["payload"]) if row else None

    def list_experiment_evidence(
        self, *, project_id: str, snapshot_id: str | None = None
    ) -> tuple[ExperimentEvidence, ...]:
        if snapshot_id is None:
            rows = self.connection.execute(
                "SELECT payload FROM experiment_evidence WHERE project_id = ? ORDER BY id",
                (project_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """SELECT payload FROM experiment_evidence
                   WHERE project_id = ? AND snapshot_id = ? ORDER BY id""",
                (project_id, snapshot_id),
            ).fetchall()
        return tuple(ExperimentEvidence.model_validate_json(row["payload"]) for row in rows)

    def save_workflow(self, run: WorkflowRun) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO workflow_runs(id, snapshot_id, payload) VALUES (?, ?, ?)",
                (run.id, run.snapshot_id, run.model_dump_json()),
            )

    def save_proposal(self, record: ProposalRecord) -> None:
        """Append lifecycle facts; never rewrite an existing transition or attempt."""

        proposal = record.proposal
        existing = self.load_proposal(proposal.id)
        approval_matches = proposal.basis_digest == proposal.approval_digest()
        terminal_attempt = {
            ProposalState.APPLIED: AttemptStatus.APPLIED,
            ProposalState.WRITE_FAILED: AttemptStatus.WRITE_FAILED,
            ProposalState.VALIDATION_FAILED: AttemptStatus.VALIDATION_FAILED,
        }.get(proposal.state)
        if terminal_attempt is not None and (
            not record.attempts or record.attempts[-1].status is not terminal_attempt
        ):
            raise ValueError("terminal proposal state requires its matching application attempt")
        if not approval_matches:
            # A failed approval-integrity preflight is itself durable evidence. Retain
            # the last valid proposal payload and append only the typed failure.
            if existing is None:
                raise ValueError("proposal content does not match its approval digest")
            new_attempts = record.attempts[len(existing.attempts) :]
            if (
                record.history != existing.history
                or record.attempts[: len(existing.attempts)] != existing.attempts
                or not new_attempts
                or any(item.status is not AttemptStatus.PREFLIGHT_FAILED for item in new_attempts)
            ):
                raise ValueError("invalid proposal content may only append preflight failures")
            proposal = existing.proposal
        if existing is None and (
            proposal.state is not ProposalState.DRAFT
            or len(record.history) != 1
            or record.attempts
        ):
            raise ValueError("a new persisted proposal must enter as an untouched draft")
        if existing is not None:
            if existing.proposal.basis_digest != proposal.basis_digest:
                raise ValueError("a persisted proposal basis cannot be replaced")
            if record.history[: len(existing.history)] != existing.history:
                raise ValueError("proposal history is append-only")
            if record.attempts[: len(existing.attempts)] != existing.attempts:
                raise ValueError("application attempts are append-only")
            if len(record.history) < len(existing.history) or len(record.attempts) < len(existing.attempts):
                raise ValueError("proposal persistence cannot remove history")
        with self.connection:
            self.connection.execute(
                """INSERT INTO proposal_records(id, project_id, basis_digest, payload)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET payload = excluded.payload""",
                (
                    proposal.id,
                    proposal.project_id,
                    proposal.basis_digest,
                    proposal.model_dump_json(),
                ),
            )
            start_history = len(existing.history) if existing else 0
            for transition in record.history[start_history:]:
                self.connection.execute(
                    "INSERT INTO proposal_transitions(proposal_id, sequence, payload) VALUES (?, ?, ?)",
                    (proposal.id, transition.sequence, transition.model_dump_json()),
                )
            start_attempts = len(existing.attempts) if existing else 0
            for attempt in record.attempts[start_attempts:]:
                self.connection.execute(
                    "INSERT INTO application_attempts(id, proposal_id, payload) VALUES (?, ?, ?)",
                    (attempt.id, proposal.id, attempt.model_dump_json()),
                )
            self.index(
                proposal.id,
                "design_proposal",
                proposal.title,
                f"{proposal.question} {proposal.rationale} {proposal.state.value}",
            )

    def load_proposal(self, proposal_id: str) -> ProposalRecord | None:
        proposal_row = self.connection.execute(
            "SELECT payload FROM proposal_records WHERE id = ?", (proposal_id,)
        ).fetchone()
        if proposal_row is None:
            return None
        transitions = self.connection.execute(
            "SELECT payload FROM proposal_transitions WHERE proposal_id = ? ORDER BY sequence",
            (proposal_id,),
        ).fetchall()
        attempts = self.connection.execute(
            "SELECT payload FROM application_attempts WHERE proposal_id = ? ORDER BY rowid",
            (proposal_id,),
        ).fetchall()
        return ProposalRecord(
            proposal=DesignProposal.model_validate_json(proposal_row["payload"]),
            history=tuple(
                ProposalTransition.model_validate_json(row["payload"]) for row in transitions
            ),
            attempts=tuple(
                ApplicationAttempt.model_validate_json(row["payload"]) for row in attempts
            ),
        )

    def list_proposals(self, project_id: str) -> tuple[ProposalRecord, ...]:
        rows = self.connection.execute(
            "SELECT id FROM proposal_records WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
        return tuple(
            item for row in rows if (item := self.load_proposal(row["id"])) is not None
        )

    def save_regression_case(self, case: BehavioralRegressionCase) -> None:
        case = self.validate_regression_case(case)
        existing = self.load_regression_case(case.id)
        if existing is not None:
            if existing.content_digest() != case.content_digest():
                raise ValueError("regression case content is immutable; create a new case ID")
            if existing.state is not DecisionState.PROPOSED and existing != case:
                raise ValueError("a decided regression case is immutable")
            if existing.state is DecisionState.PROPOSED and case.state not in {
                DecisionState.PROPOSED,
                DecisionState.ACCEPTED,
                DecisionState.REJECTED,
            }:
                raise ValueError("invalid regression case decision transition")
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO regression_cases(id, project_id, payload) VALUES (?, ?, ?)",
                (case.id, case.project_id, case.model_dump_json()),
            )

    def validate_regression_case(
        self, case: BehavioralRegressionCase
    ) -> BehavioralRegressionCase:
        """Revalidate public input and verify any applied-proposal approval origin."""

        case = BehavioralRegressionCase.model_validate(case.model_dump(mode="json"))
        if (
            case.state is DecisionState.ACCEPTED
            and case.kind is RegressionCaseKind.APPROVED_BEHAVIOR
        ):
            record = self.load_proposal(case.source_proposal_id or "")
            if record is None or record.proposal.state is not ProposalState.APPLIED:
                raise ValueError(
                    "approved behavior case origin is not a loaded applied proposal"
                )
            approval = case.approval
            if approval is None or approval.source_basis_digest != record.proposal.basis_digest:
                raise ValueError("approved behavior case basis does not match its proposal")
            attempt = next(
                (
                    item
                    for item in record.attempts
                    if item.id == approval.source_application_attempt_id
                ),
                None,
            )
            if (
                attempt is None
                or attempt.status is not AttemptStatus.APPLIED
                or attempt.dry_run
            ):
                raise ValueError(
                    "approved behavior case does not reference a successful application"
                )
        return case

    def accept_regression_case(
        self,
        case: BehavioralRegressionCase,
        *,
        actor: str,
        rationale: str,
        proposal_id: str | None = None,
    ) -> BehavioralRegressionCase:
        """Accept and persist a proposed case through the store-backed authority path."""

        record = None
        if case.kind is RegressionCaseKind.APPROVED_BEHAVIOR:
            if not proposal_id:
                raise ValueError("approved behavior acceptance requires a proposal ID")
            record = self.load_proposal(proposal_id)
            if record is None:
                raise ValueError("approved behavior origin is not a loaded proposal")
        elif proposal_id is not None:
            raise ValueError("adversarial cases cannot claim a proposal origin")
        accepted = accept_regression_case(
            case,
            actor=actor,
            rationale=rationale,
            applied_proposal=record,
        )
        self.save_regression_case(accepted)
        return accepted

    def load_regression_case(self, case_id: str) -> BehavioralRegressionCase | None:
        row = self.connection.execute(
            "SELECT payload FROM regression_cases WHERE id = ?", (case_id,)
        ).fetchone()
        return BehavioralRegressionCase.model_validate_json(row["payload"]) if row else None

    def save_regression_run(self, run: RegressionSuiteRun) -> None:
        existing = self.load_regression_run(run.id)
        if existing is not None and existing != run:
            raise ValueError("a regression run ID cannot be replaced with different results")
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO regression_runs(id, suite_id, payload) VALUES (?, ?, ?)",
                (run.id, run.suite_id, run.model_dump_json()),
            )

    def load_regression_run(self, run_id: str) -> RegressionSuiteRun | None:
        row = self.connection.execute(
            "SELECT payload FROM regression_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return RegressionSuiteRun.model_validate_json(row["payload"]) if row else None

    def list_regression_runs(self, project_id: str | None = None) -> tuple[RegressionSuiteRun, ...]:
        rows = self.connection.execute(
            "SELECT payload FROM regression_runs ORDER BY id"
        ).fetchall()
        runs = tuple(RegressionSuiteRun.model_validate_json(row["payload"]) for row in rows)
        return tuple(item for item in runs if project_id is None or item.project_id == project_id)

    def save_extension_proposal(self, proposal: GeneratedExtensionProposal) -> None:
        proposal = GeneratedExtensionProposal.model_validate(
            proposal.model_dump(mode="json")
        )
        existing = self.load_extension_proposal(proposal.id)
        if existing is None:
            if proposal.state is not ExtensionProposalState.PROPOSED or len(proposal.history) != 1:
                raise ValueError("a new extension proposal must enter in proposed state")
        else:
            if existing.digest != proposal.digest:
                raise ValueError("generated extension content is immutable")
            if proposal.history[: len(existing.history)] != existing.history:
                raise ValueError("extension proposal history is append-only")
            if len(proposal.history) < len(existing.history):
                raise ValueError("extension persistence cannot remove history")
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO extension_proposals(id, project_id, payload) VALUES (?, ?, ?)",
                (proposal.id, proposal.project_id, proposal.model_dump_json()),
            )
            self.index(proposal.id, "extension_proposal", proposal.title, proposal.rationale)

    def load_extension_proposal(self, proposal_id: str) -> GeneratedExtensionProposal | None:
        row = self.connection.execute(
            "SELECT payload FROM extension_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return GeneratedExtensionProposal.model_validate_json(row["payload"]) if row else None

    def list_extension_proposals(self, project_id: str) -> tuple[GeneratedExtensionProposal, ...]:
        rows = self.connection.execute(
            "SELECT payload FROM extension_proposals WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
        return tuple(GeneratedExtensionProposal.model_validate_json(row["payload"]) for row in rows)

    def save_knowledge(self, project_id: str, knowledge: KnowledgeBase) -> None:
        with self.connection:
            for policy in knowledge.policies:
                self.connection.execute(
                    "INSERT OR REPLACE INTO policies(id, project_id, payload) VALUES (?, ?, ?)",
                    (policy.id, project_id, policy.model_dump_json()),
                )
                self.index(policy.id, "policy", policy.title, policy.statement)
            for precedent in knowledge.precedents:
                self.connection.execute(
                    "INSERT OR REPLACE INTO precedents(id, payload) VALUES (?, ?)",
                    (precedent.id, precedent.model_dump_json()),
                )
                self.index(
                    precedent.id,
                    "precedent",
                    precedent.title,
                    f"{precedent.question} {precedent.decision} {precedent.rationale}",
                )
            for evidence in knowledge.experiential_evidence:
                self.connection.execute(
                    "INSERT OR REPLACE INTO experiential_evidence(id, project_id, payload) VALUES (?, ?, ?)",
                    (evidence.id, project_id, evidence.model_dump_json()),
                )
                self.index(
                    evidence.id,
                    "experiential_evidence",
                    evidence.summary,
                    " ".join(evidence.observations),
                )

    def save_review(self, review: DesignReview) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO design_reviews(id, snapshot_id, payload) VALUES (?, ?, ?)",
                (review.id, review.snapshot_id, review.model_dump_json()),
            )
            self.index(
                review.id,
                "design_review",
                review.intent.desired_outcome or review.intent.original_prompt,
                " ".join(
                    [claim.statement for claim in review.claims]
                    + [finding.statement for finding in review.critic_findings]
                ),
            )

    def load_review(self, review_id: str) -> DesignReview | None:
        row = self.connection.execute(
            "SELECT payload FROM design_reviews WHERE id = ?", (review_id,)
        ).fetchone()
        return DesignReview.model_validate_json(row["payload"]) if row else None

    def save_simulation(self, run: SimulationRun) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO simulation_runs(id, snapshot_id, payload) VALUES (?, ?, ?)",
                (run.id, run.provenance.snapshot_id, run.model_dump_json()),
            )
            self.index(
                run.id,
                "simulation_run",
                f"{run.config.id} {run.final_state.status.value if run.final_state else 'blocked'}",
                " ".join(
                    [event.event_type for event in run.events]
                    + [blocker.message for blocker in run.blockers]
                ),
            )

    def load_simulation(self, run_id: str) -> SimulationRun | None:
        row = self.connection.execute(
            "SELECT payload FROM simulation_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return SimulationRun.model_validate_json(row["payload"]) if row else None

    def index(self, record_id: str, record_type: str, title: str, body: str) -> None:
        self.connection.execute("DELETE FROM search_index WHERE record_id = ?", (record_id,))
        self.connection.execute(
            "INSERT INTO search_index(record_id, record_type, title, body) VALUES (?, ?, ?, ?)",
            (record_id, record_type, title, body),
        )

    def search(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        rows = self.connection.execute(
            """
            SELECT record_id, record_type, title,
                   snippet(search_index, 3, '<mark>', '</mark>', '…', 12) AS excerpt
            FROM search_index
            WHERE search_index MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def export_counts(self) -> dict[str, int]:
        names = (
            "snapshots",
            "claims",
            "workflow_runs",
            "design_reviews",
            "simulation_runs",
            "hypotheses",
            "experiment_evidence",
            "policies",
            "precedents",
            "experiential_evidence",
            "proposal_records",
            "proposal_transitions",
            "application_attempts",
            "regression_cases",
            "regression_runs",
            "extension_proposals",
        )
        return {
            name: self.connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in names
        }
