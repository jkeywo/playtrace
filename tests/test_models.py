from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from playtrace.models import (
    Assertion,
    AuthoredStatus,
    DesignEntity,
    DesignSnapshot,
    EvidenceClaim,
    Intent,
    IntentKind,
    Persistence,
    ProvenanceRef,
    SourceMedium,
    SourceRevision,
)


def test_intent_defaults_to_transactional_persistence() -> None:
    intent = Intent(
        id="intent:1",
        project_id="project:1",
        kind=IntentKind.CHANGE_REQUEST,
        original_prompt="Try a lower movement cap.",
    )

    assert intent.persistence is Persistence.TRANSACTIONAL


def test_core_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Intent(
            id="intent:1",
            project_id="project:1",
            kind=IntentKind.QUESTION,
            original_prompt="Why?",
            typo=True,
        )


def test_material_claim_requires_an_evidence_or_judgment_link() -> None:
    with pytest.raises(ValidationError, match="needs evidence"):
        EvidenceClaim(id="claim:1", statement="This is material.", scope=(), evidence=())


def test_snapshot_rejects_assertion_with_missing_subject() -> None:
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    source = SourceRevision(
        id="source:1@r1",
        source_id="source:1",
        medium=SourceMedium.SYNTHETIC,
        locator="synthetic://source",
        revision="r1",
        observed_at=observed,
    )
    assertion = Assertion(
        id="assertion:1",
        subject_id="entity:missing",
        predicate="does_something",
        value=True,
        authored_status=AuthoredStatus.WORKING,
        provenance=ProvenanceRef(
            source_revision_id="source:1@r1",
            location="rule:1",
            extractor="test",
            extracted_at=observed,
        ),
    )

    with pytest.raises(ValidationError, match="missing entities"):
        DesignSnapshot(
            id="snapshot:1",
            project_id="project:1",
            adapter_id="test",
            source_revisions=(source,),
            entities=(DesignEntity(id="entity:present", kind="rule", name="Present"),),
            assertions=(assertion,),
        )
