from __future__ import annotations

import sqlite3

import pytest

from playtrace.docs_adapter import replace_text_in_utf16_range, utf16_code_unit_length
from playtrace.models import ChangeOperation, ChangeSet, DecisionState
from playtrace.patches import (
    DocumentTextPatch,
    SemanticPatchExecutor,
    SemanticPatchSet,
    WriteSagaJournal,
)
from playtrace.plugins.crisis_wardens import CrisisWardensSimulationPlugin
from playtrace.proposals import (
    ProposalBasis,
    ProposalOrchestrator,
    ProposalState,
    create_proposal,
    transition_proposal,
)
from playtrace.regressions import (
    BehavioralRegressionCase,
    BehavioralRegressionRunner,
    BehavioralRegressionSuite,
    RegressionCaseKind,
    RegressionExpectation,
    accept_regression_case,
    stable_run_digest,
)
from playtrace.simulation import InProcessSimulationRunner, SimulationConfig, snapshot_content_digest
from playtrace.storage import PlayTraceStore, SCHEMA
from playtrace.workspace import FixtureWorkspaceGateway
from test_simulation import _snapshot


class Registry:
    def create(self, plugin_id):
        assert plugin_id == CrisisWardensSimulationPlugin.id
        return CrisisWardensSimulationPlugin()


def test_approved_and_adversarial_cases_run_deterministically() -> None:
    snapshot = _snapshot()
    config = SimulationConfig(
        id="regression:pass",
        scenario_id="SCN-001",
        character_ids=("CHR-001",),
        max_turns=1,
    )
    original = InProcessSimulationRunner().run(
        CrisisWardensSimulationPlugin(), snapshot, config
    )
    status = original.final_state.status.value if original.final_state else None
    proposed_behavior = _case(
        "case:approved", RegressionCaseKind.APPROVED_BEHAVIOR, snapshot, config,
        RegressionExpectation(status=status)
    )
    approved = accept_regression_case(
        proposed_behavior,
        actor="designer",
        rationale="The applied design decision established this expected behavior.",
        applied_proposal=_applied_record(snapshot),
    )
    proposed_adversarial = _case(
        "case:adversarial", RegressionCaseKind.ADVERSARIAL, snapshot, config,
        RegressionExpectation(status=status)
    )
    adversarial = accept_regression_case(
        proposed_adversarial,
        actor="maintainer",
        rationale="This boundary must remain rejected deterministically.",
    )
    suite = BehavioralRegressionSuite(
        id="suite:m5", project_id=snapshot.project_id, cases=(approved, adversarial)
    )

    first = BehavioralRegressionRunner(Registry()).run(suite, snapshot)
    second = BehavioralRegressionRunner(Registry()).run(suite, snapshot)

    assert first.passed
    assert first.digest == second.digest
    assert first.case_results[0].run_digest == stable_run_digest(original)


def test_rejected_case_is_excluded_from_suite() -> None:
    snapshot = _snapshot()
    case = _case(
        "case:rejected",
        RegressionCaseKind.ADVERSARIAL,
        snapshot,
        SimulationConfig(id="r", scenario_id="SCN-001", character_ids=("CHR-001",)),
        RegressionExpectation(),
    )
    case = BehavioralRegressionCase.model_validate(
        case.model_dump(mode="json") | {"state": DecisionState.REJECTED}
    )
    with pytest.raises(ValueError, match="only accepted"):
        BehavioralRegressionSuite(id="suite:bad", project_id=snapshot.project_id, cases=(case,))


def test_regression_reports_behavioral_mismatch() -> None:
    snapshot = _snapshot()
    case = accept_regression_case(_case(
        "case:failure",
        RegressionCaseKind.ADVERSARIAL,
        snapshot,
        SimulationConfig(
            id="regression:failure", scenario_id="SCN-001", character_ids=("CHR-001",), max_turns=1
        ),
        RegressionExpectation(status="won", event_type_counts={"never.happens": 1}),
    ), actor="maintainer", rationale="Exercise mismatch reporting.")
    result = BehavioralRegressionRunner(Registry()).run(
        BehavioralRegressionSuite(id="suite:failure", project_id=snapshot.project_id, cases=(case,)),
        snapshot,
    )
    assert not result.passed
    assert len(result.case_results[0].failures) == 2


def test_approved_behavior_requires_verified_applied_proposal_in_storage(tmp_path) -> None:
    snapshot = _snapshot()
    proposed = _case(
        "case:stored",
        RegressionCaseKind.APPROVED_BEHAVIOR,
        snapshot,
        SimulationConfig(id="stored", scenario_id="SCN-001", character_ids=("CHR-001",)),
        RegressionExpectation(),
    )
    initial, approved, applied = _proposal_lifecycle(snapshot)
    with PlayTraceStore(tmp_path / "cases.db") as store:
        store.save_proposal(initial)
        store.save_proposal(approved)
        store.save_proposal(applied)
        store.save_regression_case(proposed)
        accepted = store.accept_regression_case(
            proposed,
            actor="designer",
            rationale="Application passed and established this behavior.",
            proposal_id=applied.proposal.id,
        )
        assert store.load_regression_case(proposed.id) == accepted

    fabricated = accepted.model_copy(
        update={
            "source_proposal_id": "proposal:missing",
        }
    )
    with PlayTraceStore(tmp_path / "fabricated.db") as store:
        with pytest.raises(ValueError, match="loaded applied proposal"):
            store.save_regression_case(fabricated)


