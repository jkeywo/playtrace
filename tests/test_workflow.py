import pytest

from playtrace.workflow import WorkflowRun, WorkflowStage


def test_explicit_workflow_accepts_declared_transition() -> None:
    run = WorkflowRun(id="run:1", intent_id="intent:1", snapshot_id="snapshot:1")

    updated = run.transition(WorkflowStage.INTENT, "Intent captured")

    assert run.stage is WorkflowStage.SNAPSHOT
    assert updated.stage is WorkflowStage.INTENT
    assert updated.history[-1].note == "Intent captured"


def test_explicit_workflow_rejects_skipping_review_graph() -> None:
    run = WorkflowRun(id="run:1", intent_id="intent:1", snapshot_id="snapshot:1")

    with pytest.raises(ValueError, match="invalid workflow transition"):
        run.transition(WorkflowStage.APPLY)

