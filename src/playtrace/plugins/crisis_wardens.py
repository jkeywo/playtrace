"""Executable projection for the currently modelled Crisis Wardens rules."""

from __future__ import annotations

import random
import re
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from playtrace.models import DesignEntity, DesignSnapshot
from playtrace.simulation import (
    BlockerKind,
    PlayerSimulationState,
    PlayerPolicyKind,
    SimulationBlocker,
    SimulationConfig,
    SimulationCheckpoint,
    SimulationEvent,
    SimulationMetrics,
    SimulationProvenance,
    SimulationRun,
    SimulationStatus,
    TabletopSimulationState,
    content_digest,
    snapshot_content_digest,
)


class CrisisWardensSimulationPlugin:
    id = "crisis-wardens-tabletop"
    version = "0.2.0"
    provisional_ruleset = "cw:rules:provisional-core-v0.2"

    def run(
        self, snapshot: DesignSnapshot, config: SimulationConfig, *, runner: str
    ) -> SimulationRun:
        entities = {entity.id: entity for entity in snapshot.entities}
        assertions = {assertion.id: assertion for assertion in snapshot.assertions}
        blockers: list[SimulationBlocker] = []
        overrides = config.override_map()
        provisional = config.ruleset_id == self.provisional_ruleset
        if overrides.get("end_turn_pressure_target", "highest") not in {
            "highest",
            "lowest",
        }:
            blockers.append(
                _blocker(
                    "end-turn-pressure-target",
                    BlockerKind.INVALID_CONFIGURATION,
                    "end_turn_pressure_target must be 'highest' or 'lowest'.",
                    (config.scenario_id,),
                )
            )
        if config.ruleset_id and not provisional:
            blockers.append(
                _blocker(
                    "ruleset",
                    BlockerKind.INVALID_CONFIGURATION,
                    f"Unknown Crisis Wardens ruleset {config.ruleset_id!r}.",
                    (),
                )
            )

        scenario = entities.get(config.scenario_id)
        if scenario is None or scenario.kind != "scenario":
            blockers.append(
                _blocker(
                    "scenario",
                    BlockerKind.INVALID_CONFIGURATION,
                    f"{config.scenario_id} is not a scenario in the snapshot.",
                    (config.scenario_id,),
                )
            )
            return self._blocked(snapshot, config, runner, blockers)
        missing_characters = tuple(
            item
            for item in config.character_ids
            if item not in entities or entities[item].kind != "character"
        )
        if missing_characters:
            blockers.append(
                _blocker(
                    "characters",
                    BlockerKind.INVALID_CONFIGURATION,
                    "The configuration references missing character IDs.",
                    missing_characters,
                )
            )
        if provisional and not 1 <= len(config.character_ids) <= 2:
            blockers.append(
                _blocker(
                    "player-count",
                    BlockerKind.INVALID_CONFIGURATION,
                    "The provisional game supports one or two responders; four-player rules are reserved until more characters exist.",
                    config.character_ids,
                )
            )
        if provisional:
            missing_decks = tuple(
                character_id
                for character_id in config.character_ids
                if not any(
                    entity.kind == "character_card"
                    and str(_fields(entity).get("Character ID", "")) == character_id
                    for entity in snapshot.entities
                )
            )
            if missing_decks:
                blockers.append(
                    _blocker(
                        "character-decks",
                        BlockerKind.MISSING_SOURCE_FACT,
                        "Every selected responder needs at least one character card.",
                        missing_decks,
                    )
                )

        cards = {
            entity.id: entity
            for entity in snapshot.entities
            if entity.kind == "scenario_card"
            and str(_fields(entity).get("Scenario ID", "")) == config.scenario_id
        }
        starting_names = _split_semicolon(
            str(_fields(scenario).get("Back: Starting Tableau", ""))
        )
        by_name = {entity.name: entity.id for entity in cards.values()}
        missing_starting = tuple(name for name in starting_names if name not in by_name)
        if missing_starting:
            blockers.append(
                _blocker(
                    "starting-tableau",
                    BlockerKind.MISSING_SOURCE_FACT,
                    "Starting tableau names do not resolve to card IDs.",
                    (config.scenario_id,),
                    required_input="Stable card IDs for every starting tableau entry.",
                )
            )
        starting_ids = tuple(by_name[name] for name in starting_names if name in by_name)
        starting_cubes = (
            {card_id: 0 for card_id in starting_ids}
            if provisional
            else overrides.get("starting_cubes")
        )
        if not isinstance(starting_cubes, dict) or any(
            card_id not in starting_cubes for card_id in starting_ids
        ):
            blockers.append(
                _blocker(
                    "starting-cubes",
                    BlockerKind.MISSING_SOURCE_FACT,
                    "The source says to use each tableau card's starting cubes but supplies no counts.",
                    (config.scenario_id, *starting_ids),
                    assertion_ids=(f"{config.scenario_id}:field:back_setup",),
                    required_input="starting_cubes mapping for every starting tableau card",
                    overridable=True,
                )
            )
        if config.player_policy is PlayerPolicyKind.PASSIVE and overrides.get(
            "allow_pass"
        ) is not True:
            blockers.append(
                _blocker(
                    "pass-action",
                    BlockerKind.AMBIGUOUS_SOURCE_RULE,
                    "The turn reference requires activating a card; it does not define passing.",
                    config.character_ids,
                    required_input="A legal player action policy or an explicit allow_pass experiment override.",
                    overridable=True,
                )
            )
        deck = self._deck(config, cards, overrides, blockers, provisional=provisional)
        if blockers:
            return self._blocked(snapshot, config, runner, blockers)

        assert isinstance(starting_cubes, dict)
        cube_meanings = {
            card_id: str(_fields(cards[card_id]).get("Cube Meaning", "")).strip()
            for card_id in starting_ids
        }
        missing_meanings = tuple(
            card_id for card_id, meaning in cube_meanings.items() if not meaning
        )
        if missing_meanings:
            blockers.append(
                _blocker(
                    "cube-meaning",
                    BlockerKind.MISSING_SOURCE_FACT,
                    "Every tableau card needs a local cube meaning.",
                    missing_meanings,
                )
            )
            return self._blocked(snapshot, config, runner, blockers)

        escalation = _split_escalation(
            str(_fields(scenario).get("Front: Escalation", ""))
        )
        if len(escalation) < 2:
            blockers.append(
                _blocker(
                    "escalation-track",
                    BlockerKind.MISSING_SOURCE_FACT,
                    "The scenario does not define an ordered escalation track.",
                    (config.scenario_id,),
                )
            )
            return self._blocked(snapshot, config, runner, blockers)

        initial = TabletopSimulationState(
            escalation=escalation[0],
            deck=deck,
            tableau={card_id: int(starting_cubes[card_id]) for card_id in starting_ids},
            cube_meanings=cube_meanings,
            players=self._players(snapshot, config) if provisional else {},
            status=SimulationStatus.READY,
        )
        runtime = _Runtime(
            snapshot=snapshot,
            config=config,
            scenario=scenario,
            cards=cards,
            assertions=assertions,
            escalation=escalation,
            state=initial,
            provisional=provisional,
        )
        runtime.emit(
            "simulation.started",
            entity_ids=(config.scenario_id, *config.character_ids),
            data={
                "seed": config.seed,
                "player_policy": config.player_policy.value,
                "ruleset_id": config.ruleset_id,
            },
        )
        runtime.emit(
            "scenario.setup",
            entity_ids=starting_ids,
            data={"tableau": initial.tableau, "escalation": initial.escalation},
            assertion_ids=(f"{config.scenario_id}:field:back_starting_tableau",),
        )
        runtime.status = SimulationStatus.RUNNING
        while runtime.status is SimulationStatus.RUNNING:
            if runtime.turn >= config.max_turns:
                runtime.status = SimulationStatus.MAX_TURNS
                break
            if not runtime.deck:
                runtime.status = SimulationStatus.WON
                runtime.emit("objective.completed", entity_ids=(config.scenario_id,))
                break
            runtime.turn += 1
            actor = config.character_ids[runtime.active_player] if config.character_ids else None
            runtime.emit("turn.started", actor_id=actor)
            if provisional and config.player_policy in {
                PlayerPolicyKind.GREEDY,
                PlayerPolicyKind.RISK_AVERSE,
                PlayerPolicyKind.RANDOM_CONSTRAINED,
            }:
                runtime.take_player_turn(actor)
            elif config.player_policy is PlayerPolicyKind.PASSIVE:
                runtime.emit(
                    "player.action_skipped",
                    actor_id=actor,
                    data={"override": "allow_pass"},
                )
            else:
                runtime.block(
                    _blocker(
                        "player-policy",
                        BlockerKind.UNSUPPORTED_RULE,
                        f"Player policy {config.player_policy.value!r} is not implemented for Crisis Wardens.",
                        config.character_ids,
                    )
                )
                break
            runtime.draw_and_resolve()
            if runtime.status is SimulationStatus.RUNNING:
                runtime.resolve_end_of_turn(actor)
            if runtime.status is SimulationStatus.RUNNING:
                runtime.refill(actor)
                runtime.emit("turn.completed", actor_id=actor)
                if config.character_ids:
                    runtime.active_player = (runtime.active_player + 1) % len(
                        config.character_ids
                    )

        return runtime.result(self, runner)

    @staticmethod
    def _deck(
        config: SimulationConfig,
        cards: dict[str, DesignEntity],
        overrides: dict[str, Any],
        blockers: list[SimulationBlocker],
        *,
        provisional: bool,
    ) -> tuple[str, ...]:
        if config.deck_order:
            deck = list(config.deck_order)
        elif isinstance(overrides.get("scenario_deck_card_ids"), list):
            deck = [str(item) for item in overrides["scenario_deck_card_ids"]]
            random.Random(config.seed).shuffle(deck)
        elif provisional:
            deck = []
            for card_id, card in cards.items():
                fields = _fields(card)
                if str(fields.get("Card Kind", "")).lower() != "event":
                    continue
                quantity = int(str(fields.get("Qty", "1") or "1"))
                if card_id == "SCD-012":
                    quantity = min(quantity, 2)
                deck.extend([card_id] * quantity)
            random.Random(config.seed).shuffle(deck)
        else:
            blockers.append(
                _blocker(
                    "deck-zones",
                    BlockerKind.AMBIGUOUS_SOURCE_RULE,
                    "The card table gives quantities but does not identify which tableau cards begin in play, are set aside, or enter the draw deck.",
                    (config.scenario_id,),
                    required_input="An ordered deck or scenario_deck_card_ids experiment override.",
                    overridable=True,
                )
            )
            return ()
        unknown = tuple(item for item in deck if item not in cards)
        if unknown:
            blockers.append(
                _blocker(
                    "deck-card",
                    BlockerKind.INVALID_CONFIGURATION,
                    "The deck contains unknown card IDs.",
                    unknown,
                )
            )
        return tuple(deck)

    @staticmethod
    def _players(
        snapshot: DesignSnapshot, config: SimulationConfig
    ) -> dict[str, PlayerSimulationState]:
        character_cards = {
            character_id: [
                entity
                for entity in snapshot.entities
                if entity.kind == "character_card"
                and str(_fields(entity).get("Character ID", "")) == character_id
            ]
            for character_id in config.character_ids
        }
        players: dict[str, PlayerSimulationState] = {}
        for character_id, entities in character_cards.items():
            deck = [
                entity.id
                for entity in entities
                for _ in range(int(str(_fields(entity).get("Qty", "1") or "1")))
            ]
            seed = int(content_digest([config.seed, character_id])[:16], 16)
            random.Random(seed).shuffle(deck)
            hand, remaining = deck[:3], deck[3:]
            players[character_id] = PlayerSimulationState(
                character_id=character_id,
                deck=tuple(remaining),
                hand=tuple(hand),
            )
        return players

    def _blocked(
        self,
        snapshot: DesignSnapshot,
        config: SimulationConfig,
        runner: str,
        blockers: list[SimulationBlocker],
    ) -> SimulationRun:
        provenance = _provenance(snapshot, config, self, runner)
        identity = content_digest(
            {
                "provenance": provenance.model_dump(mode="json", exclude={"observed_at"}),
                "blockers": [item.model_dump(mode="json") for item in blockers],
            }
        )[:20]
        return SimulationRun(
            id=f"{snapshot.project_id}:simulation:{identity}",
            config=config,
            provenance=provenance,
            blockers=tuple(blockers),
            metrics=SimulationMetrics(
                turns_completed=0,
                cards_resolved=0,
                escalation_steps=0,
                improves=0,
                worsens=0,
                maximum_tableau_cubes=0,
                blocker_count=len(blockers),
            ),
        )


