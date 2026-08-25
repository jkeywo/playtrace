"""Phase 2 deterministic coverage, critic routing, and review assembly."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from pydantic import model_validator

from playtrace.knowledge import (
    EvidenceApplicability,
    ExperientialEvidence,
    KnowledgeBase,
    LayeredPolicyResolver,
    PolicyClause,
    PolicySeverity,
    PrecedentMatch,
    ScopedPrecedentRetriever,
    load_knowledge,
)
from playtrace.evidence import ExperimentApplicability, ExperimentEvidence
from playtrace.models import (
    AuthoredStatus,
    DesignSnapshot,
    EvidenceClaim,
    EvidenceLink,
    EvidenceRelation,
    Hypothesis,
    Intent,
    StrictModel,
)


class CriticStance(StrEnum):
    SUPPORT = "support"
    RISK = "risk"
    OPPORTUNITY = "opportunity"
    MIXED = "mixed"


class ReviewDecisionState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REVISE = "revise"


class HumanReviewDecision(StrictModel):
    state: ReviewDecisionState = ReviewDecisionState.PENDING
    rationale: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None

    @model_validator(mode="after")
    def require_decision_details(self) -> "HumanReviewDecision":
        if self.state is not ReviewDecisionState.PENDING:
            if not self.rationale or not self.decided_by or not self.decided_at:
                raise ValueError("a completed review decision needs rationale, actor, and time")
        return self

    @classmethod
    def decided(
        cls, state: ReviewDecisionState, *, rationale: str, decided_by: str
    ) -> "HumanReviewDecision":
        if state is ReviewDecisionState.PENDING:
            return cls()
        return cls(
            state=state,
            rationale=rationale,
            decided_by=decided_by,
            decided_at=datetime.now(UTC),
        )


class CoverageStatus(StrEnum):
    COVERED = "covered"
    MISSING = "missing"


class CriticDefinition(StrictModel):
    id: str
    title: str
    mandatory: bool = False
    trigger_terms: frozenset[str] = frozenset()
    trigger_systems: frozenset[str] = frozenset()


class CriticRoute(StrictModel):
    critic_id: str
    reason: str
    mandatory: bool


class CriticFinding(StrictModel):
    id: str
    critic_id: str
    title: str
    statement: str
    stance: CriticStance
    scope: tuple[str, ...] = ()
    basis_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    unsupported_judgment: bool = False

    @model_validator(mode="after")
    def label_unsupported_judgment(self) -> "CriticFinding":
        if not self.basis_ids and not self.unsupported_judgment:
            raise ValueError("a critic finding needs a basis or explicit unsupported judgment")
        return self


class CoverageItem(StrictModel):
    target_id: str
    target_kind: str
    reason: str
    status: CoverageStatus
    covered_by: tuple[str, ...] = ()


class CoverageReport(StrictModel):
    items: tuple[CoverageItem, ...]
    complete: bool

    @model_validator(mode="after")
    def complete_matches_items(self) -> "CoverageReport":
        actual = all(item.status is CoverageStatus.COVERED for item in self.items)
        if self.complete != actual:
            raise ValueError("coverage completeness does not match its items")
        return self


class CriticDisagreement(StrictModel):
    subject_id: str
    finding_ids: tuple[str, ...]
    summary: str


class DesignReview(StrictModel):
    id: str
    project_id: str
    snapshot_id: str
    intent: Intent
    policies: tuple[PolicyClause, ...]
    precedents: tuple[PrecedentMatch, ...]
    experiential_evidence: tuple[ExperientialEvidence, ...]
    experiment_evidence: tuple[ExperimentEvidence, ...] = ()
    hypotheses: tuple[Hypothesis, ...]
    claims: tuple[EvidenceClaim, ...]
    critic_routes: tuple[CriticRoute, ...]
    critic_findings: tuple[CriticFinding, ...]
    disagreements: tuple[CriticDisagreement, ...]
    coverage: CoverageReport
    alternatives: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    decision: HumanReviewDecision = HumanReviewDecision()


CORE_CRITICS = (
    CriticDefinition(id="critic:coherence", title="Coherence and consistency", mandatory=True),
    CriticDefinition(
        id="critic:intended-player-experience",
        title="Intended player experience",
        mandatory=True,
    ),
    CriticDefinition(
        id="critic:evidence-provenance", title="Evidence and provenance", mandatory=True
    ),
)

SPECIALIST_CRITICS = (
    CriticDefinition(
        id="critic:agency",
        title="Decision quality and player agency",
        trigger_terms=frozenset({"action", "assist", "choose", "choice", "player"}),
    ),
    CriticDefinition(
        id="critic:complexity",
        title="Complexity and bookkeeping",
        trigger_terms=frozenset({"cube", "threshold", "keyword", "status"}),
    ),
    CriticDefinition(
        id="critic:pacing",
        title="Pacing and downtime",
        trigger_terms=frozenset({"turn", "scenario", "deck", "escalation", "pacing"}),
    ),
    CriticDefinition(
        id="critic:balance",
        title="Balance and variance",
        trigger_terms=frozenset({"ap", "qty", "damage", "balance", "variance"}),
    ),
    CriticDefinition(
        id="critic:social",
        title="Social and coordination dynamics",
        trigger_terms=frozenset({"assist", "cooperative", "people", "coordination"}),
    ),
    CriticDefinition(
        id="critic:accessibility",
        title="Accessibility and readability",
        trigger_terms=frozenset({"icon", "keyword", "readability", "reference"}),
    ),
    CriticDefinition(
        id="critic:exploit",
        title="Exploit and dominant strategy",
        trigger_terms=frozenset({"free", "immediately", "prevent", "ignore"}),
    ),
    CriticDefinition(
        id="critic:onboarding",
        title="Onboarding",
        trigger_terms=frozenset({"setup", "rulebook", "reference"}),
    ),
    CriticDefinition(
        id="critic:content-repetition",
        title="Content repetition",
        trigger_terms=frozenset({"deck", "qty", "repeated"}),
    ),
)


class CriticRouter:
    def route(
        self, snapshot: DesignSnapshot, intent: Intent
    ) -> tuple[CriticRoute, ...]:
        scoped_ids = set(intent.scope)
        entities = [
            entity
            for entity in snapshot.entities
            if not scoped_ids or entity.id in scoped_ids
        ]
        assertions = [
            assertion
            for assertion in snapshot.assertions
            if not scoped_ids or assertion.subject_id in scoped_ids
        ]
        corpus = " ".join(
            [intent.original_prompt, *intent.scope]
            + [entity.name for entity in entities]
            + [str(assertion.value) for assertion in assertions]
        ).lower()
        terms = set(re.findall(r"[a-z][a-z0-9_-]+", corpus))
        routes = [
            CriticRoute(
                critic_id=critic.id,
                reason="mandatory Phase 2 core critic",
                mandatory=True,
            )
            for critic in CORE_CRITICS
        ]
        for critic in SPECIALIST_CRITICS:
            matched = tuple(sorted(critic.trigger_terms.intersection(terms)))
            if matched:
                routes.append(
                    CriticRoute(
                        critic_id=critic.id,
                        reason=f"matched project terms: {', '.join(matched)}",
                        mandatory=False,
                    )
                )
        return tuple(routes)


class MandatoryCoverageEngine:
    def validate(
        self,
        *,
        intent: Intent,
        snapshot: DesignSnapshot,
        policies: Iterable[PolicyClause],
        evidence: Iterable[ExperientialEvidence],
        claims: Iterable[EvidenceClaim],
        hypotheses: Iterable[Hypothesis],
        findings: Iterable[CriticFinding],
        experiment_evidence: Iterable[ExperimentEvidence] = (),
    ) -> CoverageReport:
        references: dict[str, set[str]] = defaultdict(set)
        for claim in claims:
            if ":claim:policy:" not in claim.id:
                for target in claim.scope:
                    references[target].add(claim.id)
            for link in claim.evidence:
                references[link.evidence_id].add(claim.id)
        for hypothesis in hypotheses:
            for target in (*hypothesis.scope, *hypothesis.basis_ids):
                references[target].add(hypothesis.id)
        for finding in findings:
            for target in (*finding.scope, *finding.basis_ids):
                references[target].add(finding.id)

        targets: list[tuple[str, str, str]] = []
        targets.extend(
            (policy.id, "hard_policy", "applicable hard policy must be examined")
            for policy in policies
            if policy.severity is PolicySeverity.HARD
        )
        targets.extend(
            (
                item.id,
                "current_experiment_evidence",
                "current hypothesis-linked experiment evidence must be examined",
            )
            for item in experiment_evidence
            if item.applicability is ExperimentApplicability.CURRENT
        )
        entity_ids = {entity.id for entity in snapshot.entities}
        targets.extend(
            (target, "intent_scope", "explicit intent scope must be examined")
            for target in intent.scope
            if target in entity_ids
        )
        targets.extend(
            (item.id, "current_experiential_evidence", "current scoped evidence must be examined")
            for item in evidence
            if item.applicability is EvidenceApplicability.CURRENT
            and (not intent.scope or set(item.linked_entity_ids).intersection(intent.scope))
        )
        items = tuple(
            CoverageItem(
                target_id=target_id,
                target_kind=kind,
                reason=reason,
                status=(
                    CoverageStatus.COVERED
                    if references.get(target_id)
                    else CoverageStatus.MISSING
                ),
                covered_by=tuple(sorted(references.get(target_id, set()))),
            )
            for target_id, kind, reason in targets
        )
        return CoverageReport(
            items=items,
            complete=all(item.status is CoverageStatus.COVERED for item in items),
        )


class Phase2ReviewEngine:
    """Build an inspectable review without inventing playtest evidence."""

    def __init__(self, shared_root: str | Path, game_root: str | Path) -> None:
        self.shared_root = Path(shared_root)
        self.game_root = Path(game_root)

    def review(
        self,
        snapshot: DesignSnapshot,
        intent: Intent,
        *,
        experiment_evidence: Iterable[ExperimentEvidence] = (),
        experiment_hypotheses: Iterable[Hypothesis] = (),
    ) -> DesignReview:
        experiment_evidence = tuple(experiment_evidence)
        experiment_hypotheses = tuple(experiment_hypotheses)
        included_hypothesis_ids = {item.id for item in experiment_hypotheses}
        missing_links = sorted(
            {
                item.hypothesis_link.hypothesis_id
                for item in experiment_evidence
                if item.hypothesis_link.hypothesis_id not in included_hypothesis_ids
            }
        )
        if missing_links:
            raise ValueError(
                "experiment evidence links to hypotheses not included in the review: "
                + ", ".join(missing_links)
            )
        knowledge = load_knowledge(self.shared_root, self.game_root)
        systems = _systems(snapshot, intent)
        policies = LayeredPolicyResolver().resolve(
            knowledge.policies,
            project_id=snapshot.project_id,
            medium="tabletop",
            capabilities=snapshot.capabilities,
            systems=systems,
        )
        precedents = ScopedPrecedentRetriever().retrieve(
            knowledge.precedents,
            project_id=snapshot.project_id,
            medium="tabletop",
            capabilities=snapshot.capabilities,
            systems=systems,
            query=intent.original_prompt,
        )
        evidence = tuple(
            item
            for item in knowledge.experiential_evidence
            if item.applicability is EvidenceApplicability.CURRENT
            and (
                not intent.scope
                or not item.linked_entity_ids
                or set(item.linked_entity_ids).intersection(intent.scope)
            )
        )
        routes = CriticRouter().route(snapshot, intent)
        assessed_experiments = tuple(
            item.assess(snapshot) for item in experiment_evidence
        )
        claims = self._claims(snapshot, intent, policies, evidence)
        experiment_claims = tuple(item.to_claim() for item in assessed_experiments)
        claims = tuple({item.id: item for item in (*claims, *experiment_claims)}.values())
        generated_hypotheses = self._hypotheses(snapshot, claims)
        hypotheses = tuple(
            {
                item.id: item
                for item in (*generated_hypotheses, *tuple(experiment_hypotheses))
            }.values()
        )
        findings = self._findings(snapshot, routes, claims, evidence)
        coverage = MandatoryCoverageEngine().validate(
            intent=intent,
            snapshot=snapshot,
            policies=policies,
            evidence=evidence,
            claims=claims,
            hypotheses=hypotheses,
            findings=findings,
            experiment_evidence=assessed_experiments,
        )
        provenance = CriticFinding(
            id=f"{snapshot.project_id}:finding:coverage",
            critic_id="critic:evidence-provenance",
            title="Mandatory material was checked",
            statement=(
                "All mandatory policy, intent, and current evidence targets are covered."
                if coverage.complete
                else "The review has uncovered mandatory coverage gaps."
            ),
            stance=CriticStance.SUPPORT if coverage.complete else CriticStance.RISK,
            basis_ids=tuple(item.target_id for item in coverage.items),
            unsupported_judgment=not coverage.items,
        )
        findings = findings + (provenance,)
        disagreements = find_critic_disagreements(findings)
        return DesignReview(
            id=f"{snapshot.project_id}:review:{snapshot.id.rsplit(':', 1)[-1]}",
            project_id=snapshot.project_id,
            snapshot_id=snapshot.id,
            intent=intent,
            policies=policies,
            precedents=precedents,
            experiential_evidence=evidence,
            experiment_evidence=assessed_experiments,
            hypotheses=hypotheses,
            claims=claims,
            critic_routes=routes,
            critic_findings=findings,
            disagreements=disagreements,
            coverage=coverage,
            alternatives=(
                "Resolve the current rule gaps before changing card quantities.",
                "Run a deliberately rough first play trace and use observed blockers to order the rules work.",
                "Reduce the scenario deck toward its stated target before testing pacing.",
            ),
            limitations=(
                "No playtest observation is inferred from authored rules or card content.",
                "Deterministic critics identify inspectable structural risks, not subjective fun.",
                "Precedent absence remains visible rather than being filled with an unapproved analogy.",
            ),
        )

    @staticmethod
    def _claims(
        snapshot: DesignSnapshot,
        intent: Intent,
        policies: tuple[PolicyClause, ...],
        evidence: tuple[ExperientialEvidence, ...],
    ) -> tuple[EvidenceClaim, ...]:
        claims: list[EvidenceClaim] = []
        affected = set(intent.scope)
        placeholders = tuple(
            assertion
            for assertion in snapshot.assertions
            if (not affected or assertion.subject_id in affected)
            and (
                assertion.authored_status is AuthoredStatus.PLACEHOLDER
                or "PLACEHOLDER" in str(assertion.value).upper()
            )
        )
        if placeholders:
            subjects = tuple(sorted({item.subject_id for item in placeholders}))
            claims.append(
                EvidenceClaim(
                    id=f"{snapshot.project_id}:claim:placeholder-load",
                    statement=f"The affected scenario contains {len(placeholders)} assertions across {len(subjects)} entities that are marked PLACEHOLDER or contain placeholder mechanics, so a complete trace still requires assumptions.",
                    scope=subjects,
                    evidence=tuple(
                        EvidenceLink(
                            evidence_id=item.id,
                            relation=EvidenceRelation.SUPPORTS,
                            rationale="The canonical source marks this assertion PLACEHOLDER.",
                        )
                        for item in placeholders
                    ),
                )
            )
        deck_claim = _deck_size_claim(snapshot)
        if deck_claim is not None:
            claims.append(deck_claim)
        for policy in policies:
            if policy.severity is PolicySeverity.HARD:
                claims.append(
                    EvidenceClaim(
                        id=f"{snapshot.project_id}:claim:policy:{policy.id.split(':')[-1]}",
                        statement=f"This review must preserve the hard policy: {policy.statement}",
                        scope=intent.scope,
                        evidence=(
                            EvidenceLink(
                                evidence_id=policy.id,
                                relation=EvidenceRelation.CONTEXT,
                                rationale=f"Mandatory {policy.layer.value} policy coverage.",
                            ),
                        ),
                    )
                )
        for item in evidence:
            if not intent.scope or set(item.linked_entity_ids).intersection(intent.scope):
                claims.append(
                    EvidenceClaim(
                        id=f"{snapshot.project_id}:claim:evidence:{item.id.split(':')[-1]}",
                        statement=item.summary,
                        scope=item.linked_entity_ids,
                        evidence=(
                            EvidenceLink(
                                evidence_id=item.id,
                                relation=EvidenceRelation.CONTEXT,
                                rationale="Current experiential evidence in the affected scope.",
                                assumptions=item.assumptions,
                            ),
                        ),
                    )
                )
        return tuple(claims)

    @staticmethod
    def _hypotheses(
        snapshot: DesignSnapshot, claims: tuple[EvidenceClaim, ...]
    ) -> tuple[Hypothesis, ...]:
        hypotheses: list[Hypothesis] = []
        deck = next((claim for claim in claims if claim.id.endswith(":deck-size")), None)
        if deck:
            hypotheses.append(
                Hypothesis(
                    id=f"{snapshot.project_id}:hypothesis:deck-pacing",
                    statement="A 16-card scenario deck produces a longer and more escalation-heavy first play trace than the stated approximately 12-card target.",
                    scope=deck.scope,
                    test_method="Run otherwise identical scripted play traces with the current 16-card deck and a reviewed 12-card deck; compare turns, escalation steps, and unresolved tableau load.",
                    supporting_outcomes=(
                        "The 16-card configuration takes more turns or reaches more escalation steps in most traces.",
                    ),
                    contradicting_outcomes=(
                        "Trace length and escalation exposure remain materially equivalent.",
                    ),
                    basis_ids=(deck.id,),
                    evidence_requirements=("simulation_result", "playtest_observation"),
                )
            )
        return tuple(hypotheses)

    @staticmethod
    def _findings(
        snapshot: DesignSnapshot,
        routes: tuple[CriticRoute, ...],
        claims: tuple[EvidenceClaim, ...],
        evidence: tuple[ExperientialEvidence, ...],
    ) -> tuple[CriticFinding, ...]:
        selected = {route.critic_id for route in routes}
        findings: list[CriticFinding] = []
        placeholder_claim = next(
            (claim for claim in claims if claim.id.endswith(":placeholder-load")), None
        )
        if placeholder_claim:
            findings.append(
                CriticFinding(
                    id=f"{snapshot.project_id}:finding:incomplete-loop",
                    critic_id="critic:coherence",
                    title="The first playable loop is not fully executable",
                    statement="Placeholder mechanics occur across escalation and card thresholds; a trace must stop or make assumptions at those points.",
                    stance=CriticStance.RISK,
                    scope=placeholder_claim.scope,
                    basis_ids=(placeholder_claim.id,),
                    claim_ids=(placeholder_claim.id,),
                )
            )
        if not evidence:
            findings.append(
                CriticFinding(
                    id=f"{snapshot.project_id}:finding:no-experience-evidence",
                    critic_id="critic:intended-player-experience",
                    title="Player experience is currently unobserved",
                    statement="The sources describe intended play, but no captured playtest observation, quote, or annotated trace is available to establish how it feels in play.",
                    stance=CriticStance.RISK,
                    unsupported_judgment=True,
                )
            )
        deck = next((claim for claim in claims if claim.id.endswith(":deck-size")), None)
        if deck and "critic:pacing" in selected:
            findings.append(
                CriticFinding(
                    id=f"{snapshot.project_id}:finding:deck-target-mismatch",
                    critic_id="critic:pacing",
                    title="Deck construction exceeds the stated prototype target",
                    statement=deck.statement,
                    stance=CriticStance.RISK,
                    scope=deck.scope,
                    basis_ids=(deck.id,),
                    claim_ids=(deck.id,),
                )
            )
        if placeholder_claim and "critic:complexity" in selected:
            findings.append(
                CriticFinding(
                    id=f"{snapshot.project_id}:finding:bookkeeping-unknowns",
                    critic_id="critic:complexity",
                    title="Threshold bookkeeping cannot yet be evaluated",
                    statement="Several threshold and escalation effects are placeholders, so their cognitive and physical bookkeeping cost is presently unknown.",
                    stance=CriticStance.MIXED,
                    scope=placeholder_claim.scope,
                    basis_ids=(placeholder_claim.id,),
                    claim_ids=(placeholder_claim.id,),
                )
            )
        return tuple(findings)


def _deck_size_claim(snapshot: DesignSnapshot) -> EvidenceClaim | None:
    scenario_targets: dict[str, tuple[int, str]] = {}
    for assertion in snapshot.assertions:
        if assertion.predicate != "back_deck_construction":
            continue
        match = re.search(r"target:\s*~?(\d+)", str(assertion.value), re.IGNORECASE)
        if match:
            scenario_targets[assertion.subject_id] = (int(match.group(1)), assertion.id)
    quantities: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    entities = {entity.id: entity for entity in snapshot.entities}
    for entity in snapshot.entities:
        if entity.kind != "scenario_card":
            continue
        fields = entity.attributes.get("fields", {})
        scenario_id = str(fields.get("Scenario ID", ""))
        try:
            quantity = int(fields.get("Qty", 0))
        except (TypeError, ValueError):
            continue
        quantities[scenario_id].append((quantity, f"{entity.id}:field:qty", entity.id))
    for scenario_id, (target, target_assertion) in sorted(scenario_targets.items()):
        items = quantities.get(scenario_id, [])
        total = sum(item[0] for item in items)
        if items and total != target:
            return EvidenceClaim(
                id=f"{snapshot.project_id}:claim:deck-size",
                statement=f"{entities[scenario_id].name} currently constructs a {total}-card scenario deck against its stated prototype target of approximately {target} resolved cards.",
                scope=(scenario_id, *(item[2] for item in items)),
                evidence=(
                    EvidenceLink(
                        evidence_id=target_assertion,
                        relation=EvidenceRelation.CONTEXT,
                        rationale="The scenario source states the prototype deck target.",
                    ),
                    *(
                        EvidenceLink(
                            evidence_id=item[1],
                            relation=EvidenceRelation.SUPPORTS,
                            rationale=f"This card contributes quantity {item[0]} to the current deck.",
                        )
                        for item in items
                    ),
                ),
            )
    return None


def _systems(snapshot: DesignSnapshot, intent: Intent) -> set[str]:
    systems = set(re.findall(r"[a-z][a-z0-9_-]+", intent.original_prompt.lower()))
    scoped = set(intent.scope)
    for entity in snapshot.entities:
        if not scoped or entity.id in scoped:
            systems.add(entity.kind)
            systems.update(
                re.findall(r"[a-z][a-z0-9_-]+", " ".join(entity.attributes.keys()).lower())
            )
    return systems


def find_critic_disagreements(
    findings: tuple[CriticFinding, ...]
) -> tuple[CriticDisagreement, ...]:
    by_claim: dict[str, list[CriticFinding]] = defaultdict(list)
    for finding in findings:
        for claim_id in finding.claim_ids:
            by_claim[claim_id].append(finding)
    disagreements: list[CriticDisagreement] = []
    for claim_id, items in by_claim.items():
        stances = {item.stance for item in items}
        if CriticStance.SUPPORT in stances and CriticStance.RISK in stances:
            disagreements.append(
                CriticDisagreement(
                    subject_id=claim_id,
                    finding_ids=tuple(item.id for item in items),
                    summary="Critics reached opposing conclusions; no aggregate score was applied.",
                )
            )
    return tuple(disagreements)
