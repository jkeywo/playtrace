"""Disposable SQLite and FTS5 working state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from playtrace.canonical import (
    CANONICAL_IDENTITY_SCHEMA,
    CANONICAL_IDENTITY_VERSION,
)
from playtrace.knowledge import KnowledgeBase
from playtrace.evidence import ExperimentEvidence
from playtrace.models import DecisionState, DesignSnapshot, EvidenceClaim, Hypothesis
from playtrace.reviewing import DesignReview
from playtrace.simulation import SimulationRun
from playtrace.workflow import WorkflowRun
from playtrace.extensions import (
    ExtensionProposalState,
    GeneratedExtensionProposal,
    adopt_legacy_extension,
    extension_integrity_matches,
)
from playtrace.proposals import (
    ApplicationAttempt,
    AttemptStatus,
    DesignProposal,
    ProposalRecord,
    ProposalState,
    ProposalTransition,
    adopt_legacy_proposal,
    approval_integrity_matches,
)
from playtrace.regressions import (
    BehavioralRegressionCase,
    RegressionCaseKind,
    RegressionSuiteRun,
    accept_regression_case,
    adopt_legacy_regression_run,
    regression_run_integrity_matches,
)

_PROPOSAL_ARTIFACT = "design_proposal"
_REGRESSION_RUN_ARTIFACT = "regression_run"
_EXTENSION_ARTIFACT = "extension_proposal"
_LEGACY_DIGEST_SCHEMA = "legacy-json-sha256-v0"
_IDENTITY_ADOPTION_KEY = "identity_adoption_complete"
_IDENTITY_ADOPTION_VERSION = (
    f"{CANONICAL_IDENTITY_SCHEMA}/{CANONICAL_IDENTITY_VERSION}"
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

CREATE TABLE IF NOT EXISTS artifact_integrity (
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    schema TEXT NOT NULL,
    seal TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);

CREATE TABLE IF NOT EXISTS identity_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
        self.connection.execute("PRAGMA foreign_keys = ON")
        try:
            self._initialize_schema()
            self._initialize_identity_epoch()
        except Exception:
            self.connection.close()
            raise

    def _initialize_schema(self) -> None:
        """Create the M6A store without requiring the optional M6C migrations.

        A full installation uses the migration manager so the M6C operational
        tables are also available.  M6A remains independently usable when that
        module is not packaged (for example while staging milestones in
        isolation); in that case it owns only the base schema at version one.
        """

        statements = _split_schema_statements(SCHEMA)
        try:
            from playtrace.migrations import MigrationManager
        except ModuleNotFoundError as error:
            if error.name != "playtrace.migrations":
                raise
            self._apply_base_schema(statements)
        else:
            MigrationManager(
                self.connection, initial_statements=statements
            ).migrate()

        # A database already at a newer optional schema version does not replay
        # SCHEMA. Ensure M6A's independently-owned tables nevertheless exist.
        with self.connection:
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS artifact_integrity (
                       kind TEXT NOT NULL,
                       id TEXT NOT NULL,
                       schema TEXT NOT NULL,
                       seal TEXT NOT NULL,
                       PRIMARY KEY (kind, id)
                   )"""
            )
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS identity_metadata (
                       key TEXT PRIMARY KEY,
                       value TEXT NOT NULL
                   )"""
            )

    def _apply_base_schema(self, statements: tuple[str, ...]) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                if statement.strip():
                    self.connection.execute(statement)
            if int(self.connection.execute("PRAGMA user_version").fetchone()[0]) == 0:
                self.connection.execute("PRAGMA user_version = 1")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _initialize_identity_epoch(self) -> None:
        """Adopt every historical identity atomically, then close the epoch.

        Adoption is deliberately database-wide rather than a side effect of a
        load.  No payload or sidecar is written until every proposal lifecycle,
        extension and regression run has validated.  Once the metadata row is
        present all records are verified strictly and can never re-enter the
        adoption path.
        """

        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT value FROM identity_metadata WHERE key = ?",
                (_IDENTITY_ADOPTION_KEY,),
            ).fetchone()
            if row is None:
                self._require_pristine_legacy_identity_store()
                updates, sidecars = self._validate_identity_records(adopting=True)
                for table, artifact_id, payload in updates:
                    self.connection.execute(
                        f"UPDATE {table} SET payload = ? WHERE id = ?",
                        (payload, artifact_id),
                    )
                for kind, artifact_id, schema, seal in sidecars:
                    self.connection.execute(
                        """INSERT INTO artifact_integrity(kind, id, schema, seal)
                           VALUES (?, ?, ?, ?)""",
                        (kind, artifact_id, schema, seal),
                    )
                self.connection.execute(
                    "INSERT INTO identity_metadata(key, value) VALUES (?, ?)",
                    (_IDENTITY_ADOPTION_KEY, _IDENTITY_ADOPTION_VERSION),
                )
            elif row["value"] == _IDENTITY_ADOPTION_VERSION:
                self._validate_identity_records(adopting=False)
            else:
                raise ValueError(
                    "unsupported identity adoption epoch "
                    f"{row['value']!r}; refusing to reinterpret durable identities"
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _require_pristine_legacy_identity_store(self) -> None:
        """Refuse to reopen an interrupted or previously completed adoption.

        Absence of the epoch marker grants adoption authority only to a
        completely pristine pre-M6A database.  A sidecar, any identity metadata,
        or either embedded M6A marker proves that the store is not pristine.  We
        deliberately do not try to infer or repair how far a prior adoption got:
        doing so could reseal tampered durable identities.
        """

        integrity_count = self.connection.execute(
            "SELECT COUNT(*) FROM artifact_integrity"
        ).fetchone()[0]
        if integrity_count:
            raise ValueError(
                "identity adoption epoch is missing from a non-pristine store "
                "containing integrity sidecars"
            )

        orphan_metadata = self.connection.execute(
            "SELECT key FROM identity_metadata LIMIT 1"
        ).fetchone()
        if orphan_metadata is not None:
            raise ValueError(
                "identity adoption epoch is missing from a non-pristine store "
                f"containing identity marker {orphan_metadata['key']!r}"
            )

        for label, table in (
            ("proposal", "proposal_records"),
            ("extension", "extension_proposals"),
            ("regression run", "regression_runs"),
        ):
            rows = self.connection.execute(
                f"SELECT id, payload FROM {table} ORDER BY id"
            ).fetchall()
            for artifact in rows:
                raw = _load_persisted_json(artifact["payload"])
                if "canonical_seal" in raw or "digest_schema" in raw:
                    raise ValueError(
                        "identity adoption epoch is missing from a non-pristine "
                        f"store: {label} {artifact['id']!r} contains an embedded "
                        "identity marker"
                    )

    def _validate_identity_records(
        self, *, adopting: bool
    ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str, str]]]:
        updates: list[tuple[str, str, str]] = []
        sidecars: list[tuple[str, str, str, str]] = []

        orphan_transition = self.connection.execute(
            """SELECT t.proposal_id FROM proposal_transitions AS t
               LEFT JOIN proposal_records AS p ON p.id = t.proposal_id
               WHERE p.id IS NULL LIMIT 1"""
        ).fetchone()
        orphan_attempt = self.connection.execute(
            """SELECT a.proposal_id FROM application_attempts AS a
               LEFT JOIN proposal_records AS p ON p.id = a.proposal_id
               WHERE p.id IS NULL LIMIT 1"""
        ).fetchone()
        if orphan_transition is not None or orphan_attempt is not None:
            raise ValueError("proposal lifecycle storage contains an orphaned record")
        for kind, table in (
            (_PROPOSAL_ARTIFACT, "proposal_records"),
            (_EXTENSION_ARTIFACT, "extension_proposals"),
            (_REGRESSION_RUN_ARTIFACT, "regression_runs"),
        ):
            orphan_sidecar = self.connection.execute(
                f"""SELECT i.id FROM artifact_integrity AS i
                    LEFT JOIN {table} AS artifact ON artifact.id = i.id
                    WHERE i.kind = ? AND artifact.id IS NULL LIMIT 1""",
                (kind,),
            ).fetchone()
            if orphan_sidecar is not None:
                raise ValueError(
                    f"{kind} {orphan_sidecar['id']!r} has an orphaned integrity seal"
                )

        proposal_rows = self.connection.execute(
            "SELECT id, project_id, basis_digest, payload FROM proposal_records ORDER BY id"
        ).fetchall()
        for row in proposal_rows:
            raw = _load_persisted_json(row["payload"])
            _require_raw_storage_identity(raw, "id", row["id"], "proposal")
            _require_raw_storage_identity(
                raw, "project_id", row["project_id"], "proposal project"
            )
            _require_raw_storage_identity(
                raw, "basis_digest", row["basis_digest"], "proposal digest"
            )
            if _raw_digest_schema(raw) == _LEGACY_DIGEST_SCHEMA:
                _verify_legacy_digest(
                    raw,
                    digest_field="basis_digest",
                    excluded={
                        "basis_digest",
                        "canonical_seal",
                        "digest_schema",
                        "state",
                        "created_at",
                    },
                    label=f"proposal {row['id']!r}",
                )
            proposal = DesignProposal.model_validate(raw)
            transition_rows = self.connection.execute(
                    """SELECT sequence, payload FROM proposal_transitions
                       WHERE proposal_id = ? ORDER BY sequence""",
                    (row["id"],),
                ).fetchall()
            transitions = tuple(
                ProposalTransition.model_validate(_load_persisted_json(item["payload"]))
                for item in transition_rows
            )
            for item, transition in zip(transition_rows, transitions, strict=True):
                if item["sequence"] != transition.sequence:
                    raise ValueError("proposal transition storage sequence does not match payload")
            attempt_rows = self.connection.execute(
                    """SELECT id, payload FROM application_attempts
                       WHERE proposal_id = ? ORDER BY rowid""",
                    (row["id"],),
                ).fetchall()
            attempts = tuple(
                ApplicationAttempt.model_validate(_load_persisted_json(item["payload"]))
                for item in attempt_rows
            )
            for item, attempt in zip(attempt_rows, attempts, strict=True):
                if item["id"] != attempt.id:
                    raise ValueError("application attempt storage ID does not match payload")
            # Full lifecycle validation is part of adoption, not deferred until
            # the proposal happens to be requested.
            ProposalRecord(proposal=proposal, history=transitions, attempts=attempts)
            adopted = adopt_legacy_proposal(proposal) if adopting else proposal
            if not approval_integrity_matches(adopted):
                raise ValueError(f"proposal {row['id']!r} failed identity validation")
            self._stage_identity(
                adopting=adopting,
                table="proposal_records",
                kind=_PROPOSAL_ARTIFACT,
                artifact=adopted,
                original=proposal,
                raw=raw,
                updates=updates,
                sidecars=sidecars,
            )

        extension_rows = self.connection.execute(
            "SELECT id, project_id, payload FROM extension_proposals ORDER BY id"
        ).fetchall()
        for row in extension_rows:
            raw = _load_persisted_json(row["payload"])
            _require_raw_storage_identity(raw, "id", row["id"], "extension")
            _require_raw_storage_identity(
                raw, "project_id", row["project_id"], "extension project"
            )
            if _raw_digest_schema(raw) == _LEGACY_DIGEST_SCHEMA:
                _verify_legacy_digest(
                    raw,
                    digest_field="digest",
                    excluded={
                        "digest",
                        "canonical_seal",
                        "digest_schema",
                        "state",
                        "history",
                        "created_at",
                    },
                    label=f"extension {row['id']!r}",
                )
            extension = GeneratedExtensionProposal.model_validate(raw)
            adopted = adopt_legacy_extension(extension) if adopting else extension
            if not extension_integrity_matches(adopted):
                raise ValueError(f"extension {row['id']!r} failed identity validation")
            self._stage_identity(
                adopting=adopting,
                table="extension_proposals",
                kind=_EXTENSION_ARTIFACT,
                artifact=adopted,
                original=extension,
                raw=raw,
                updates=updates,
                sidecars=sidecars,
            )

        regression_rows = self.connection.execute(
            "SELECT id, suite_id, payload FROM regression_runs ORDER BY id"
        ).fetchall()
        for row in regression_rows:
            raw = _load_persisted_json(row["payload"])
            _require_raw_storage_identity(raw, "id", row["id"], "regression run")
            _require_raw_storage_identity(
                raw, "suite_id", row["suite_id"], "regression suite"
            )
            if _raw_digest_schema(raw) == _LEGACY_DIGEST_SCHEMA:
                _verify_legacy_digest(
                    raw,
                    digest_field="digest",
                    excluded={"digest", "canonical_seal", "digest_schema"},
                    label=f"regression run {row['id']!r}",
                )
            run = RegressionSuiteRun.model_validate(raw)
            adopted = adopt_legacy_regression_run(run) if adopting else run
            if not regression_run_integrity_matches(adopted):
                raise ValueError(f"regression run {row['id']!r} failed identity validation")
            self._stage_identity(
                adopting=adopting,
                table="regression_runs",
                kind=_REGRESSION_RUN_ARTIFACT,
                artifact=adopted,
                original=run,
                raw=raw,
                updates=updates,
                sidecars=sidecars,
            )
        return updates, sidecars

    def _stage_identity(
        self,
        *,
        adopting: bool,
        table: str,
        kind: str,
        artifact: Any,
        original: Any,
        raw: dict[str, Any],
        updates: list[tuple[str, str, str]],
        sidecars: list[tuple[str, str, str, str]],
    ) -> None:
        integrity = self._artifact_integrity(kind, artifact.id)
        expected = (artifact.digest_schema, artifact.canonical_seal)
        if expected[1] is None:
            raise ValueError(f"{kind} {artifact.id!r} has no canonical integrity seal")
        if integrity is None:
            if not adopting:
                raise ValueError(f"{kind} {artifact.id!r} is missing its external integrity seal")
            sidecars.append((kind, artifact.id, expected[0], expected[1]))
        elif (integrity["schema"], integrity["seal"]) != expected:
            raise ValueError(f"{kind} {artifact.id!r} does not match its external integrity seal")
        if adopting and artifact != original:
            # Never round-trip legacy content through the typed model here.
            # Unordered fields such as frozensets would be serialized in a new
            # process order, invalidating the historical digest we just proved.
            sealed_payload = dict(raw)
            sealed_payload["digest_schema"] = artifact.digest_schema
            sealed_payload["canonical_seal"] = artifact.canonical_seal
            updates.append(
                (
                    table,
                    artifact.id,
                    json.dumps(
                        sealed_payload,
                        allow_nan=False,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            )

    @property
    def schema_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        self.connection.close()

    def _artifact_integrity(self, kind: str, artifact_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT schema, seal FROM artifact_integrity WHERE kind = ? AND id = ?",
            (kind, artifact_id),
        ).fetchone()

    def _require_artifact_integrity(
        self,
        *,
        kind: str,
        artifact_id: str,
        digest_schema: str,
        canonical_seal: str | None,
    ) -> None:
        """Verify an artifact after the database-wide adoption epoch closed."""

        integrity = self._artifact_integrity(kind, artifact_id)
        if integrity is None:
            raise ValueError(
                f"{kind} {artifact_id!r} is missing its external integrity seal"
            )
        if (
            integrity["schema"] != digest_schema
            or integrity["seal"] != canonical_seal
        ):
            raise ValueError(
                f"{kind} {artifact_id!r} does not match its external integrity seal"
            )

    def _insert_artifact_integrity(
        self,
        *,
        kind: str,
        artifact_id: str,
        digest_schema: str,
        canonical_seal: str | None,
    ) -> None:
        if canonical_seal is None:
            raise ValueError(f"{kind} {artifact_id!r} has no canonical integrity seal")
        existing = self._artifact_integrity(kind, artifact_id)
        if existing is not None:
            if (
                existing["schema"] != digest_schema
                or existing["seal"] != canonical_seal
            ):
                raise ValueError(
                    f"{kind} {artifact_id!r} cannot replace its external integrity seal"
                )
            return
        self.connection.execute(
            "INSERT INTO artifact_integrity(kind, id, schema, seal) VALUES (?, ?, ?, ?)",
            (kind, artifact_id, digest_schema, canonical_seal),
        )

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
        approval_matches = approval_integrity_matches(proposal)
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
            self._insert_artifact_integrity(
                kind=_PROPOSAL_ARTIFACT,
                artifact_id=proposal.id,
                digest_schema=proposal.digest_schema,
                canonical_seal=proposal.canonical_seal,
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
            "SELECT project_id, basis_digest, payload FROM proposal_records WHERE id = ?",
            (proposal_id,),
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
        proposal = DesignProposal.model_validate_json(proposal_row["payload"])
        if proposal.id != proposal_id:
            raise ValueError("proposal storage ID does not match its payload")
        if proposal.project_id != proposal_row["project_id"]:
            raise ValueError("proposal storage project does not match its payload")
        if proposal.basis_digest != proposal_row["basis_digest"]:
            raise ValueError("proposal storage digest does not match its payload")
        self._require_artifact_integrity(
            kind=_PROPOSAL_ARTIFACT,
            artifact_id=proposal_id,
            digest_schema=proposal.digest_schema,
            canonical_seal=proposal.canonical_seal,
        )
        return ProposalRecord(
            proposal=proposal,
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
        run = RegressionSuiteRun.model_validate(run.model_dump(mode="python"))
        if not regression_run_integrity_matches(run):
            raise ValueError("regression run lacks a valid canonical integrity seal")
        existing = self.load_regression_run(run.id)
        if existing is not None and existing != run:
            raise ValueError("a regression run ID cannot be replaced with different results")
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO regression_runs(id, suite_id, payload) VALUES (?, ?, ?)",
                (run.id, run.suite_id, run.model_dump_json()),
            )
            self._insert_artifact_integrity(
                kind=_REGRESSION_RUN_ARTIFACT,
                artifact_id=run.id,
                digest_schema=run.digest_schema,
                canonical_seal=run.canonical_seal,
            )

    def load_regression_run(self, run_id: str) -> RegressionSuiteRun | None:
        row = self.connection.execute(
            "SELECT suite_id, payload FROM regression_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        run = RegressionSuiteRun.model_validate_json(row["payload"])
        if run.id != run_id:
            raise ValueError("regression run storage ID does not match its payload")
        if run.suite_id != row["suite_id"]:
            raise ValueError("regression run storage suite does not match its payload")
        self._require_artifact_integrity(
            kind=_REGRESSION_RUN_ARTIFACT,
            artifact_id=run_id,
            digest_schema=run.digest_schema,
            canonical_seal=run.canonical_seal,
        )
        return run

    def list_regression_runs(self, project_id: str | None = None) -> tuple[RegressionSuiteRun, ...]:
        rows = self.connection.execute(
            "SELECT payload FROM regression_runs ORDER BY id"
        ).fetchall()
        runs = tuple(
            run
            for row in rows
            if (run := self.load_regression_run(
                RegressionSuiteRun.model_validate_json(row["payload"]).id
            ))
            is not None
        )
        return tuple(item for item in runs if project_id is None or item.project_id == project_id)

    def save_extension_proposal(self, proposal: GeneratedExtensionProposal) -> None:
        proposal = GeneratedExtensionProposal.model_validate(
            proposal.model_dump(mode="python")
        )
        if not extension_integrity_matches(proposal):
            raise ValueError("extension proposal lacks a valid canonical integrity seal")
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
            self._insert_artifact_integrity(
                kind=_EXTENSION_ARTIFACT,
                artifact_id=proposal.id,
                digest_schema=proposal.digest_schema,
                canonical_seal=proposal.canonical_seal,
            )
            self.index(proposal.id, "extension_proposal", proposal.title, proposal.rationale)

    def load_extension_proposal(self, proposal_id: str) -> GeneratedExtensionProposal | None:
        row = self.connection.execute(
            "SELECT project_id, payload FROM extension_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            return None
        proposal = GeneratedExtensionProposal.model_validate_json(row["payload"])
        if proposal.id != proposal_id:
            raise ValueError("extension storage ID does not match its payload")
        if proposal.project_id != row["project_id"]:
            raise ValueError("extension storage project does not match its payload")
        self._require_artifact_integrity(
            kind=_EXTENSION_ARTIFACT,
            artifact_id=proposal_id,
            digest_schema=proposal.digest_schema,
            canonical_seal=proposal.canonical_seal,
        )
        return proposal

    def list_extension_proposals(self, project_id: str) -> tuple[GeneratedExtensionProposal, ...]:
        rows = self.connection.execute(
            "SELECT payload FROM extension_proposals WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
        return tuple(
            proposal
            for row in rows
            if (proposal := self.load_extension_proposal(
                GeneratedExtensionProposal.model_validate_json(row["payload"]).id
            ))
            is not None
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


def _split_schema_statements(script: str) -> tuple[str, ...]:
    """Split the deliberately simple base schema without an M6C dependency."""

    statements: list[str] = []
    buffer = ""
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("PRAGMA foreign_keys"):
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip().rstrip(";"))
            buffer = ""
    if buffer.strip():
        raise ValueError("incomplete SQLite schema statement")
    return tuple(statements)


def _load_persisted_json(payload: str) -> dict[str, Any]:
    """Load JSON without losing persisted collection order or duplicate keys."""

    def ordered_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"persisted identity JSON has duplicate key {key!r}")
            result[key] = value
        return result

    value = json.loads(payload, object_pairs_hook=ordered_object)
    if not isinstance(value, dict):
        raise ValueError("persisted identity payload must be a JSON object")
    return value


def _require_raw_storage_identity(
    raw: dict[str, Any], field: str, stored: str, label: str
) -> None:
    if raw.get(field) != stored:
        raise ValueError(f"{label} storage {field} does not match its payload")


def _raw_digest_schema(raw: dict[str, Any]) -> str:
    schema = raw.get("digest_schema", _LEGACY_DIGEST_SCHEMA)
    if not isinstance(schema, str):
        raise ValueError("persisted digest schema must be a string")
    return schema


def _verify_legacy_digest(
    raw: dict[str, Any],
    *,
    digest_field: str,
    excluded: set[str],
    label: str,
) -> None:
    """Verify the pre-M6A SHA-256 against the raw persisted JSON value order.

    Old Pydantic models could contain frozensets.  Their process-dependent order
    became an ordinary JSON array on disk, so reparsing through today's typed
    model before hashing would no longer prove the historical digest.  Hash the
    raw persisted structure with the exact legacy JSON algorithm instead.
    """

    historical = raw.get(digest_field)
    if not isinstance(historical, str):
        raise ValueError(f"{label} has no textual historical digest")
    content = {key: value for key, value in raw.items() if key not in excluded}
    encoded = json.dumps(
        content,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    expected = hashlib.sha256(encoded).hexdigest()
    if historical != expected:
        raise ValueError(f"{label} historical digest does not match persisted JSON")
