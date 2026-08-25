"""Disposable SQLite and FTS5 working state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from playtrace.knowledge import KnowledgeBase
from playtrace.evidence import ExperimentEvidence
from playtrace.models import DesignSnapshot, EvidenceClaim, Hypothesis
from playtrace.reviewing import DesignReview
from playtrace.simulation import SimulationRun
from playtrace.workflow import WorkflowRun


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
        )
        return {
            name: self.connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in names
        }