class _Runtime:
    def __init__(
        self,
        *,
        snapshot: DesignSnapshot,
        config: SimulationConfig,
        scenario: DesignEntity,
        cards: dict[str, DesignEntity],
        assertions: dict[str, Any],
        escalation: tuple[str, ...],
        state: TabletopSimulationState,
        provisional: bool,
    ) -> None:
        self.snapshot = snapshot
        self.config = config
        self.scenario = scenario
        self.cards = cards
        self.assertions = assertions
        self.escalation = escalation
        self.provisional = provisional
        self.initial = state
        self.turn = 0
        self.active_player = 0
        self.deck = list(state.deck)
        self.discard: list[str] = []
        self.tableau = dict(state.tableau)
        self.cube_meanings = dict(state.cube_meanings)
        self.status = SimulationStatus.READY
        self.events: list[SimulationEvent] = []
        self.state_trace: list[SimulationCheckpoint] = []
        self.blockers: list[SimulationBlocker] = []
        self.escalation_index = 0
        self.maximum_tableau_cubes = max(self.tableau.values(), default=0)
        self.players = {
            character_id: {
                "deck": list(player.deck),
                "hand": list(player.hand),
                "discard": list(player.discard),
                "elements": dict(player.elements),
                "fatigue_discarded": player.fatigue_discarded,
                "rests": player.rests,
            }
            for character_id, player in state.players.items()
        }
        self.character_cards = {
            entity.id: entity
            for entity in snapshot.entities
            if entity.kind == "character_card"
        }
        self.characters = {
            entity.id: entity for entity in snapshot.entities if entity.kind == "character"
        }
        self.protected: set[str] = set()
        self.suppressed_thresholds: set[str] = set()
        self.prevent_next_structure = False
        self.affinity_gained: set[str] = set()

    def take_player_turn(self, actor_id: str | None) -> None:
        if actor_id is None or actor_id not in self.players:
            self.block(
                _blocker(
                    "player-state",
                    BlockerKind.INVALID_CONFIGURATION,
                    "The active responder has no character deck state.",
                    (actor_id,) if actor_id else (),
                )
            )
            return
        player = self.players[actor_id]
        self.protected.clear()
        self.suppressed_thresholds.clear()
        self.prevent_next_structure = False
        self.affinity_gained.discard(actor_id)
        if not player["hand"]:
            if not player["deck"] and player["discard"]:
                seed = int(content_digest([self.config.seed, actor_id, self.turn, "rest"])[:16], 16)
                random.Random(seed).shuffle(player["discard"])
                player["deck"] = player["discard"]
                player["discard"] = []
                player["rests"] += 1
                self._draw_to_hand(actor_id)
                self.emit(
                    "player.rested",
                    actor_id=actor_id,
                    data={"hand_size": len(player["hand"])},
                )
                return
            self._draw_to_hand(actor_id)
        if not player["hand"]:
            self.emit("player.no_action", actor_id=actor_id)
            return

        card_id = self._choose_activation(actor_id)
        player["hand"].remove(card_id)
        card = self.character_cards[card_id]
        fields = _fields(card)
        for element in _split_semicolon(str(fields.get("Elements", ""))):
            player["elements"][element] = player["elements"].get(element, 0) + 1
        ap = int(str(fields.get("AP", "0") or "0"))
        self.emit(
            "player.card_activated",
            actor_id=actor_id,
            entity_ids=(card_id,),
            data={"ap": ap, "persistent_elements": dict(player["elements"])},
        )
        ap = self._use_character_card(actor_id, card_id, ap)
        self._use_available_powers(actor_id)
        while ap > 0 and self.status is SimulationStatus.RUNNING:
            target = self._highest_cubes()
            if target is None or self.tableau[target] <= 0:
                break
            self.improve(target, source_id="cw:rule:basic-improve", actor_id=actor_id)
            ap -= 1
            self.emit(
                "player.ap_spent",
                actor_id=actor_id,
                entity_ids=(target,),
                data={"action": "Improve", "remaining_ap": ap},
            )
        if ap:
            self.emit("player.ap_lost", actor_id=actor_id, data={"amount": ap})
        player["discard"].append(card_id)
        self.emit("player.card_discarded", actor_id=actor_id, entity_ids=(card_id,))

    def _choose_activation(self, actor_id: str) -> str:
        hand = self.players[actor_id]["hand"]
        if self.config.player_policy is PlayerPolicyKind.RANDOM_CONSTRAINED:
            seed = int(content_digest([self.config.seed, actor_id, self.turn, "activation"])[:16], 16)
            return random.Random(seed).choice(sorted(hand))
        return sorted(
            hand,
            key=lambda item: (-int(str(_fields(self.character_cards[item]).get("AP", "0") or "0")), item),
        )[0]

    def _use_character_card(self, actor_id: str, card_id: str, ap: int) -> int:
        fields = _fields(self.character_cards[card_id])
        timing = str(fields.get("Timing / Cost", ""))
        if timing.lower() == "assist":
            return ap
        match = re.search(r"(\d+)\s*AP", timing, re.IGNORECASE)
        cost = int(match.group(1)) if match else 0
        if cost > ap:
            return ap
        used = False
        if card_id == "CCD-O02":
            first = self._highest_cubes("Hazard")
            second = self._lowest_cubes("Hazard", exclude={first} if first else set())
            if first and self.tableau[first] > 0 and second:
                self.improve(first, source_id=card_id, actor_id=actor_id)
                self.worsen(second, source_id=card_id)
                used = True
        elif card_id == "CCD-O03":
            target = self._highest_cubes("Structure")
            if target and self.tableau[target] > 0:
                self.improve(target, source_id=card_id, actor_id=actor_id)
                if self.tableau.get(target, 0) > 0:
                    self.improve(target, source_id=card_id, actor_id=actor_id)
                used = True
        elif card_id == "CCD-O05":
            target = self._highest_cubes("Hazard")
            if target:
                self.protected.add(target)
                self.emit("card.protected", actor_id=actor_id, entity_ids=(target,), data={"source_id": card_id})
                used = True
        elif card_id == "CCD-O06" and len(self.deck) >= 2:
            top = self.deck[:2]
            self.deck[:2] = sorted(top, key=lambda item: item == "SCD-012")
            self.emit("scenario.deck_reordered", actor_id=actor_id, entity_ids=tuple(top), data={"source_id": card_id})
            used = True
        elif card_id == "CCD-O07":
            first = self._highest_cubes("Hazard")
            second = self._lowest_cubes(exclude={first} if first else set())
            if first and self.tableau[first] > 0 and second:
                self.improve(first, source_id=card_id, actor_id=actor_id)
                self.worsen(second, source_id=card_id)
                used = True
        elif card_id == "CCD-S01":
            target = self._highest_cubes("Panic")
            if target and self.tableau[target] > 0:
                self.improve(target, source_id=card_id, actor_id=actor_id)
                used = True
        elif card_id == "CCD-S02":
            target = self._highest_cubes("People")
            if target and self.tableau[target] > 0:
                self.improve(target, source_id=card_id, actor_id=actor_id)
                if self._has_keyword(target, "Panic"):
                    affinity = str(_fields(self.characters[actor_id]).get("Elemental Affinity", ""))
                    if affinity:
                        self.players[actor_id]["elements"][affinity] = self.players[actor_id]["elements"].get(affinity, 0) + 1
                used = True
        elif card_id == "CCD-S03":
            target = self._highest_cubes_any(("People", "Beast", "Spirit"))
            if target:
                self.suppressed_thresholds.add(target)
                self.emit("card.threshold_suppressed", actor_id=actor_id, entity_ids=(target,), data={"source_id": card_id})
                used = True
        elif card_id == "CCD-S05":
            other = next((item for item in self.config.character_ids if item != actor_id), None)
            target = self._highest_cubes()
            if other and target and self.tableau[target] > 0:
                self.improve(target, source_id=card_id, actor_id=other)
                used = True
        elif card_id == "CCD-S06":
            pair = self._shared_social_pair()
            if pair:
                better, worse = sorted(pair, key=lambda item: self.tableau[item], reverse=True)
                if self.tableau[better] > 0:
                    self.improve(better, source_id=card_id, actor_id=actor_id)
                    self.worsen(worse, source_id=card_id)
                    used = True
        if used:
            self.emit("player.card_action_resolved", actor_id=actor_id, entity_ids=(card_id,), data={"cost": cost})
            return ap - cost
        return ap

    def _use_available_powers(self, actor_id: str) -> None:
        elements = self.players[actor_id]["elements"]
        if actor_id == "CHR-001":
            if self._meets(elements, {"Earth": 2}):
                self.prevent_next_structure = True
                self.emit("player.power_prepared", actor_id=actor_id, data={"power": "Hold Fast"})
            if self._meets(elements, {"Water": 2, "Earth": 1}):
                first = self._highest_cubes("Hazard")
                second = self._lowest_cubes("Hazard", exclude={first} if first else set())
                if first and self.tableau[first] > 0 and second:
                    self.improve(first, source_id="CHR-001:power:turn-the-waters", actor_id=actor_id)
                    self.worsen(second, source_id="CHR-001:power:turn-the-waters")
                    self.emit("player.power_used", actor_id=actor_id, data={"power": "Turn the Waters"})
            if self._meets(elements, {"Water": 3, "Earth": 1}):
                target = self._highest_cubes("Hazard")
                if target and self.tableau[target] > 0:
                    self.improve(target, source_id="CHR-001:power:break-the-force", actor_id=actor_id)
                    self.emit("player.power_used", actor_id=actor_id, data={"power": "Break the Force"})
        elif actor_id == "CHR-002":
            if self._meets(elements, {"Air": 2}):
                target = self._highest_cubes("Panic")
                if target and self.tableau[target] > 0:
                    self.improve(target, source_id="CHR-002:power:steady-voice", actor_id=actor_id)
                    self.emit("player.power_used", actor_id=actor_id, data={"power": "Steady Voice"})
            if self._meets(elements, {"Air": 1, "Spirit": 1}):
                target = self._highest_cubes_any(("People", "Beast", "Spirit"))
                if target:
                    self.suppressed_thresholds.add(target)
                    self.emit("player.power_used", actor_id=actor_id, entity_ids=(target,), data={"power": "Shared Purpose"})
            if self._meets(elements, {"Air": 2, "Spirit": 1}):
                panic = self._highest_cubes("Panic")
                if panic and self.tableau[panic] > 0:
                    self.improve(panic, source_id="CHR-002:power:call-them-back", actor_id=actor_id)
                    related = self._highest_cubes_any(("People", "Beast", "Spirit"), exclude={panic})
                    if related and self.tableau[related] > 0:
                        self.improve(related, source_id="CHR-002:power:call-them-back", actor_id=actor_id)
                    self.emit("player.power_used", actor_id=actor_id, data={"power": "Call Them Back"})

    @staticmethod
    def _meets(pool: dict[str, int], requirement: dict[str, int]) -> bool:
        return all(pool.get(element, 0) >= count for element, count in requirement.items())

    def _highest_cubes(self, keyword: str | None = None) -> str | None:
        candidates = [item for item in self.tableau if keyword is None or self._has_keyword(item, keyword)]
        return min(candidates, key=lambda item: (-self.tableau[item], item)) if candidates else None

    def _lowest_cubes(
        self, keyword: str | None = None, *, exclude: set[str] | None = None
    ) -> str | None:
        excluded = exclude or set()
        candidates = [
            item
            for item in self.tableau
            if item not in excluded and (keyword is None or self._has_keyword(item, keyword))
        ]
        return min(candidates, key=lambda item: (self.tableau[item], item)) if candidates else None

    def _highest_cubes_any(
        self, keywords: tuple[str, ...], *, exclude: set[str] | None = None
    ) -> str | None:
        excluded = exclude or set()
        candidates = [
            item
            for item in self.tableau
            if item not in excluded and any(self._has_keyword(item, keyword) for keyword in keywords)
        ]
        return min(candidates, key=lambda item: (-self.tableau[item], item)) if candidates else None

    def _shared_social_pair(self) -> tuple[str, str] | None:
        keywords = ("People", "Beast", "Spirit")
        candidates = sorted(self.tableau)
        for index, first in enumerate(candidates):
            for second in candidates[index + 1 :]:
                if any(self._has_keyword(first, key) and self._has_keyword(second, key) for key in keywords):
                    return first, second
        return None

    def _has_keyword(self, card_id: str, keyword: str) -> bool:
        return keyword in _split_semicolon(str(_fields(self.cards[card_id]).get("Keywords", "")))

    def _draw_to_hand(self, actor_id: str) -> None:
        player = self.players[actor_id]
        drawn: list[str] = []
        while len(player["hand"]) < 3 and player["deck"]:
            card_id = player["deck"].pop(0)
            player["hand"].append(card_id)
            drawn.append(card_id)
        if drawn:
            self.emit("player.cards_drawn", actor_id=actor_id, entity_ids=tuple(drawn), data={"hand_size": len(player["hand"])})

    def refill(self, actor_id: str | None) -> None:
        if actor_id in self.players:
            self._draw_to_hand(actor_id)

    def fatigue(self, actor_id: str, amount: int, *, source_id: str) -> None:
        player = self.players[actor_id]
        discarded: list[str] = []
        for _ in range(amount):
            if not player["deck"]:
                break
            card_id = player["deck"].pop(0)
            player["discard"].append(card_id)
            player["fatigue_discarded"] += 1
            discarded.append(card_id)
        self.emit(
            "player.fatigued",
            actor_id=actor_id,
            entity_ids=tuple(discarded),
            data={"source_id": source_id, "requested": amount, "discarded": len(discarded)},
        )

    def resolve_end_of_turn(self, actor_id: str | None) -> None:
        if self.escalation[self.escalation_index].lower() == "emergency" and "SCD-004" in self.tableau:
            self.worsen("SCD-004", source_id="cw:rule:emergency-panicked-evacuation")
        pressure = self.config.override_map().get("end_turn_pressure", {})
        pressure_target = self.config.override_map().get(
            "end_turn_pressure_target", "highest"
        )
        level = self.escalation[self.escalation_index].lower()
        amount = pressure.get(level, 0) if isinstance(pressure, dict) else 0
        if isinstance(amount, int) and 0 <= amount <= 3:
            for _ in range(amount):
                if self.status is not SimulationStatus.RUNNING or not self.tableau:
                    break
                target_value = (
                    min(self.tableau.values())
                    if pressure_target == "lowest"
                    else max(self.tableau.values())
                )
                target = sorted(
                    card_id
                    for card_id, cubes in self.tableau.items()
                    if cubes == target_value
                )[0]
                self.worsen(target, source_id="cw:experiment:end-turn-pressure")
        elif amount:
            self.block(
                _blocker(
                    "end-turn-pressure",
                    BlockerKind.INVALID_CONFIGURATION,
                    "end_turn_pressure values must be integers from zero to three.",
                    (self.config.scenario_id,),
                )
            )
        delayed = tuple(sorted(self.suppressed_thresholds))
        self.suppressed_thresholds.clear()
        for card_id in delayed:
            if self.status is SimulationStatus.RUNNING:
                self.resolve_threshold(card_id, before=max(0, self.tableau.get(card_id, 0) - 1))

    def improve(self, card_id: str, *, source_id: str, actor_id: str | None = None) -> None:
        if card_id not in self.tableau or self.tableau[card_id] <= 0:
            return
        self.tableau[card_id] -= 1
        self.emit(
            "card.improved",
            actor_id=actor_id,
            entity_ids=(card_id,),
            data={"source_id": source_id, "cubes": self.tableau[card_id], "cube_meaning": self.cube_meanings[card_id]},
            assertion_ids=(f"{source_id}:field:rules_text",),
        )
        if actor_id == "CHR-001" and actor_id not in self.affinity_gained and (
            self._has_keyword(card_id, "Hazard") or self._has_keyword(card_id, "Structure")
        ):
            self.players[actor_id]["elements"]["Earth"] = self.players[actor_id]["elements"].get("Earth", 0) + 1
            self.affinity_gained.add(actor_id)
        if actor_id == "CHR-002" and actor_id not in self.affinity_gained and self._has_keyword(card_id, "Panic"):
            self.players[actor_id]["elements"]["Air"] = self.players[actor_id]["elements"].get("Air", 0) + 1
            self.affinity_gained.add(actor_id)
        self._play_improve_assists(card_id, source_id)

    def _play_improve_assists(self, card_id: str, source_id: str) -> None:
        if not self.provisional:
            return
        for actor_id in self.config.character_ids:
            hand = self.players[actor_id]["hand"]
            if "CCD-O08" in hand and self.tableau.get(card_id, 0) > 0:
                hand.remove("CCD-O08")
                self.players[actor_id]["discard"].append("CCD-O08")
                self.emit("assist.played", actor_id=actor_id, entity_ids=("CCD-O08", card_id), data={"source_id": source_id, "effect": "additional Improve"})
                self.improve(card_id, source_id="CCD-O08", actor_id=actor_id)
                break
        if not (self._has_keyword(card_id, "People") or self._has_keyword(card_id, "Panic")):
            return
        for actor_id in self.config.character_ids:
            hand = self.players[actor_id]["hand"]
            if "CCD-S07" not in hand:
                continue
            target = self._highest_cubes_any(("People", "Panic"), exclude={card_id})
            if target and self.tableau[target] > 0:
                hand.remove("CCD-S07")
                self.players[actor_id]["discard"].append("CCD-S07")
                self.emit("assist.played", actor_id=actor_id, entity_ids=("CCD-S07", target), data={"source_id": source_id, "effect": "Improve different card"})
                self.improve(target, source_id="CCD-S07", actor_id=actor_id)
                break

    def emit(
        self,
        event_type: str,
        *,
        actor_id: str | None = None,
        entity_ids: tuple[str, ...] = (),
        data: dict[str, Any] | None = None,
        assertion_ids: tuple[str, ...] = (),
    ) -> None:
        event = SimulationEvent(
                sequence=len(self.events),
                turn=self.turn,
                event_type=event_type,
                actor_id=actor_id,
                entity_ids=entity_ids,
                data=data or {},
                assertion_ids=tuple(
                    item for item in assertion_ids if item in self.assertions
                ),
            )
        self.events.append(event)
        self.state_trace.append(
            SimulationCheckpoint(sequence=event.sequence, state=self._state_snapshot())
        )

    def _state_snapshot(self) -> TabletopSimulationState:
        return TabletopSimulationState(
            turn=self.turn,
            active_player_index=self.active_player,
            escalation=self.escalation[self.escalation_index],
            deck=tuple(self.deck),
            discard=tuple(self.discard),
            tableau=dict(self.tableau),
            cube_meanings=dict(self.cube_meanings),
            players={
                actor_id: PlayerSimulationState(
                    character_id=actor_id,
                    deck=tuple(player["deck"]),
                    hand=tuple(player["hand"]),
                    discard=tuple(player["discard"]),
                    elements=dict(player["elements"]),
                    fatigue_discarded=player["fatigue_discarded"],
                    rests=player["rests"],
                )
                for actor_id, player in self.players.items()
            },
            status=self.status,
        )

    def block(self, blocker: SimulationBlocker) -> None:
        self.blockers.append(blocker)
        self.status = SimulationStatus.BLOCKED
        self.emit(
            "simulation.blocked",
            entity_ids=blocker.entity_ids,
            data={"blocker_id": blocker.id, "message": blocker.message},
            assertion_ids=blocker.assertion_ids,
        )

    def draw_and_resolve(self) -> None:
        if not self.deck or self.status is not SimulationStatus.RUNNING:
            return
        card_id = self.deck.pop(0)
        card = self.cards[card_id]
        fields = _fields(card)
        self.emit(
            "scenario.card_drawn",
            entity_ids=(card_id,),
            data={"name": card.name},
        )
        if str(fields.get("Status", "")).upper() == "PLACEHOLDER" and not self.provisional:
            self.block(
                _blocker(
                    f"placeholder-{card_id}",
                    BlockerKind.UNSUPPORTED_RULE,
                    f"{card.name} is explicitly PLACEHOLDER and cannot be treated as settled simulation semantics.",
                    (card_id,),
                    assertion_ids=(f"{card_id}:field:rules_text",),
                )
            )
            return
        if str(fields.get("Card Kind", "")) == "Tableau":
            self.block(
                _blocker(
                    f"tableau-entry-{card_id}",
                    BlockerKind.MISSING_SOURCE_FACT,
                    f"{card.name} was drawn as a tableau card but has no defined entry cube count.",
                    (card_id,),
                    assertion_ids=(f"{card_id}:field:cube_meaning",),
                    required_input="Entry cube count and zone semantics for drawn tableau cards.",
                    overridable=True,
                )
            )
            return

        if card_id == "SCD-005":
            target = self.choose("Hazard", most_cubes=True)
            if target:
                self.worsen(target, source_id=card_id)
                if (
                    self.status is SimulationStatus.RUNNING
                    and self.escalation[self.escalation_index].lower() == "emergency"
                ):
                    self.worsen(target, source_id=card_id)
        elif card_id == "SCD-006":
            self.worsen("SCD-001", source_id=card_id)
            if self.status is SimulationStatus.RUNNING:
                target = self.choose("Structure")
                if target:
                    self.worsen(target, source_id=card_id)
        elif card_id == "SCD-007":
            target = "SCD-002" if "SCD-002" in self.tableau else self.choose("Structure")
            if target:
                self.worsen(target, source_id=card_id)
        elif card_id == "SCD-009":
            if "SCD-008" in self.tableau:
                self.worsen("SCD-008", source_id=card_id)
            else:
                self.tableau["SCD-008"] = 2
                self.maximum_tableau_cubes = max(self.maximum_tableau_cubes, 2)
                self.cube_meanings["SCD-008"] = str(
                    _fields(self.cards["SCD-008"]).get("Cube Meaning", "Panic")
                )
                self.emit(
                    "tableau.card_added",
                    entity_ids=("SCD-008",),
                    data={"cubes": 2, "cube_meaning": self.cube_meanings["SCD-008"]},
                    assertion_ids=("SCD-009:field:rules_text",),
                )
        elif card_id == "SCD-010":
            target = self.choose("People")
            if target:
                self.worsen(target, source_id=card_id)
            self.emit(
                "assist.window_declined",
                entity_ids=(card_id,),
                data={"policy": self.config.player_policy.value},
                assertion_ids=("SCD-010:field:rules_text",),
            )
        elif card_id == "SCD-011":
            if self.provisional:
                target = self._highest_cubes("Hazard")
                if target:
                    self.improve(target, source_id=card_id)
            else:
                self.block(
                    _blocker(
                        "placeholder-SCD-011",
                        BlockerKind.UNSUPPORTED_RULE,
                        "Sudden Calm is a PLACEHOLDER card.",
                        (card_id,),
                    )
                )
        elif card_id == "SCD-012":
            self.advance_escalation(card_id)
            if self.status is SimulationStatus.RUNNING:
                self.draw_and_resolve()
        else:
            self.block(
                _blocker(
                    f"unsupported-{card_id}",
                    BlockerKind.UNSUPPORTED_RULE,
                    f"No approved simulation semantics exist for {card.name}.",
                    (card_id,),
                    assertion_ids=(f"{card_id}:field:rules_text",),
                )
            )
        self.discard.append(card_id)
        if self.status is not SimulationStatus.BLOCKED:
            self.emit("scenario.card_resolved", entity_ids=(card_id,))

    def choose(self, keyword: str, *, most_cubes: bool = False) -> str | None:
        candidates = [
            card_id
            for card_id in self.tableau
            if keyword in _split_semicolon(str(_fields(self.cards[card_id]).get("Keywords", "")))
        ]
        if not candidates:
            if self.provisional:
                self.emit(
                    "effect.no_legal_target",
                    data={"keyword": keyword},
                )
                return None
            self.block(
                _blocker(
                    f"no-target-{keyword.lower()}",
                    BlockerKind.AMBIGUOUS_SOURCE_RULE,
                    f"A rule requires a {keyword} target, but none is in the tableau and the source does not define whether the effect fizzles.",
                    tuple(self.tableau),
                    required_input="No-legal-target resolution rule.",
                )
            )
            return None
        if most_cubes:
            maximum = max(self.tableau[item] for item in candidates)
            candidates = [item for item in candidates if self.tableau[item] == maximum]
        return sorted(candidates)[0]

    def worsen(self, card_id: str, *, source_id: str) -> None:
        if card_id not in self.tableau:
            self.block(
                _blocker(
                    f"missing-target-{card_id}",
                    BlockerKind.AMBIGUOUS_SOURCE_RULE,
                    "A named Worsen target is not in the tableau.",
                    (source_id, card_id),
                )
            )
            return
        if card_id in self.protected:
            self.emit("card.worsen_prevented", entity_ids=(card_id,), data={"source_id": source_id, "reason": "protected"})
            return
        if self.prevent_next_structure and self._has_keyword(card_id, "Structure"):
            self.prevent_next_structure = False
            self.emit("card.worsen_prevented", entity_ids=(card_id,), data={"source_id": source_id, "reason": "Hold Fast"})
            return
        if self._play_prevention_assist(card_id, source_id):
            return
        before = self.tableau[card_id]
        self.tableau[card_id] += 1
        self.maximum_tableau_cubes = max(
            self.maximum_tableau_cubes, self.tableau[card_id]
        )
        self.emit(
            "card.worsened",
            entity_ids=(card_id,),
            data={
                "source_id": source_id,
                "cubes": self.tableau[card_id],
                "cube_meaning": self.cube_meanings[card_id],
            },
            assertion_ids=(f"{source_id}:field:rules_text",),
        )
        self.resolve_threshold(card_id, before=before)

    def _play_prevention_assist(self, card_id: str, source_id: str) -> bool:
        if not self.provisional:
            return False
        valid: set[str] = set()
        if self._has_keyword(card_id, "Structure"):
            valid.update({"CCD-O01", "CCD-O04"})
        if self._has_keyword(card_id, "People"):
            valid.add("CCD-S04")
        for actor_id in self.config.character_ids:
            hand = self.players[actor_id]["hand"]
            card = next((item for item in sorted(hand) if item in valid), None)
            if card:
                hand.remove(card)
                self.players[actor_id]["discard"].append(card)
                self.emit(
                    "assist.played",
                    actor_id=actor_id,
                    entity_ids=(card, card_id),
                    data={"source_id": source_id, "effect": "prevent Worsen"},
                )
                return True
        return False

    def resolve_threshold(self, card_id: str, *, before: int) -> None:
        rules = str(_fields(self.cards[card_id]).get("Rules Text", ""))
        match = re.search(r"At\s+(\d+)\+?\s+", rules, re.IGNORECASE)
        if not match:
            return
        threshold = int(match.group(1))
        if self.provisional and card_id == "SCD-001" and self.escalation[self.escalation_index].lower() == "emergency":
            threshold = 3
        current = self.tableau.get(card_id, 0)
        repeat_at_or_above = card_id == "SCD-001"
        triggered = current >= threshold and (repeat_at_or_above or before < threshold)
        if not triggered or card_id in self.suppressed_thresholds:
            return
        if "PLACEHOLDER" in rules.upper() and not self.provisional:
            self.block(
                _blocker(
                    f"threshold-{card_id}",
                    BlockerKind.UNSUPPORTED_RULE,
                    f"{self.cards[card_id].name} reached a threshold whose effect is marked PLACEHOLDER.",
                    (card_id,),
                    assertion_ids=(f"{card_id}:field:rules_text",),
                )
            )
        elif card_id == "SCD-001":
            target = self.choose("Structure")
            if target:
                self.worsen(target, source_id=card_id)
        elif card_id == "SCD-004":
            self.advance_escalation(card_id)
        elif self.provisional and card_id == "SCD-002":
            del self.tableau[card_id]
            del self.cube_meanings[card_id]
            self.emit("tableau.card_collapsed", entity_ids=(card_id,), data={"provisional_effect": "discard, fatigue each responder, escalate"})
            for actor_id in self.config.character_ids:
                self.fatigue(actor_id, 1, source_id=card_id)
            self.advance_escalation(card_id)
        elif self.provisional and card_id == "SCD-003":
            if "SCD-004" in self.tableau:
                self.worsen("SCD-004", source_id=card_id)
            else:
                self.tableau["SCD-004"] = 2
                self.maximum_tableau_cubes = max(self.maximum_tableau_cubes, 2)
                self.cube_meanings["SCD-004"] = str(_fields(self.cards["SCD-004"]).get("Cube Meaning", "Disorder"))
                self.emit("tableau.card_added", entity_ids=("SCD-004",), data={"cubes": 2, "source_id": card_id})
        elif self.provisional and card_id == "SCD-008":
            targets = tuple(item for item in self.tableau if self._has_keyword(item, "Structure"))
            for target in targets:
                if self.status is SimulationStatus.RUNNING:
                    self.worsen(target, source_id=card_id)
        else:
            self.block(
                _blocker(
                    f"threshold-unsupported-{card_id}",
                    BlockerKind.UNSUPPORTED_RULE,
                    "A threshold fired without registered simulation semantics.",
                    (card_id,),
                    assertion_ids=(f"{card_id}:field:rules_text",),
                )
            )

    def advance_escalation(self, source_id: str) -> None:
        if self.escalation_index + 1 >= len(self.escalation):
            self.status = SimulationStatus.LOST
            return
        self.escalation_index += 1
        level = self.escalation[self.escalation_index]
        self.emit(
            "scenario.escalated",
            entity_ids=(self.config.scenario_id,),
            data={"source_id": source_id, "level": level},
        )
        if level.lower() == "catastrophic":
            self.status = SimulationStatus.LOST
            self.emit("objective.failed", entity_ids=(self.config.scenario_id,))

    def result(self, plugin: CrisisWardensSimulationPlugin, runner: str) -> SimulationRun:
        final = TabletopSimulationState(
            turn=self.turn,
            active_player_index=self.active_player,
            escalation=self.escalation[self.escalation_index],
            deck=tuple(self.deck),
            discard=tuple(self.discard),
            tableau=dict(self.tableau),
            cube_meanings=dict(self.cube_meanings),
            players={
                actor_id: PlayerSimulationState(
                    character_id=actor_id,
                    deck=tuple(player["deck"]),
                    hand=tuple(player["hand"]),
                    discard=tuple(player["discard"]),
                    elements=dict(player["elements"]),
                    fatigue_discarded=player["fatigue_discarded"],
                    rests=player["rests"],
                )
                for actor_id, player in self.players.items()
            },
            status=self.status,
        )
        metrics = SimulationMetrics(
            turns_completed=sum(1 for item in self.events if item.event_type == "turn.completed"),
            cards_resolved=sum(
                1 for item in self.events if item.event_type == "scenario.card_resolved"
            ),
            escalation_steps=sum(
                1 for item in self.events if item.event_type == "scenario.escalated"
            ),
            improves=sum(1 for item in self.events if item.event_type == "card.improved"),
            worsens=sum(1 for item in self.events if item.event_type == "card.worsened"),
            maximum_tableau_cubes=self.maximum_tableau_cubes,
            blocker_count=len(self.blockers),
            player_actions=sum(1 for item in self.events if item.event_type == "player.card_activated"),
            rests=sum(1 for item in self.events if item.event_type == "player.rested"),
            assists=sum(1 for item in self.events if item.event_type == "assist.played"),
            fatigue_discarded=sum(player["fatigue_discarded"] for player in self.players.values()),
        )
        provenance = _provenance(self.snapshot, self.config, plugin, runner)
        identity = content_digest(
            {
                "provenance": provenance.model_dump(mode="json", exclude={"observed_at"}),
                "events": [item.model_dump(mode="json") for item in self.events],
                "final": final.model_dump(mode="json"),
            }
        )[:20]
        return SimulationRun(
            id=f"{self.snapshot.project_id}:simulation:{identity}",
            config=self.config,
            provenance=provenance,
            initial_state=self.initial,
            final_state=final,
            events=tuple(self.events),
            state_trace=tuple(self.state_trace),
            blockers=tuple(self.blockers),
            metrics=metrics,
        )


