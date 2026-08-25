"""Inspectable danger and dramatic-flow measurements for simulation traces."""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean
from typing import Iterable

from playtrace.models import StrictModel
from playtrace.simulation import SimulationRun, SimulationStatus, TabletopSimulationState


class DangerComponents(StrictModel):
    escalation: float
    thresholds: float
    imminent_escalation: float
    fatigue: float


class TensionSample(StrictModel):
    sequence: int
    turn: int
    event_type: str
    danger: float
    components: DangerComponents


class FlowTargets(StrictModel):
    meaningful_change: float = 4.0
    minimum_range: float = 35.0
    finish_danger_low: float = 70.0
    finish_danger_high: float = 95.0
    late_climax_start: float = 0.70


class TensionAnalysis(StrictModel):
    run_id: str
    combination: str
    seed: int
    status: SimulationStatus
    samples: tuple[TensionSample, ...]
    start_danger: float
    finish_danger: float
    peak_danger: float
    peak_progress: float
    danger_range: float
    trend: float
    meaningful_rises: int
    meaningful_falls: int
    reversals: int
    excitement_score: float
    upward_trend: bool
    has_rises_and_falls: bool
    late_climax: bool
    near_loss_finish: bool


class CombinationSummary(StrictModel):
    combination: str
    runs: int
    win_rate: float
    mean_excitement: float
    mean_finish_danger: float
    mean_peak_danger: float
    mean_trend: float
    target_pass_rate: float


class TensionBatch(StrictModel):
    id: str
    ruleset_id: str | None
    analyses: tuple[TensionAnalysis, ...]
    combinations: tuple[CombinationSummary, ...]
    mean_excitement: float
    combination_excitement_spread: float


class CombinationDelta(StrictModel):
    combination: str
    paired_runs: int
    excitement_delta: float
    finish_danger_delta: float
    target_pass_rate_delta: float


class ABComparison(StrictModel):
    baseline_id: str
    candidate_id: str
    paired_runs: int
    mean_excitement_delta: float
    candidate_better_rate: float
    deltas: tuple[CombinationDelta, ...]
    baseline_combination_spread: float
    candidate_combination_spread: float


_THRESHOLDS = {"SCD-001": 4, "SCD-002": 4, "SCD-003": 4, "SCD-004": 4, "SCD-008": 4}
_ESCALATION = {"rising": 10.0, "flooding": 38.0, "emergency": 68.0, "catastrophic": 100.0}


def danger_for_state(state: TabletopSimulationState) -> tuple[float, DangerComponents]:
    """Return 0–100 proximity to catastrophic loss from visible state facts."""

    escalation = _ESCALATION.get(state.escalation.lower(), 0.0)
    ratios = []
    for card_id, cubes in state.tableau.items():
        threshold = _THRESHOLDS.get(card_id)
        if threshold:
            if card_id == "SCD-001" and state.escalation.lower() == "emergency":
                threshold = 3
            ratios.append(min(1.0, cubes / threshold))
    thresholds = 0.0
    if ratios:
        thresholds = 100.0 * (0.65 * max(ratios) + 0.35 * fmean(ratios))

    escalation_cards = sum(card == "SCD-012" for card in state.deck)
    steps_left = {"rising": 3, "flooding": 2, "emergency": 1, "catastrophic": 0}.get(
        state.escalation.lower(), 3
    )
    imminent = 100.0 if steps_left == 0 else min(100.0, 100.0 * escalation_cards / steps_left)

    fatigue_values = []
    for player in state.players.values():
        original_capacity = len(player.deck) + len(player.hand) + len(player.discard) + player.fatigue_discarded
        if original_capacity:
            fatigue_values.append(100.0 * player.fatigue_discarded / original_capacity)
    fatigue = fmean(fatigue_values) if fatigue_values else 0.0

    components = DangerComponents(
        escalation=round(escalation, 2),
        thresholds=round(thresholds, 2),
        imminent_escalation=round(imminent, 2),
        fatigue=round(fatigue, 2),
    )
    # Escalation is a floor, not one independent risk among several: Emergency is
    # already one step from Catastrophic. Other pressures consume the safety
    # margin that remains above that floor.
    remaining_safety = 100.0 - escalation
    danger = escalation + remaining_safety * (
        0.65 * thresholds / 100.0
        + 0.20 * imminent / 100.0
        + 0.15 * fatigue / 100.0
    )
    if state.status is SimulationStatus.LOST or state.escalation.lower() == "catastrophic":
        danger = 100.0
    return round(min(100.0, danger), 2), components


