"""Narrow reusable executor for exact tabletop cube effects."""

from __future__ import annotations

from typing import Protocol

from pydantic import model_validator

from playtrace.cnl import RuleIR, RuleKind
from playtrace.models import StrictModel
from playtrace.simulation import BlockerKind, SimulationBlocker


class RuleExecutionState(StrictModel):
    tableau: dict[str, int]

    @model_validator(mode="after")
    def require_nonnegative_cube_counts(self) -> "RuleExecutionState":
        if any(count < 0 for count in self.tableau.values()):
            raise ValueError("tableau cube counts cannot be negative")
        return self


class RuleExecutionEvent(StrictModel):
    rule_id: str
    action: str
    source_id: str | None = None
    target_id: str
    amount: int


class TargetSelector(Protocol):
    """Game-owned boundary for resolving authored selector phrases."""

    def resolve(
        self, phrase: str, state: RuleExecutionState
    ) -> tuple[str, ...]: ...


class RuleExecutionResult(StrictModel):
    state: RuleExecutionState
    events: tuple[RuleExecutionEvent, ...] = ()
    blockers: tuple[SimulationBlocker, ...] = ()


class ExactRuleExecutor:
    """Execute only exact Improve, Worsen, and cube Transfer operations."""

    def execute(
        self,
        rule: RuleIR,
        state: RuleExecutionState,
        *,
        selector: TargetSelector | None = None,
    ) -> RuleExecutionResult:
        if rule.kind is RuleKind.STATE_CHANGE:
            target, blocker = self._target(rule.subject or "", state, selector, rule.id)
            if blocker:
                return RuleExecutionResult(state=state, blockers=(blocker,))
            assert target is not None
            return self.apply_state_change(rule.id, rule.action or "", target, state)
        if rule.kind is RuleKind.TRANSFER:
            if (rule.resource or "").lower() not in {"cube", "cubes"}:
                return self._unsupported(rule, state, "Only cube transfers are executable.")
            source, source_blocker = self._target(rule.subject or "", state, selector, rule.id)
            target, target_blocker = self._target(rule.action or "", state, selector, rule.id)
            blockers = tuple(item for item in (source_blocker, target_blocker) if item)
            if blockers:
                return RuleExecutionResult(state=state, blockers=blockers)
            assert source is not None and target is not None
            amount = rule.amount or 0
            if amount < 1 or state.tableau[source] < amount:
                return RuleExecutionResult(
                    state=state,
                    blockers=(self._blocker(rule.id, BlockerKind.INVARIANT_FAILURE, "Transfer source has fewer cubes than the exact amount.", (source, target)),),
                )
            tableau = dict(state.tableau)
            tableau[source] -= amount
            tableau[target] += amount
            return RuleExecutionResult(
                state=RuleExecutionState(tableau=tableau),
                events=(RuleExecutionEvent(rule_id=rule.id, action="transfer", source_id=source, target_id=target, amount=amount),),
            )
        return self._unsupported(rule, state, "This RuleIR kind is outside the narrow executable subset.")

    def apply_state_change(
        self,
        rule_id: str,
        action: str,
        target_id: str,
        state: RuleExecutionState,
    ) -> RuleExecutionResult:
        action = action.lower()
        if target_id not in state.tableau:
            return RuleExecutionResult(
                state=state,
                blockers=(self._blocker(rule_id, BlockerKind.AMBIGUOUS_SOURCE_RULE, "The exact target is not in the tableau.", (target_id,)),),
            )
        delta = {"improve": -1, "worsen": 1}.get(action)
        if delta is None:
            return RuleExecutionResult(
                state=state,
                blockers=(self._blocker(rule_id, BlockerKind.UNSUPPORTED_RULE, f"Unsupported state change {action!r}.", (target_id,)),),
            )
        if delta < 0 and state.tableau[target_id] <= 0:
            return RuleExecutionResult(
                state=state,
                blockers=(self._blocker(rule_id, BlockerKind.INVARIANT_FAILURE, "Improve cannot remove a cube from an empty target.", (target_id,)),),
            )
        tableau = dict(state.tableau)
        tableau[target_id] += delta
        return RuleExecutionResult(
            state=RuleExecutionState(tableau=tableau),
            events=(RuleExecutionEvent(rule_id=rule_id, action=action, target_id=target_id, amount=1),),
        )

    def _target(
        self,
        phrase: str,
        state: RuleExecutionState,
        selector: TargetSelector | None,
        rule_id: str,
    ) -> tuple[str | None, SimulationBlocker | None]:
        if phrase in state.tableau:
            return phrase, None
        if selector is None:
            return None, self._blocker(
                rule_id,
                BlockerKind.AMBIGUOUS_SOURCE_RULE,
                f"{phrase!r} is a selector phrase, not an exact tableau ID.",
                tuple(state.tableau),
                required_input="Register an explicit game-owned TargetSelector or author an exact target ID.",
            )
        targets = selector.resolve(phrase, state)
        if len(targets) != 1 or targets[0] not in state.tableau:
            return None, self._blocker(
                rule_id,
                BlockerKind.AMBIGUOUS_SOURCE_RULE,
                f"Selector {phrase!r} did not resolve to exactly one legal target.",
                tuple(targets),
                required_input="Make the selector deterministic for this state.",
            )
        return targets[0], None

    def _unsupported(self, rule: RuleIR, state: RuleExecutionState, message: str) -> RuleExecutionResult:
        return RuleExecutionResult(
            state=state,
            blockers=(self._blocker(rule.id, BlockerKind.UNSUPPORTED_RULE, message, (rule.id,)),),
        )

    @staticmethod
    def _blocker(
        rule_id: str,
        kind: BlockerKind,
        message: str,
        entity_ids: tuple[str, ...],
        *,
        required_input: str | None = None,
    ) -> SimulationBlocker:
        return SimulationBlocker(
            id=f"rule-execution:blocker:{rule_id}",
            kind=kind,
            message=message,
            entity_ids=entity_ids,
            required_input=required_input,
        )
