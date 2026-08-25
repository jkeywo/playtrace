import pytest

from playtrace.adapters import SyntheticProjectAdapter
from playtrace.analysis import SnapshotAnalyzer
from playtrace.models import SourceMedium


@pytest.mark.parametrize(
    ("left_medium", "right_medium"),
    [
        (SourceMedium.DRIVE, SourceMedium.GIT),
        (SourceMedium.GIT, SourceMedium.SYNTHETIC),
        (SourceMedium.DRIVE, SourceMedium.MIXED),
    ],
)
def test_same_semantic_snapshot_has_same_analysis_regardless_of_source_medium(
    left_medium: SourceMedium,
    right_medium: SourceMedium,
) -> None:
    analyzer = SnapshotAnalyzer()
    left_snapshot = SyntheticProjectAdapter(left_medium).snapshot()
    right_snapshot = SyntheticProjectAdapter(right_medium).snapshot()

    left_report = analyzer.analyze(left_snapshot)
    right_report = analyzer.analyze(right_snapshot)

    assert left_snapshot.source_revisions[0].medium is not right_snapshot.source_revisions[0].medium
    assert left_snapshot.adapter_id != right_snapshot.adapter_id
    assert left_report.source_neutral_result() == right_report.source_neutral_result()

