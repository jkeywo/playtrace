from playtrace.knowledge import (
    KnowledgeScope,
    LayeredPolicyResolver,
    PolicyClause,
    PolicyLayer,
    PolicySeverity,
    PrecedentCase,
    PrecedentTier,
    ScopedPrecedentRetriever,
)
from playtrace.models import DecisionState


def test_game_policy_can_explicitly_override_shared_policy() -> None:
    shared = PolicyClause(
        id="policy:shared",
        title="Shared",
        statement="Prefer short rounds.",
        severity=PolicySeverity.STRONG_PREFERENCE,
        layer=PolicyLayer.SHARED,
        source="shared.json",
    )
    game = PolicyClause(
        id="policy:game",
        title="Game exception",
        statement="This game uses long deliberative rounds.",
        severity=PolicySeverity.HARD,
        layer=PolicyLayer.GAME,
        overrides=(shared.id,),
        scope=KnowledgeScope(project_ids=("game",)),
        source="game.json",
    )

    resolved = LayeredPolicyResolver().resolve(
        (shared, game),
        project_id="game",
        medium="tabletop",
        capabilities=(),
        systems=(),
    )

    assert resolved == (game,)


def test_precedent_retrieval_preserves_scope_order_and_analogy_label() -> None:
    def case(case_id: str, **scope: object) -> PrecedentCase:
        return PrecedentCase(
            id=case_id,
            title="Escalation pacing",
            snapshot_id="snapshot:1",
            question="How quickly should escalation advance?",
            alternatives=("fast", "slow"),
            criteria=("pacing",),
            decision="slow",
            rationale="Players need time to coordinate.",
            scope=KnowledgeScope(systems=("pacing",), **scope),
            state=DecisionState.ACCEPTED,
            source="case.json",
            project_id="game" if case_id == "case:same" else None,
        )

    matches = ScopedPrecedentRetriever().retrieve(
        (
            case("case:analogy", media=("videogame",)),
            case("case:medium", media=("tabletop",)),
            case("case:same", project_ids=("game",)),
        ),
        project_id="game",
        medium="tabletop",
        capabilities=(),
        systems=("pacing",),
        query="escalation pacing",
    )

    assert [item.tier for item in matches] == [
        PrecedentTier.SAME_GAME,
        PrecedentTier.SAME_MEDIUM_CAPABILITY,
        PrecedentTier.CROSS_MEDIUM_ANALOGY,
    ]
    assert matches[-1].analogy is True
