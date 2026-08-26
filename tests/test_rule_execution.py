import pytest

from playtrace.cnl import TabletopRuleCompiler
from playtrace.rule_execution import ExactRuleExecutor, RuleExecutionState
from playtrace.simulation import BlockerKind


def test_cnl_exact_improve_worsen_and_transfer_execute() -> None:
    rules = TabletopRuleCompiler().compile(
        '\n'.join(
            (
                'RULE exact:improve: IMPROVE "river".',
                'RULE exact:worsen: WORSEN "bridge".',
                'RULE exact:transfer: TRANSFER 1 "cube" FROM "bridge" TO "river".',
            )
        )
    )
    executor = ExactRuleExecutor()
    state = RuleExecutionState(tableau={"river": 2, "bridge": 1})

    for rule in rules:
        result = executor.execute(rule, state)
        assert result.blockers == ()
        state = result.state

    assert state.tableau == {"river": 2, "bridge": 1}


def test_selector_phrase_blocks_without_explicit_selector_boundary() -> None:
    rule = TabletopRuleCompiler().compile(
        'RULE exact:selector: IMPROVE "one Hazard".'
    )[0]

    result = ExactRuleExecutor().execute(
        rule, RuleExecutionState(tableau={"river": 2})
    )

    assert result.blockers[0].kind is BlockerKind.AMBIGUOUS_SOURCE_RULE
    assert "TargetSelector" in (result.blockers[0].required_input or "")


def test_game_owned_selector_must_resolve_exactly_one_target() -> None:
    class RiverSelector:
        def resolve(self, phrase, state):
            assert phrase == "one Hazard"
            return ("river",)

    rule = TabletopRuleCompiler().compile(
        'RULE exact:selector: WORSEN "one Hazard".'
    )[0]

    result = ExactRuleExecutor().execute(
        rule,
        RuleExecutionState(tableau={"river": 2, "bridge": 1}),
        selector=RiverSelector(),
    )

    assert result.blockers == ()
    assert result.state.tableau["river"] == 3


def test_execution_state_rejects_negative_cube_counts() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        RuleExecutionState(tableau={"river": -1})


def test_improve_guards_nonpositive_count() -> None:
    state = RuleExecutionState(tableau={"river": 0})

    result = ExactRuleExecutor().apply_state_change(
        "exact:improve", "improve", "river", state
    )

    assert result.state is state
    assert result.blockers[0].kind is BlockerKind.INVARIANT_FAILURE
