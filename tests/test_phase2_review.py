from datetime import UTC, datetime

from playtrace.models import (
    Assertion,
    AuthoredStatus,
    DesignEntity,
    DesignSnapshot,
    Intent,
    IntentKind,
    ProvenanceRef,
    SourceMedium,
    SourceRevision,
)
from playtrace.reviewing import (
    CoverageStatus,
    CriticFinding,
    CriticStance,
    Phase2ReviewEngine,
    find_critic_disagreements,
)


def test_phase2_review_finds_deck_mismatch_and_labels_missing_experience(tmp_path) -> None:
    (tmp_path / "policy").mkdir()
    game = tmp_path / "game"
    policy = game / "design" / "policy"
    policy.mkdir(parents=True)
    (policy / "hard.json").write_text(
        """{
          "id": "game:policy:local-cubes",
          "title": "Local cubes",
          "statement": "Cube meanings remain local to cards.",
          "severity": "hard",
          "layer": "game",
          "scope": {"project_ids": ["game"]},
          "source": "design/policy/hard.json"
        }""",
        encoding="utf-8",
    )
    snapshot = _snapshot()
    intent = Intent(
        id="game:intent:first-loop",
        project_id="game",
        kind=IntentKind.DIAGNOSIS,
        original_prompt="Identify blockers to a traceable first scenario and its pacing.",
        scope=("SCN-001",),
    )

    review = Phase2ReviewEngine(tmp_path, game).review(snapshot, intent)

    assert next(claim for claim in review.claims if claim.id.endswith(":deck-size"))
    assert next(
        finding
        for finding in review.critic_findings
        if finding.id.endswith(":no-experience-evidence")
    ).unsupported_judgment
    assert review.coverage.complete
    assert all(item.status is CoverageStatus.COVERED for item in review.coverage.items)
    assert "critic:pacing" in {route.critic_id for route in review.critic_routes}


def test_current_experiential_evidence_is_claim_linked_and_covered(tmp_path) -> None:
    (tmp_path / "policy").mkdir()
    game = tmp_path / "game"
    evidence = game / "design" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "playtest.json").write_text(
        """{
          "id": "game:evidence:first-trace",
          "kind": "playtest_observation",
          "summary": "The first trace stalled at escalation.",
          "observations": ["Players could not resolve the threshold."],
          "linked_entity_ids": ["SCN-001"],
          "source_revision": "notes@1",
          "location": "session 1, turn 3",
          "captured_by": "facilitator",
          "source": "design/evidence/playtest.json"
        }""",
        encoding="utf-8",
    )
    review = Phase2ReviewEngine(tmp_path, game).review(
        _snapshot(),
        Intent(
            id="game:intent:trace",
            project_id="game",
            kind=IntentKind.DIAGNOSIS,
            original_prompt="Review escalation.",
            scope=("SCN-001",),
        ),
    )

    item = next(
        item
        for item in review.coverage.items
        if item.target_id == "game:evidence:first-trace"
    )
    assert item.status is CoverageStatus.COVERED
    assert review.experiential_evidence[0].id == "game:evidence:first-trace"


def test_critic_disagreement_is_preserved_without_averaging() -> None:
    findings = (
        CriticFinding(
            id="finding:risk",
            critic_id="critic:agency",
            title="Risk",
            statement="This reduces choice.",
            stance=CriticStance.RISK,
            claim_ids=("claim:change",),
            unsupported_judgment=True,
        ),
        CriticFinding(
            id="finding:support",
            critic_id="critic:pacing",
            title="Benefit",
            statement="This shortens turns.",
            stance=CriticStance.SUPPORT,
            claim_ids=("claim:change",),
            unsupported_judgment=True,
        ),
    )

    disagreements = find_critic_disagreements(findings)

    assert disagreements[0].subject_id == "claim:change"
    assert set(disagreements[0].finding_ids) == {"finding:risk", "finding:support"}


def _snapshot() -> DesignSnapshot:
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    provenance = ProvenanceRef(
        source_revision_id="source@1",
        location="fixture",
        extractor="test",
        extracted_at=observed,
    )
    scenario = DesignEntity(
        id="SCN-001",
        kind="scenario",
        name="Flood",
        attributes={"fields": {"Back: Deck Construction": "Prototype target: ~2 resolved cards."}},
    )
    cards = tuple(
        DesignEntity(
            id=f"CARD-{index}",
            kind="scenario_card",
            name=f"Card {index}",
            attributes={"fields": {"Scenario ID": "SCN-001", "Qty": quantity}},
        )
        for index, quantity in ((1, 2), (2, 1))
    )
    assertions = (
        Assertion(
            id="SCN-001:field:back_deck_construction",
            subject_id="SCN-001",
            predicate="back_deck_construction",
            value="Prototype target: ~2 resolved cards.",
            authored_status=AuthoredStatus.WORKING,
            provenance=provenance,
        ),
        Assertion(
            id="CARD-1:field:qty",
            subject_id="CARD-1",
            predicate="qty",
            value=2,
            authored_status=AuthoredStatus.WORKING,
            provenance=provenance,
        ),
        Assertion(
            id="CARD-2:field:qty",
            subject_id="CARD-2",
            predicate="qty",
            value=1,
            authored_status=AuthoredStatus.PLACEHOLDER,
            provenance=provenance,
        ),
    )
    return DesignSnapshot(
        id="game:snapshot:1",
        project_id="game",
        adapter_id="test",
        source_revisions=(
            SourceRevision(
                id="source@1",
                source_id="source",
                medium=SourceMedium.DRIVE,
                locator="fixture",
                revision="1",
                observed_at=observed,
            ),
        ),
        entities=(scenario, *cards),
        assertions=assertions,
        capabilities=frozenset({"controlled_language"}),
    )
