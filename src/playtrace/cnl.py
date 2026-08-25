"""Deterministic tabletop controlled-language parser and semantic compiler."""

from __future__ import annotations

import json
from enum import StrEnum
from importlib.resources import files

from lark import Lark, Transformer

from playtrace.models import StrictModel


class RuleKind(StrEnum):
    TRIGGER = "trigger"
    CONDITION = "condition"
    PERMISSION = "permission"
    OBLIGATION = "obligation"
    COST = "cost"
    LIMIT = "limit"
    CHOICE = "choice"
    SETUP = "setup"
    TRANSFER = "transfer"
    STATE_CHANGE = "state_change"


class RuleIR(StrictModel):
    id: str
    kind: RuleKind
    subject: str | None = None
    action: str | None = None
    condition: str | None = None
    trigger: str | None = None
    amount: int | None = None
    resource: str | None = None
    period: str | None = None
    options: tuple[str, ...] = ()
    source: str


def _text(token: object) -> str:
    return json.loads(str(token))


class _RuleTransformer(Transformer):
    def start(self, items: list[RuleIR]) -> tuple[RuleIR, ...]:
        return tuple(items)

    def rule(self, items: list[object]) -> RuleIR:
        rule_id, rule = items
        assert isinstance(rule, RuleIR)
        return rule.model_copy(update={"id": str(rule_id)})

    def trigger(self, items: list[object]) -> RuleIR:
        return RuleIR(id="pending", kind=RuleKind.TRIGGER, trigger=_text(items[0]), action=_text(items[1]), source="")

    def condition(self, items: list[object]) -> RuleIR:
        return RuleIR(id="pending", kind=RuleKind.CONDITION, condition=_text(items[0]), action=_text(items[1]), source="")

    def permission(self, items: list[object]) -> RuleIR:
        return RuleIR(id="pending", kind=RuleKind.PERMISSION, subject=_text(items[0]), action=_text(items[1]), source="")

    def obligation(self, items: list[object]) -> RuleIR:
        return RuleIR(id="pending", kind=RuleKind.OBLIGATION, subject=_text(items[0]), action=_text(items[1]), source="")

    def cost(self, items: list[object]) -> RuleIR:
        return RuleIR(id="pending", kind=RuleKind.COST, subject=_text(items[0]), amount=int(items[1]), resource=_text(items[2]), source="")

    def limit(self, items: list[object]) -> RuleIR:
        return RuleIR(id="pending", kind=RuleKind.LIMIT, subject=_text(items[0]), amount=int(items[1]), period=_text(items[2]), source="")

    def string_list(self, items: list[object]) -> tuple[str, ...]:
        return tuple(_text(item) for item in items)

    def choice(self, items: list[object]) -> RuleIR:
        return RuleIR(id="pending", kind=RuleKind.CHOICE, subject=_text(items[0]), amount=int(items[1]), options=items[2], source="")

    def setup(self, items: list[object]) -> RuleIR:
        return RuleIR(id="pending", kind=RuleKind.SETUP, action=_text(items[0]), source="")

    def transfer(self, items: list[object]) -> RuleIR:
        return RuleIR(
            id="pending",
            kind=RuleKind.TRANSFER,
            amount=int(items[0]),
            resource=_text(items[1]),
            subject=_text(items[2]),
            action=_text(items[3]),
            source="",
        )

    def state_change(self, items: list[object]) -> RuleIR:
        return RuleIR(
            id="pending",
            kind=RuleKind.STATE_CHANGE,
            action=str(items[0]).lower(),
            subject=_text(items[1]),
            source="",
        )


class TabletopRuleCompiler:
    def __init__(self) -> None:
        grammar = files("playtrace.grammar").joinpath("tabletop.lark").read_text(encoding="utf-8")
        self._parser = Lark(grammar, parser="lalr", start="start")

    def compile(self, text: str) -> tuple[RuleIR, ...]:
        tree = self._parser.parse(text)
        rules = _RuleTransformer().transform(tree)
        assert isinstance(rules, tuple)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return tuple(
            rule.model_copy(update={"source": line})
            for rule, line in zip(rules, lines, strict=True)
        )

