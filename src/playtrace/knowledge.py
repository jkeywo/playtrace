"""Inspectable taste knowledge: policy, precedent, and experiential evidence."""

from __future__ import annotations

import json
import re
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from pydantic import Field, model_validator

from playtrace.models import DecisionState, StrictModel

if TYPE_CHECKING:
    from playtrace.proposals import ProposalRecord


class PolicyLayer(StrEnum):
    SHARED = "shared"
    GAME = "game"
    INTENT = "intent"


class PolicySeverity(StrEnum):
    HARD = "hard"
    STRONG_PREFERENCE = "strong_preference"
    HEURISTIC = "heuristic"


class EvidenceKind(StrEnum):
    CANONICAL_SOURCE = "canonical_source"
    USER_REQUEST = "user_request"
    DESIGN_POLICY = "design_policy"
    APPROVED_PRECEDENT = "approved_precedent"
    PLAYTEST_OBSERVATION = "playtest_observation"
    SIMULATION_RESULT = "simulation_result"
    TELEMETRY_RESULT = "telemetry_result"
    DETERMINISTIC_TEST = "deterministic_test"
    VISUAL_REPLAY = "visual_replay"
    EXTERNAL_RESEARCH = "external_research"
    DESIGNER_JUDGMENT = "designer_judgment"


