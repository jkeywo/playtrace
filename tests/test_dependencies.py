import pytest

from playtrace.dependencies import DependencyCycleError, DependencyGraph
from playtrace.models import DependencyEdge


def test_dependency_graph_propagates_affected_state() -> None:
    graph = DependencyGraph(
        (
            DependencyEdge(upstream_id="rule:a", downstream_id="claim:b"),
            DependencyEdge(upstream_id="claim:b", downstream_id="test:c"),
        )
    )

    assert graph.affected_by(("rule:a",)) == ("claim:b", "rule:a", "test:c")


def test_dependency_graph_rejects_cycles() -> None:
    graph = DependencyGraph((DependencyEdge(upstream_id="a", downstream_id="b"),))

    with pytest.raises(DependencyCycleError):
        graph.add(DependencyEdge(upstream_id="b", downstream_id="a"))

