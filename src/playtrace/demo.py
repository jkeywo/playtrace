"""Synthetic M0 acceptance data and review projection."""

from __future__ import annotations

from playtrace.adapters import SyntheticProjectAdapter
from playtrace.analysis import SnapshotAnalyzer
from playtrace.models import (
    EvidenceClaim,
    EvidenceLink,
    EvidenceRelation,
    Hypothesis,
    SourceMedium,
)


def build_demo_review(medium: SourceMedium = SourceMedium.SYNTHETIC) -> dict[str, object]:
    snapshot = SyntheticProjectAdapter(medium).snapshot()
    report = SnapshotAnalyzer().analyze(snapshot)
    claim = EvidenceClaim(
        id="synthetic:claim:pressure-needs-test",
        statement="The current pressure cadence needs player-count scaling tests.",
        scope=("synthetic:rule:pressure", "synthetic:question:scaling"),
        evidence=(
            EvidenceLink(
                evidence_id="synthetic:assertion:cadence",
                relation=EvidenceRelation.CONTEXT,
                rationale="The cadence is explicitly a working rule.",
            ),
            EvidenceLink(
                evidence_id="synthetic:assertion:scaling-question",
                relation=EvidenceRelation.SUPPORTS,
                rationale="The source explicitly records scaling as unresolved.",
            ),
        ),
    )
    hypothesis = Hypothesis(
        id="synthetic:hypothesis:pressure-scaling",
        statement="A fixed pressure event after every player turn produces materially different difficulty by player count.",
        scope=("synthetic:rule:pressure",),
        test_method="Compare outcome distributions under fixed player policies for one through four players.",
        supporting_outcomes=("Outcome distributions diverge beyond the agreed tolerance.",),
        contradicting_outcomes=("Outcome distributions remain within the agreed tolerance.",),
    )
    return {
        "snapshot": snapshot.model_dump(mode="json"),
        "analysis": report.model_dump(mode="json"),
        "claim": claim.model_dump(mode="json"),
        "hypothesis": hypothesis.model_dump(mode="json"),
    }