class EvidenceApplicability(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    WEAK = "weakly_applicable"


class KnowledgeScope(StrictModel):
    project_ids: tuple[str, ...] = ()
    media: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    systems: tuple[str, ...] = ()
    interaction: str | None = None
    time_model: str | None = None

    def applies_to(
        self,
        *,
        project_id: str,
        medium: str,
        capabilities: Iterable[str],
        systems: Iterable[str],
    ) -> bool:
        capability_set = set(capabilities)
        system_set = set(systems)
        return not (
            (self.project_ids and project_id not in self.project_ids)
            or (self.media and medium not in self.media)
            or (self.capabilities and not set(self.capabilities).issubset(capability_set))
            or (self.systems and not set(self.systems).intersection(system_set))
        )


class PolicyClause(StrictModel):
    id: str
    title: str
    statement: str
    severity: PolicySeverity
    layer: PolicyLayer
    scope: KnowledgeScope = Field(default_factory=KnowledgeScope)
    rationale: str | None = None
    overrides: tuple[str, ...] = ()
    source: str


class PrecedentCase(StrictModel):
    id: str
    title: str
    project_id: str | None = None
    snapshot_id: str
    question: str
    relevant_intent_ids: tuple[str, ...] = ()
    policy_ids: tuple[str, ...] = ()
    alternatives: tuple[str, ...]
    criteria: tuple[str, ...]
    evidence_for: tuple[str, ...] = ()
    evidence_against: tuple[str, ...] = ()
    critic_disagreement: tuple[str, ...] = ()
    decision: str
    rationale: str
    consequences: tuple[str, ...] = ()
    scope: KnowledgeScope = Field(default_factory=KnowledgeScope)
    state: DecisionState = DecisionState.PROPOSED
    source: str
    source_proposal_id: str | None = None

    @model_validator(mode="after")
    def require_meaningful_alternatives(self) -> "PrecedentCase":
        if len(self.alternatives) < 2:
            raise ValueError("a precedent case needs at least two alternatives")
        return self


class ExperientialEvidence(StrictModel):
    id: str
    kind: EvidenceKind
    summary: str
    observations: tuple[str, ...]
    linked_entity_ids: tuple[str, ...] = ()
    source_revision: str
    location: str
    captured_by: str
    assumptions: tuple[str, ...] = ()
    dependency_ids: tuple[str, ...] = ()
    applicability: EvidenceApplicability = EvidenceApplicability.CURRENT
    designer_evaluation: str | None = None
    source: str

    @model_validator(mode="after")
    def require_experiential_kind(self) -> "ExperientialEvidence":
        allowed = {
            EvidenceKind.PLAYTEST_OBSERVATION,
            EvidenceKind.VISUAL_REPLAY,
            EvidenceKind.DESIGNER_JUDGMENT,
        }
        if self.kind not in allowed:
            raise ValueError("experiential evidence must be observation, replay, or judgment")
        return self


class KnowledgeBase(StrictModel):
    policies: tuple[PolicyClause, ...] = ()
    precedents: tuple[PrecedentCase, ...] = ()
    experiential_evidence: tuple[ExperientialEvidence, ...] = ()


class PrecedentTier(StrEnum):
    SAME_GAME = "same_game"
    SAME_MEDIUM_CAPABILITY = "same_medium_capability"
    GENERAL = "general"
    CROSS_MEDIUM_ANALOGY = "cross_medium_analogy"


class PrecedentMatch(StrictModel):
    case: PrecedentCase
    tier: PrecedentTier
    analogy: bool = False
    matched_terms: tuple[str, ...] = ()


class LayeredPolicyResolver:
    def resolve(
        self,
        policies: Iterable[PolicyClause],
        *,
        project_id: str,
        medium: str,
        capabilities: Iterable[str],
        systems: Iterable[str],
    ) -> tuple[PolicyClause, ...]:
        order = {PolicyLayer.SHARED: 0, PolicyLayer.GAME: 1, PolicyLayer.INTENT: 2}
        applicable = [
            clause
            for clause in policies
            if clause.scope.applies_to(
                project_id=project_id,
                medium=medium,
                capabilities=capabilities,
                systems=systems,
            )
        ]
        applicable.sort(key=lambda clause: (order[clause.layer], clause.id))
        overridden = {target for clause in applicable for target in clause.overrides}
        return tuple(clause for clause in applicable if clause.id not in overridden)


class ScopedPrecedentRetriever:
    def retrieve(
        self,
        precedents: Iterable[PrecedentCase],
        *,
        project_id: str,
        medium: str,
        capabilities: Iterable[str],
        systems: Iterable[str],
        query: str,
        limit: int = 8,
    ) -> tuple[PrecedentMatch, ...]:
        capability_set = set(capabilities)
        system_set = set(systems)
        query_terms = _terms(query).union(system_set)
        matches: list[tuple[int, int, str, PrecedentMatch]] = []
        for case in precedents:
            if case.state is not DecisionState.ACCEPTED:
                continue
            case_terms = _terms(
                " ".join(
                    (case.title, case.question, case.decision, case.rationale, *case.scope.systems)
                )
            )
            overlap = tuple(sorted(query_terms.intersection(case_terms)))
            scope = case.scope
            if case.project_id == project_id or project_id in scope.project_ids:
                tier, score = PrecedentTier.SAME_GAME, 0
            elif (
                (not scope.media or medium in scope.media)
                and (not scope.capabilities or set(scope.capabilities).intersection(capability_set))
            ):
                if not scope.media and not scope.capabilities and not scope.project_ids:
                    tier, score = PrecedentTier.GENERAL, 2
                else:
                    tier, score = PrecedentTier.SAME_MEDIUM_CAPABILITY, 1
            elif scope.systems and set(scope.systems).intersection(system_set):
                tier, score = PrecedentTier.CROSS_MEDIUM_ANALOGY, 3
            else:
                continue
            if query_terms and not overlap and score != 0:
                continue
            match = PrecedentMatch(
                case=case,
                tier=tier,
                analogy=tier is PrecedentTier.CROSS_MEDIUM_ANALOGY,
                matched_terms=overlap,
            )
            matches.append((score, -len(overlap), case.id, match))
        return tuple(item[3] for item in sorted(matches)[:limit])


def load_knowledge(shared_root: str | Path, game_root: str | Path) -> KnowledgeBase:
    shared = Path(shared_root)
    game = Path(game_root)
    policies = _load_records(shared / "policy", PolicyClause) + _load_records(
        game / "design" / "policy", PolicyClause
    )
    precedents = _load_records(shared / "cases", PrecedentCase) + _load_records(
        game / "design" / "cases", PrecedentCase
    )
    evidence = _load_records(
        game / "design" / "evidence", ExperientialEvidence
    )
    return KnowledgeBase(
        policies=tuple(policies),
        precedents=tuple(precedents),
        experiential_evidence=tuple(evidence),
    )


def promote_applied_proposal(
    record: "ProposalRecord",
    *,
    game_root: str | Path,
    decision: str,
    rationale: str,
    consequences: Iterable[str] = (),
) -> tuple[PrecedentCase, Path]:
    """Explicitly write one applied decision into its owning game's case library."""

    from playtrace.proposals import AttemptStatus, ProposalState

    proposal = record.proposal
    if proposal.state is not ProposalState.APPLIED:
        raise ValueError("only an applied proposal may become precedent")
    if not any(
        attempt.status is AttemptStatus.APPLIED and not attempt.dry_run
        for attempt in record.attempts
    ):
        raise ValueError(
            "precedent promotion requires a successful canonical application attempt"
        )
    if proposal.basis_digest != proposal.approval_digest():
        raise ValueError("proposal content does not match its approved basis digest")
    root = Path(game_root).resolve()
    destination = (root / "design" / "cases" / f"{_safe_name(proposal.id)}.json").resolve()
    if root not in destination.parents:
        raise ValueError("precedent destination must remain inside the game repository")
    precedent = PrecedentCase(
        id=f"precedent:{proposal.id}",
        title=proposal.title,
        project_id=proposal.project_id,
        snapshot_id=proposal.basis.snapshot.id,
        question=proposal.question,
        relevant_intent_ids=(proposal.change_set.intent_id,),
        policy_ids=proposal.policy_ids,
        alternatives=proposal.alternatives,
        criteria=proposal.criteria,
        evidence_for=proposal.evidence_ids,
        decision=decision,
        rationale=rationale,
        consequences=tuple(consequences),
        scope=KnowledgeScope(project_ids=(proposal.project_id,)),
        state=DecisionState.ACCEPTED,
        source=str(destination),
        source_proposal_id=proposal.id,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8") as output:
            output.write(precedent.model_dump_json(indent=2) + "\n")
    except FileExistsError as error:
        raise ValueError(f"precedent already exists: {destination}") from error
    return precedent, destination


def _safe_name(value: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.")
    if not name:
        raise ValueError("proposal ID cannot produce an empty precedent filename")
    return name


def _load_records(root: Path, model: type[StrictModel]) -> list[Any]:
    if not root.exists():
        return []
    records: list[Any] = []
    for path in sorted(root.rglob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            item.setdefault("source", str(path))
            records.append(model.model_validate(item))
    return records


def _terms(value: str) -> set[str]:
    return {
        item
        for item in re.findall(r"[a-z0-9][a-z0-9_-]+", value.lower())
        if len(item) > 2
    }
