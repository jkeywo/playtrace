"""Explicit workflow graph for bounded design reasoning."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field

from playtrace.models import StrictModel


class WorkflowStage(StrEnum):
    SNAPSHOT = "snapshot"
    INTENT = "intent"
    SCOPE = "scope"
    COVERAGE = "coverage"
    HYPOTHESES = "hypotheses"
    EVIDENCE = "evidence"
    CRITIQUE = "critique"
    CHANGESET = "changeset"
    DESIGN_REVIEW = "design_review"
    APPLY = "apply"
    VERIFY = "verify"
    COMPLETE = "complete"


WORKFLOW_GRAPH: dict[WorkflowStage, frozenset[WorkflowStage]] = {
    WorkflowStage.SNAPSHOT: frozenset({WorkflowStage.INTENT}),
    WorkflowStage.INTENT: frozenset({WorkflowStage.SCOPE}),
    WorkflowStage.SCOPE: frozenset({WorkflowStage.COVERAGE}),
    WorkflowStage.COVERAGE: frozenset({WorkflowStage.HYPOTHESES, WorkflowStage.EVIDENCE}),
    WorkflowStage.HYPOTHESES: frozenset({WorkflowStage.EVIDENCE}),
    WorkflowStage.EVIDENCE: frozenset({WorkflowStage.CRITIQUE, WorkflowStage.COMPLETE}),
    WorkflowStage.CRITIQUE: frozenset({WorkflowStage.CHANGESET, WorkflowStage.COMPLETE}),
    WorkflowStage.CHANGESET: frozenset({WorkflowStage.DESIGN_REVIEW}),
    WorkflowStage.DESIGN_REVIEW: frozenset({WorkflowStage.APPLY, WorkflowStage.COMPLETE}),
    WorkflowStage.APPLY: frozenset({WorkflowStage.VERIFY}),
    WorkflowStage.VERIFY: frozenset({WorkflowStage.COMPLETE, WorkflowStage.APPLY}),
    WorkflowStage.COMPLETE: frozenset(),
}


class WorkflowEvent(StrictModel):
    stage: WorkflowStage
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    note: str | None = None


class WorkflowRun(StrictModel):
    id: str
    intent_id: str
    snapshot_id: str
    stage: WorkflowStage = WorkflowStage.SNAPSHOT
    history: tuple[WorkflowEvent, ...] = ()

    def transition(self, target: WorkflowStage, note: str | None = None) -> "WorkflowRun":
        allowed = WORKFLOW_GRAPH[self.stage]
        if target not in allowed:
            raise ValueError(f"invalid workflow transition: {self.stage} -> {target}")
        event = WorkflowEvent(stage=target, note=note)
        return self.model_copy(update={"stage": target, "history": self.history + (event,)})