def _provenance(
    snapshot: DesignSnapshot,
    config: SimulationConfig,
    plugin: CrisisWardensSimulationPlugin,
    runner: str,
) -> SimulationProvenance:
    try:
        backend_version = version("playtrace")
    except PackageNotFoundError:
        backend_version = "source"
    return SimulationProvenance(
        snapshot_id=snapshot.id,
        snapshot_digest=snapshot_content_digest(snapshot),
        backend_version=backend_version,
        plugin_id=plugin.id,
        plugin_version=plugin.version,
        config_digest=content_digest(config.model_dump(mode="json")),
        player_policy=config.player_policy,
        seeds=(config.seed,),
        runner=runner,
    )


def _blocker(
    suffix: str,
    kind: BlockerKind,
    message: str,
    entity_ids: tuple[str, ...],
    *,
    assertion_ids: tuple[str, ...] = (),
    required_input: str | None = None,
    overridable: bool = False,
) -> SimulationBlocker:
    return SimulationBlocker(
        id=f"cw:blocker:{suffix}",
        kind=kind,
        message=message,
        entity_ids=entity_ids,
        assertion_ids=assertion_ids,
        required_input=required_input,
        overridable=overridable,
    )


def _fields(entity: DesignEntity) -> dict[str, Any]:
    value = entity.attributes.get("fields", {})
    return value if isinstance(value, dict) else {}


def _split_semicolon(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(";") if item.strip())


def _split_escalation(value: str) -> tuple[str, ...]:
    track = value.split(".", 1)[0]
    return tuple(item.strip() for item in track.split("→") if item.strip())
