"""Deterministic analysis shared by every source adapter."""

from __future__ import annotations

from collections import Counter

from pydantic import Field

from playtrace.canonical import canonical_digest
from playtrace.dependencies import DependencyGraph
from playtrace.models import AuthoredStatus, DesignSnapshot, StrictModel


class Finding(StrictModel):
    code: str
    severity: str
    message: str
    entity_ids: tuple[str, ...] = ()


class AnalysisReport(StrictModel):
    snapshot_id: str
    project_id: str
    semantic_digest: str
    entity_count: int
    assertion_count: int
    status_counts: dict[str, int]
    findings: tuple[Finding, ...]
    omissions: tuple[str, ...]
    affected_by_working_rules: tuple[str, ...] = Field(default_factory=tuple)

    def source_neutral_result(self) -> dict[str, object]:
        """Comparable result with transport/source identity deliberately omitted."""
        return {
            "project_id": self.project_id,
            "semantic_digest": self.semantic_digest,
            "entity_count": self.entity_count,
            "assertion_count": self.assertion_count,
            "status_counts": self.status_counts,
            "findings": [finding.model_dump(mode="json") for finding in self.findings],
            "omissions": self.omissions,
            "affected_by_working_rules": self.affected_by_working_rules,
        }


class SnapshotAnalyzer:
    """Run deterministic checks against the semantic snapshot only."""

    def analyze(self, snapshot: DesignSnapshot) -> AnalysisReport:
        statuses = Counter(assertion.authored_status.value for assertion in snapshot.assertions)
        findings: list[Finding] = []

        open_questions = tuple(
            assertion.subject_id
            for assertion in snapshot.assertions
            if assertion.authored_status is AuthoredStatus.OPEN_QUESTION
        )
        if open_questions:
            findings.append(
                Finding(
                    code="open-questions-present",
                    severity="info",
                    message=f"Snapshot contains {len(open_questions)} authored open question(s).",
                    entity_ids=tuple(sorted(set(open_questions))),
                )
            )

        placeholders = tuple(
            assertion.subject_id
            for assertion in snapshot.assertions
            if assertion.authored_status is AuthoredStatus.PLACEHOLDER
        )
        if placeholders:
            findings.append(
                Finding(
                    code="placeholder-mechanics-present",
                    severity="warning",
                    message=f"Snapshot contains {len(placeholders)} placeholder assertion(s).",
                    entity_ids=tuple(sorted(set(placeholders))),
                )
            )

        working_subjects = {
            assertion.subject_id
            for assertion in snapshot.assertions
            if assertion.authored_status is AuthoredStatus.WORKING
        }
        affected = DependencyGraph(snapshot.dependencies).affected_by(working_subjects)

        return AnalysisReport(
            snapshot_id=snapshot.id,
            project_id=snapshot.project_id,
            semantic_digest=self._semantic_digest(snapshot),
            entity_count=len(snapshot.entities),
            assertion_count=len(snapshot.assertions),
            status_counts=dict(sorted(statuses.items())),
            findings=tuple(findings),
            omissions=snapshot.omissions,
            affected_by_working_rules=affected,
        )

    @staticmethod
    def _semantic_digest(snapshot: DesignSnapshot) -> str:
        """Digest design meaning, excluding adapters, source transport and timestamps."""
        payload = {
            "project_id": snapshot.project_id,
            "entities": [
                entity.model_dump(mode="json")
                for entity in sorted(snapshot.entities, key=lambda item: item.id)
            ],
            "assertions": [
                {
                    "id": assertion.id,
                    "subject_id": assertion.subject_id,
                    "predicate": assertion.predicate,
                    "value": assertion.value,
                    "authored_status": assertion.authored_status.value,
                }
                for assertion in sorted(snapshot.assertions, key=lambda item: item.id)
            ],
            "dependencies": [
                edge.model_dump(mode="json")
                for edge in sorted(
                    snapshot.dependencies,
                    key=lambda item: (item.upstream_id, item.downstream_id, item.kind),
                )
            ],
            "capabilities": sorted(snapshot.capabilities),
            "omissions": snapshot.omissions,
        }
        return canonical_digest(payload, domain="analysis-semantic")
