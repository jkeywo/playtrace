"""Project adapter boundary and source-neutral synthetic acceptance adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from playtrace.models import (
    Assertion,
    AuthoredStatus,
    DependencyEdge,
    DesignEntity,
    DesignSnapshot,
    ProvenanceRef,
    SourceMedium,
    SourceRevision,
)


class ProjectAdapter(Protocol):
    adapter_id: str

    def snapshot(self) -> DesignSnapshot:
        """Project canonical sources into a shared DesignSnapshot."""


class SyntheticProjectAdapter:
    """Produces the same semantic game model through any declared medium.

    This is an acceptance fixture for the shared-kernel boundary, not a model of
    a real game and not a substitute for a project-owned integration fixture.
    """

    def __init__(self, medium: SourceMedium) -> None:
        self.medium = medium
        self.adapter_id = f"synthetic-{medium.value}"

    def snapshot(self) -> DesignSnapshot:
        observed = datetime(2026, 1, 1, tzinfo=UTC)
        source = SourceRevision(
            id="synthetic:rules@r1",
            source_id="synthetic:rules",
            medium=self.medium,
            locator=f"{self.medium.value}://synthetic/rules",
            revision="r1",
            observed_at=observed,
            content_digest="synthetic-rules-r1",
        )
        provenance = ProvenanceRef(
            source_revision_id=source.id,
            location="rule:pressure-cadence",
            extractor=self.adapter_id,
            extracted_at=observed,
        )
        entities = (
            DesignEntity(
                id="synthetic:game",
                kind="game",
                name="Harbour Signal",
                attributes={"cooperative": True},
            ),
            DesignEntity(
                id="synthetic:rule:pressure",
                kind="rule",
                name="Pressure cadence",
            ),
            DesignEntity(
                id="synthetic:question:scaling",
                kind="design_question",
                name="Player-count scaling",
            ),
        )
        assertions = (
            Assertion(
                id="synthetic:assertion:players",
                subject_id="synthetic:game",
                predicate="supports_player_count",
                value={"minimum": 1, "maximum": 4},
                authored_status=AuthoredStatus.CONFIRMED,
                provenance=provenance,
            ),
            Assertion(
                id="synthetic:assertion:cadence",
                subject_id="synthetic:rule:pressure",
                predicate="resolves_pressure_after_each_player_turn",
                value=True,
                authored_status=AuthoredStatus.WORKING,
                provenance=provenance,
            ),
            Assertion(
                id="synthetic:assertion:scaling-question",
                subject_id="synthetic:question:scaling",
                predicate="asks",
                value="Does pressure need to scale by player count?",
                authored_status=AuthoredStatus.OPEN_QUESTION,
                provenance=provenance,
            ),
        )
        return DesignSnapshot(
            id=f"synthetic:{self.medium.value}:r1",
            project_id="synthetic:harbour-signal",
            adapter_id=self.adapter_id,
            source_revisions=(source,),
            entities=entities,
            assertions=assertions,
            dependencies=(
                DependencyEdge(
                    upstream_id="synthetic:rule:pressure",
                    downstream_id="synthetic:question:scaling",
                ),
            ),
            capabilities=frozenset({"turn_based", "structured_rules"}),
            omissions=("No balance or experiential evidence is represented.",),
            created_at=observed,
        )
