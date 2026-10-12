from playtrace.cnl import RuleKind, TabletopRuleCompiler


def test_compiles_supported_tabletop_sentence_families() -> None:
    source = """
RULE test:trigger: WHEN "the turn ends" THEN "resolve one crisis card".
RULE test:condition: IF "Flood is at least 4" THEN "Worsen one Structure".
RULE test:permission: "another player" MAY "play one Assist".
RULE test:obligation: "the active player" MUST "activate one card".
RULE test:cost: "Improve a card" COSTS 2 "AP".
RULE test:limit: "Brace" IS LIMITED TO 1 PER "turn".
RULE test:choice: "the active player" CHOOSES 1 FROM "People", "Structure".
RULE test:setup: SETUP "place the Scenario Core face-up".
RULE test:transfer: TRANSFER 1 "cube" FROM "Swollen River" TO "Bridge".
RULE test:state: IMPROVE "one Hazard".
"""

    rules = TabletopRuleCompiler().compile(source)

    assert [rule.kind for rule in rules] == [
        RuleKind.TRIGGER,
        RuleKind.CONDITION,
        RuleKind.PERMISSION,
        RuleKind.OBLIGATION,
        RuleKind.COST,
        RuleKind.LIMIT,
        RuleKind.CHOICE,
        RuleKind.SETUP,
        RuleKind.TRANSFER,
        RuleKind.STATE_CHANGE,
    ]
    assert rules[0].id == "test:trigger"
    assert rules[4].amount == 2
    assert rules[6].options == ("People", "Structure")