def test_adversarial_case_requires_nonblank_rationale() -> None:
    snapshot = _snapshot()
    with pytest.raises(ValueError, match="nonblank rationale"):
        _case(
            "case:no-rationale",
            RegressionCaseKind.ADVERSARIAL,
            snapshot,
            SimulationConfig(id="bad", scenario_id="SCN-001", character_ids=("CHR-001",)),
            RegressionExpectation(),
            rationale="   ",
        )


def test_behavior_case_construction_is_proposed_and_source_id_cannot_self_accept() -> None:
    snapshot = _snapshot()
    proposed = _case(
        "case:normal-construction",
        RegressionCaseKind.APPROVED_BEHAVIOR,
        snapshot,
        SimulationConfig(id="normal", scenario_id="SCN-001", character_ids=("CHR-001",)),
        RegressionExpectation(),
    )
    assert proposed.state is DecisionState.PROPOSED

    payload = proposed.model_dump(mode="json") | {
        "state": DecisionState.ACCEPTED,
        "source_proposal_id": "proposal:claimed",
    }
    with pytest.raises(ValueError, match="explicit approval"):
        BehavioralRegressionCase.model_validate(payload)


def _case(id, kind, snapshot, config, expectation, rationale="Protect a known behavior."):
    return BehavioralRegressionCase(
        id=id,
        title=id,
        project_id=snapshot.project_id,
        plugin_id=CrisisWardensSimulationPlugin.id,
        snapshot_id=snapshot.id,
        snapshot_digest=snapshot_content_digest(snapshot),
        config=config,
        expectation=expectation,
        kind=kind,
        rationale=rationale,
    )


class _WritableGateway(FixtureWorkspaceGateway):
    supports_canonical_writes = True

    def batch_update_document(self, file_id, requests, required_revision_id):
        document = self.payload["documents"][file_id]
        assert document["revisionId"] == required_revision_id
        element = document["tabs"][0]["body"]["content"][0]["paragraph"]["elements"][0]
        base = int(element["startIndex"])
        text = element["textRun"]["content"]
        for request in requests:
            if "deleteContentRange" in request:
                target = request["deleteContentRange"]["range"]
                text = replace_text_in_utf16_range(
                    text, int(target["startIndex"]) - base, int(target["endIndex"]) - base, ""
                )
            else:
                insertion = request["insertText"]
                relative = int(insertion["location"]["index"]) - base
                text = replace_text_in_utf16_range(
                    text, relative, relative, str(insertion["text"])
                )
        element["textRun"]["content"] = text
        element["endIndex"] = base + utf16_code_unit_length(text)
        document["revisionId"] = f"{required_revision_id}:next"
        return super().batch_update_document(file_id, requests, required_revision_id)


def _executor(gateway):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return SemanticPatchExecutor(gateway, saga_journal=WriteSagaJournal(connection))


def _applied_record(snapshot):
    return _proposal_lifecycle(snapshot)[2]


def _proposal_lifecycle(snapshot):
    change_set = ChangeSet(
        id="change:regression-origin",
        intent_id="intent:regression-origin",
        base_snapshot_id=snapshot.id,
        operations=(
            ChangeOperation(
                target_id="source:rules",
                operation="replace_text",
                before="Old text",
                after="New text",
            ),
        ),
        rationale_claim_ids=(),
        validation=("regressions",),
    )
    patch_set = SemanticPatchSet(
        id="patch:regression-origin",
        base_snapshot_id=snapshot.id,
        document_patches=(
            DocumentTextPatch(
                source_id="source:rules",
                file_id="doc-1",
                required_revision_id="r1",
                tab_id="t.0",
                start_index=1,
                end_index=9,
                expected_text="Old text",
                replacement_text="New text",
            ),
        ),
    )
    initial = create_proposal(
        id="proposal:regression-origin",
        project_id=snapshot.project_id,
        title="Establish behavior",
        question="Should this behavior be established?",
        rationale="The test needs a real applied-decision origin.",
        alternatives=("Keep old", "Apply new"),
        criteria=("Regression passes",),
        change_set=change_set,
        patch_set=patch_set,
        basis=ProposalBasis(snapshot=snapshot),
        created_by="designer",
    )
    approved = transition_proposal(
        transition_proposal(
            initial,
            ProposalState.PENDING,
            actor="designer",
            rationale="Ready.",
        ),
        ProposalState.APPROVED,
        actor="owner",
        rationale="Approved.",
    )
    gateway = _WritableGateway(
        {
            "files": {"doc-1": {"id": "doc-1", "version": "1"}},
            "documents": {
                "doc-1": {
                    "documentId": "doc-1",
                    "revisionId": "r1",
                    "tabs": [
                        {
                            "tabProperties": {"tabId": "t.0", "title": "Rules"},
                            "body": {
                                "content": [
                                    {
                                        "startIndex": 1,
                                        "endIndex": 10,
                                        "paragraph": {
                                            "elements": [
                                                {
                                                    "startIndex": 1,
                                                    "endIndex": 10,
                                                    "textRun": {"content": "Old text\n"},
                                                }
                                            ]
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                }
            },
            "spreadsheets": {},
        }
    )
    applied = ProposalOrchestrator(
        _executor(gateway), lambda: snapshot
    ).execute(approved, snapshot, dry_run=False, actor="operator")
    assert applied.proposal.state is ProposalState.APPLIED
    return initial, approved, applied