def analyze_tension(run: SimulationRun, targets: FlowTargets | None = None) -> TensionAnalysis:
    targets = targets or FlowTargets()
    meaningful_types = {"scenario.setup", "turn.completed", "objective.completed", "objective.failed"}
    event_by_sequence = {event.sequence: event for event in run.events}
    selected = [
        checkpoint
        for checkpoint in run.state_trace
        if event_by_sequence[checkpoint.sequence].event_type in meaningful_types
    ]
    if not selected and run.state_trace:
        selected = [run.state_trace[0], run.state_trace[-1]]
    samples = []
    for checkpoint in selected:
        event = event_by_sequence[checkpoint.sequence]
        danger, components = danger_for_state(checkpoint.state)
        samples.append(
            TensionSample(
                sequence=checkpoint.sequence,
                turn=checkpoint.state.turn,
                event_type=event.event_type,
                danger=danger,
                components=components,
            )
        )
    if not samples:
        raise ValueError("a tension analysis requires at least one state checkpoint")

    values = [sample.danger for sample in samples]
    deltas = [b - a for a, b in zip(values, values[1:])]
    signs = [1 if delta >= targets.meaningful_change else -1 if delta <= -targets.meaningful_change else 0 for delta in deltas]
    significant = [sign for sign in signs if sign]
    rises = significant.count(1)
    falls = significant.count(-1)
    reversals = sum(a != b for a, b in zip(significant, significant[1:]))
    peak = max(values)
    peak_index = max(index for index, value in enumerate(values) if value == peak)
    progress = peak_index / max(1, len(values) - 1)
    trend = _linear_trend(values)
    danger_range = peak - min(values)
    near_finish = targets.finish_danger_low <= values[-1] <= targets.finish_danger_high
    late = progress >= targets.late_climax_start
    sawtooth = rises >= 2 and falls >= 1 and reversals >= 2
    upward = trend > 0 and values[-1] > values[0]

    # Five equally visible design goals, each worth 20 points.
    range_score = min(1.0, danger_range / targets.minimum_range)
    trend_score = min(1.0, max(0.0, trend) / 5.0)
    swing_score = min(1.0, min(rises / 2.0, falls / 1.0, reversals / 2.0))
    climax_score = min(1.0, max(0.0, (progress - 0.45) / 0.35))
    finish_score = _band_score(values[-1], targets.finish_danger_low, targets.finish_danger_high)
    excitement = 20.0 * sum((range_score, trend_score, swing_score, climax_score, finish_score))

    return TensionAnalysis(
        run_id=run.id,
        combination="+".join(run.config.character_ids),
        seed=run.config.seed,
        status=run.final_state.status if run.final_state else SimulationStatus.BLOCKED,
        samples=tuple(samples),
        start_danger=values[0],
        finish_danger=values[-1],
        peak_danger=peak,
        peak_progress=round(progress, 4),
        danger_range=round(danger_range, 2),
        trend=round(trend, 2),
        meaningful_rises=rises,
        meaningful_falls=falls,
        reversals=reversals,
        excitement_score=round(excitement, 2),
        upward_trend=upward,
        has_rises_and_falls=sawtooth,
        late_climax=late,
        near_loss_finish=near_finish,
    )


def build_batch(batch_id: str, runs: Iterable[SimulationRun]) -> TensionBatch:
    runs = tuple(runs)
    analyses = tuple(analyze_tension(run) for run in runs)
    grouped: dict[str, list[TensionAnalysis]] = defaultdict(list)
    for analysis in analyses:
        grouped[analysis.combination].append(analysis)
    summaries = tuple(_summarize(name, items) for name, items in sorted(grouped.items()))
    means = [item.mean_excitement for item in summaries]
    return TensionBatch(
        id=batch_id,
        ruleset_id=runs[0].config.ruleset_id if runs else None,
        analyses=analyses,
        combinations=summaries,
        mean_excitement=round(fmean(item.excitement_score for item in analyses), 2),
        combination_excitement_spread=round(max(means) - min(means), 2) if means else 0.0,
    )


def compare_batches(baseline: TensionBatch, candidate: TensionBatch) -> ABComparison:
    base = {(item.combination, item.seed): item for item in baseline.analyses}
    cand = {(item.combination, item.seed): item for item in candidate.analyses}
    keys = sorted(base.keys() & cand.keys())
    if not keys:
        raise ValueError("A/B batches have no matching combination-and-seed pairs")
    grouped: dict[str, list[tuple[TensionAnalysis, TensionAnalysis]]] = defaultdict(list)
    for key in keys:
        grouped[key[0]].append((base[key], cand[key]))
    deltas = []
    for combination, pairs in sorted(grouped.items()):
        deltas.append(
            CombinationDelta(
                combination=combination,
                paired_runs=len(pairs),
                excitement_delta=round(fmean(b.excitement_score - a.excitement_score for a, b in pairs), 2),
                finish_danger_delta=round(fmean(b.finish_danger - a.finish_danger for a, b in pairs), 2),
                target_pass_rate_delta=round(
                    fmean(float(b.upward_trend and b.has_rises_and_falls and b.late_climax and b.near_loss_finish) - float(a.upward_trend and a.has_rises_and_falls and a.late_climax and a.near_loss_finish) for a, b in pairs),
                    4,
                ),
            )
        )
    paired = [(base[key], cand[key]) for key in keys]
    return ABComparison(
        baseline_id=baseline.id,
        candidate_id=candidate.id,
        paired_runs=len(paired),
        mean_excitement_delta=round(fmean(b.excitement_score - a.excitement_score for a, b in paired), 2),
        candidate_better_rate=round(fmean(float(b.excitement_score > a.excitement_score) for a, b in paired), 4),
        deltas=tuple(deltas),
        baseline_combination_spread=baseline.combination_excitement_spread,
        candidate_combination_spread=candidate.combination_excitement_spread,
    )


def _summarize(combination: str, items: list[TensionAnalysis]) -> CombinationSummary:
    passed = [item.upward_trend and item.has_rises_and_falls and item.late_climax and item.near_loss_finish for item in items]
    return CombinationSummary(
        combination=combination,
        runs=len(items),
        win_rate=round(fmean(item.status is SimulationStatus.WON for item in items), 4),
        mean_excitement=round(fmean(item.excitement_score for item in items), 2),
        mean_finish_danger=round(fmean(item.finish_danger for item in items), 2),
        mean_peak_danger=round(fmean(item.peak_danger for item in items), 2),
        mean_trend=round(fmean(item.trend for item in items), 2),
        target_pass_rate=round(fmean(passed), 4),
    )


def _linear_trend(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    xs = list(range(len(values)))
    x_mean, y_mean = fmean(xs), fmean(values)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values)) / denominator


def _band_score(value: float, low: float, high: float) -> float:
    if low <= value <= high:
        return 1.0
    if value < low:
        return max(0.0, value / low)
    return max(0.0, (100.0 - value) / (100.0 - high))
