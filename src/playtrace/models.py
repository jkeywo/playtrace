"""Typed records shared by every Play Trace project adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Base model which rejects misspelled or undeclared fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceMedium(StrEnum):
    SYNTHETIC = "synthetic"
    DRIVE = "drive"
    GIT = "git"
    MIXED = "mixed"


class AuthoredStatus(StrEnum):
    CONFIRMED = "confirmed"
    WORKING = "working"
    PLACEHOLDER = "placeholder"
    OPEN_QUESTION = "open_question"
    DEFERRED = "deferred"
    UNSPECIFIED = "unspecified"


class IntentKind(StrEnum):
    QUESTION = "question"
    DIAGNOSIS = "diagnosis"
    CHANGE_REQUEST = "change_request"
    EXPERIMENT = "experiment"
    CONTENT_ADDITION = "content_addition"
    PLAYTEST_FEEDBACK = "playtest_feedback"
    CONSTRAINT_CHANGE = "constraint_change"
    DESIGN_POLICY_CHANGE = "design_policy_change"
    IMPLEMENTATION_REQUEST = "implementation_request"


class Persistence(StrEnum):
    TRANSACTIONAL = "transactional"
    PROJECT = "project"
    SHARED = "shared"


class EvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT = "context"
    JUDGMENT = "judgment"


class DecisionState(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEFERRED = "deferred"


class SourceRevision(StrictModel):
    id: str
    source_id: str
    medium: SourceMedium
    locator: str
    revision: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    content_digest: str | None = None


class ProvenanceRef(StrictModel):
    source_revision_id: str
    location: str
    extractor: str
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DesignEntity(StrictModel):
    id: str
    kind: str
    name: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class Assertion(StrictModel):
    id: str
    subject_id: str
    predicate: str
    value: Any
    authored_status: AuthoredStatus = AuthoredStatus.UNSPECIFIED
    provenance: ProvenanceRef


class DependencyEdge(StrictModel):
    upstream_id: str
    downstream_id: str
    kind: str = "depends_on"

    @model_validator(mode="after")
    def reject_self_dependency(self) -> "DependencyEdge":
        if self.upstream_id == self.downstream_id:
            raise ValueError("a dependency cannot point to itself")
        return self


class DesignSnapshot(StrictModel):
    id: str
    project_id: str
    adapter_id: str
    source_revisions: tuple[SourceRevision, ...]
    entities: tuple[DesignEntity, ...]
    assertions: tuple[Assertion, ...]
    dependencies: tuple[DependencyEdge, ...] = ()
    capabilities: frozenset[str] = frozenset()
    omissions: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_references(self) -> "DesignSnapshot":
        def require_unique(label: str, values: list[str]) -> None:
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                raise ValueError(f"duplicate {label} IDs: {duplicates}")

        require_unique("source revision", [revision.id for revision in self.source_revisions])
        require_unique("source", [revision.source_id for revision in self.source_revisions])
        require_unique("entity", [entity.id for entity in self.entities])
        require_unique("assertion", [assertion.id for assertion in self.assertions])
        entity_ids = {entity.id for entity in self.entities}
        revision_ids = {revision.id for revision in self.source_revisions}
        missing_subjects = {
            assertion.subject_id
            for assertion in self.assertions
            if assertion.subject_id not in entity_ids
        }
        missing_sources = {
            assertion.provenance.source_revision_id
            for assertion in self.assertions
            if assertion.provenance.source_revision_id not in revision_ids
        }
        if missing_subjects:
            raise ValueError(f"assertions reference missing entities: {sorted(missing_subjects)}")
        if missing_sources:
            raise ValueError(f"assertions reference missing sources: {sorted(missing_sources)}")
        return self


class Intent(StrictModel):
    id: str
    project_id: str
    kind: IntentKind
    original_prompt: str
    scope: tuple[str, ...] = ()
    persistence: Persistence = Persistence.TRANSACTIONAL
    desired_outcome: str | None = None
    exclusions: tuple[str, ...] = ()


class EvidenceLink(StrictModel):
    evidence_id: str
    relation: EvidenceRelation
    rationale: str
    assumptions: tuple[str, ...] = ()


class EvidenceClaim(StrictModel):
    id: str
    statement: str
    scope: tuple[str, ...]
    evidence: tuple[EvidenceLink, ...]
    applicability: str = "current"

    @model_validator(mode="after")
    def require_evidence_or_judgment(self) -> "EvidenceClaim":
        if not self.evidence:
            raise ValueError("a material claim needs evidence or an explicit judgment link")
        return self


class Hypothesis(StrictModel):
    id: str
    statement: str
    scope: tuple[str, ...]
    test_method: str
    supporting_outcomes: tuple[str, ...]
    contradicting_outcomes: tuple[str, ...]
    basis_ids: tuple[str, ...] = ()
    evidence_requirements: tuple[str, ...] = ()
    status: DecisionState = DecisionState.PROPOSED


class ChangeOperation(StrictModel):
    target_id: str
    operation: str
    before: Any = None
    after: Any = None


class ChangeSet(StrictModel):
    id: str
    intent_id: str
    base_snapshot_id: str
    operations: tuple[ChangeOperation, ...]
    rationale_claim_ids: tuple[str, ...]
    validation: tuple[str, ...]
    state: DecisionState = DecisionState.PROPOSED
