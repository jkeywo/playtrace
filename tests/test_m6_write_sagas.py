from __future__ import annotations

import json
import sqlite3

import pytest

from playtrace.adapters import SyntheticProjectAdapter
from playtrace.docs_adapter import (
    replace_text_in_utf16_range,
    text_in_document_range,
    utf16_code_unit_length,
)
from playtrace.models import ChangeOperation, ChangeSet, SourceMedium
from playtrace.patches import (
    DocumentTextPatch,
    PatchConflictError,
    SagaExecutionError,
    SagaOperation,
    SagaStatus,
    SagaWriteOutcome,
    SemanticPatchExecutor,
    SemanticPatchSet,
    SheetValuesPatch,
    WriteSaga,
    WriteSagaJournal,
)
from playtrace.proposals import (
    AttemptStatus,
    ProposalBasis,
    ProposalOrchestrator,
    ProposalState,
    create_proposal,
    transition_proposal,
)
from playtrace.storage import PlayTraceStore, SCHEMA
from playtrace.workspace import (
    FixtureWorkspaceGateway,
    GoogleWorkspaceGateway,
)


class WritableSheetGateway(FixtureWorkspaceGateway):
    supports_canonical_writes = True

    def batch_update_sheet_values(self, file_id, data):
        for item in data:
            self.payload["spreadsheets"][file_id]["ranges"][item["range"]] = [
                list(row) for row in item["values"]
            ]
        return super().batch_update_sheet_values(file_id, data)


class RacingCompareGateway(WritableSheetGateway):
    def __init__(self, payload, *, race_on_compare: int) -> None:
        super().__init__(payload)
        self.compare_count = 0
        self.race_on_compare = race_on_compare

    def compare_and_batch_update_sheet_values(self, file_id, data, expected_data):
        self.compare_count += 1
        if self.compare_count == self.race_on_compare:
            target = expected_data[0]["range"]
            self.payload["spreadsheets"][file_id]["ranges"][target] = [["third-party"]]
        return super().compare_and_batch_update_sheet_values(
            file_id, data, expected_data
        )


def _sheet_payload(value: str = "old") -> dict:
    return {
        "files": {"sheet": {"version": "1"}},
        "documents": {},
        "spreadsheets": {
            "sheet": {"metadata": {}, "ranges": {"A1": [[value]]}},
        },
    }


def _sheet_patch(snapshot_id: str = "snapshot") -> SemanticPatchSet:
    return SemanticPatchSet(
        id="patch",
        base_snapshot_id=snapshot_id,
        sheet_patches=(
            SheetValuesPatch(
                source_id="source",
                file_id="sheet",
                required_version="1",
                range="A1",
                expected_values=(("old",),),
                replacement_values=(("new",),),
            ),
        ),
    )


def _approved_record():
    snapshot = SyntheticProjectAdapter(SourceMedium.SYNTHETIC).snapshot()
    record = create_proposal(
        id="proposal",
        project_id=snapshot.project_id,
        title="Durable change",
        question="Apply it?",
        rationale="Exercise crash reconciliation.",
        alternatives=("Keep", "Apply"),
        criteria=("Durable",),
        change_set=ChangeSet(
            id="change",
            intent_id="intent",
            base_snapshot_id=snapshot.id,
            operations=(
                ChangeOperation(
                    target_id="source", operation="replace", before="old", after="new"
                ),
            ),
            rationale_claim_ids=(),
            validation=("canonical write",),
        ),
        patch_set=_sheet_patch(snapshot.id),
        basis=ProposalBasis(snapshot=snapshot),
        created_by="designer",
    )
    record = transition_proposal(
        record, ProposalState.PENDING, actor="designer", rationale="Submit."
    )
    record = transition_proposal(
        record, ProposalState.APPROVED, actor="owner", rationale="Approve."
    )
    return snapshot, record


def _journal_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def test_base_schema_owns_write_saga_journal_without_optional_migrations() -> None:
    connection = _journal_connection()
    try:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'write_sagas'"
        ).fetchone()
        assert table["name"] == "write_sagas"
        journal = WriteSagaJournal(connection)
        journal.save(
            WriteSaga(
                id="attempt:saga",
                proposal_id="proposal",
                attempt_id="attempt",
                patch_set_id="patch",
                status=SagaStatus.PREPARED,
                operations=(),
            )
        )
        assert journal.load("attempt:saga").status is SagaStatus.PREPARED
    finally:
        connection.close()


def test_live_transactional_apply_without_journal_fails_before_any_write() -> None:
    gateway = WritableSheetGateway(_sheet_payload())
    with pytest.raises(ValueError, match="durable saga journal"):
        SemanticPatchExecutor(gateway).execute_transactional(
            _sheet_patch(), proposal_id="proposal", attempt_id="attempt"
        )
    assert gateway.writes == []
    assert gateway.payload["spreadsheets"]["sheet"]["ranges"]["A1"] == [["old"]]


def test_orphaned_committed_saga_reconciles_before_stale_basis_and_never_rewrites() -> None:
    snapshot, record = _approved_record()
    gateway = WritableSheetGateway(_sheet_payload("new"))
    connection = _journal_connection()
    try:
        journal = WriteSagaJournal(connection)
        journal.save(
            WriteSaga(
                id="proposal:attempt:1:saga",
                proposal_id="proposal",
                attempt_id="proposal:attempt:1",
                patch_set_id="patch",
                status=SagaStatus.COMMITTED,
                operations=(
                    SagaOperation(
                        sequence=1,
                        kind="sheet",
                        file_id="sheet",
                        patch=_sheet_patch(snapshot.id).sheet_patches[0].model_dump(
                            mode="json"
                        ),
                        before_image={"values": (("old",),)},
                        attempted=True,
                        applied=True,
                        verified=True,
                        write_outcome=SagaWriteOutcome.RETURNED,
                        response={"updated": True},
                    ),
                ),
            )
        )
        stale = snapshot.model_copy(update={"source_revision_ids": ("changed",)})
        reconciled = ProposalOrchestrator(
            SemanticPatchExecutor(gateway, saga_journal=journal), lambda: stale
        ).execute(record, stale, dry_run=False, actor="operator")
    finally:
        connection.close()

    assert reconciled.proposal.state is ProposalState.APPLIED
    assert reconciled.attempts[-1].status is AttemptStatus.APPLIED
    assert reconciled.attempts[-1].write_saga_id == "proposal:attempt:1:saga"
    assert "Reconciled" in reconciled.attempts[-1].message
    assert gateway.writes == []


@pytest.mark.parametrize("claimed", (SagaStatus.COMMITTED, SagaStatus.RECOVERED))
def test_invalid_terminal_saga_is_durably_reopened_for_recovery(claimed) -> None:
    connection = _journal_connection()
    try:
        operation = SagaOperation(
            sequence=1,
            kind="sheet",
            file_id="sheet",
            patch=_sheet_patch().sheet_patches[0].model_dump(mode="json"),
            before_image={"values": (("old",),)},
            attempted=True,
            write_outcome=SagaWriteOutcome.UNKNOWN,
        )
        payload = WriteSaga(
            id="broken:saga",
            proposal_id="proposal",
            attempt_id="broken",
            patch_set_id="patch",
            status=SagaStatus.APPLYING,
            operations=(operation,),
        ).model_dump(mode="json")
        payload["status"] = claimed.value
        with connection:
            connection.execute(
                "INSERT INTO write_sagas VALUES (?, ?, ?, ?, ?)",
                ("broken:saga", "proposal", "broken", claimed.value, json.dumps(payload)),
            )
        loaded = WriteSagaJournal(connection).load("broken:saga")
        stored = connection.execute(
            "SELECT status, payload FROM write_sagas WHERE id = 'broken:saga'"
        ).fetchone()
        assert loaded.status is SagaStatus.RECOVERY_FAILED
        assert stored["status"] == SagaStatus.RECOVERY_FAILED.value
        assert json.loads(stored["payload"])["status"] == SagaStatus.RECOVERY_FAILED.value
    finally:
        connection.close()


@pytest.mark.parametrize("claimed", (SagaStatus.COMMITTED, SagaStatus.RECOVERED))
@pytest.mark.parametrize(
    "outcome", (SagaWriteOutcome.UNKNOWN, SagaWriteOutcome.RETURNED)
)
def test_terminal_adoption_treats_transport_outcome_as_attempt_evidence(
    claimed, outcome
) -> None:
    connection = _journal_connection()
    try:
        operation = SagaOperation(
            sequence=1,
            kind="sheet",
            file_id="sheet",
            patch=_sheet_patch().sheet_patches[0].model_dump(mode="json"),
            before_image={"values": (("old",),)},
            attempted=False,
            write_outcome=outcome,
            compensated=False,
        )
        payload = WriteSaga(
            id="outcome-evidence:saga",
            proposal_id="proposal",
            attempt_id="outcome-evidence",
            patch_set_id="patch",
            status=SagaStatus.APPLYING,
            operations=(operation,),
        ).model_dump(mode="json")
        payload["status"] = claimed.value
        # Preserve the contradictory legacy/corrupt attempted flag rather than
        # letting normal model construction repair it before persistence.
        payload["operations"][0]["attempted"] = False
        with connection:
            connection.execute(
                "INSERT INTO write_sagas VALUES (?, ?, ?, ?, ?)",
                (
                    "outcome-evidence:saga",
                    "proposal",
                    "outcome-evidence",
                    claimed.value,
                    json.dumps(payload),
                ),
            )

        loaded = WriteSagaJournal(connection).load("outcome-evidence:saga")
        assert loaded.status is SagaStatus.RECOVERY_FAILED
        assert loaded.operations[0].attempted
        assert loaded.operations[0].write_outcome is outcome
        stored = connection.execute(
            "SELECT status, payload FROM write_sagas WHERE id = ?",
            ("outcome-evidence:saga",),
        ).fetchone()
        assert stored["status"] == SagaStatus.RECOVERY_FAILED.value
        assert json.loads(stored["payload"])["operations"][0]["attempted"] is True
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("claimed", "expected_status"),
    (
        (SagaStatus.RECOVERED, SagaStatus.RECOVERED),
        (SagaStatus.COMMITTED, SagaStatus.RECOVERY_FAILED),
    ),
)
def test_terminal_adoption_treats_compensation_as_attempt_evidence(
    claimed, expected_status
) -> None:
    connection = _journal_connection()
    try:
        operation = SagaOperation(
            sequence=1,
            kind="sheet",
            file_id="sheet",
            patch=_sheet_patch().sheet_patches[0].model_dump(mode="json"),
            before_image={"values": (("old",),)},
            attempted=False,
            write_outcome=SagaWriteOutcome.NOT_STARTED,
            compensated=True,
        )
        payload = WriteSaga(
            id="compensation-evidence:saga",
            proposal_id="proposal",
            attempt_id="compensation-evidence",
            patch_set_id="patch",
            status=SagaStatus.APPLYING,
            operations=(operation,),
        ).model_dump(mode="json")
        payload["status"] = claimed.value
        # Persist the contradictory legacy/corrupt state directly so adoption
        # has to reconcile compensation with the missing attempt evidence.
        payload["operations"][0]["attempted"] = False
        payload["operations"][0]["write_outcome"] = (
            SagaWriteOutcome.NOT_STARTED.value
        )
        with connection:
            connection.execute(
                "INSERT INTO write_sagas VALUES (?, ?, ?, ?, ?)",
                (
                    "compensation-evidence:saga",
                    "proposal",
                    "compensation-evidence",
                    claimed.value,
                    json.dumps(payload),
                ),
            )

        loaded = WriteSagaJournal(connection).load("compensation-evidence:saga")
        assert loaded.status is expected_status
        assert loaded.operations[0].attempted
        assert loaded.operations[0].write_outcome is SagaWriteOutcome.UNKNOWN
        assert loaded.operations[0].compensated
        stored = connection.execute(
            "SELECT status, payload FROM write_sagas WHERE id = ?",
            ("compensation-evidence:saga",),
        ).fetchone()
        stored_payload = json.loads(stored["payload"])
        assert stored["status"] == expected_status.value
        assert stored_payload["operations"][0]["attempted"] is True
        assert stored_payload["operations"][0]["write_outcome"] == "unknown"
    finally:
        connection.close()


def test_sheet_forward_compare_and_write_rejects_observation_race() -> None:
    gateway = RacingCompareGateway(_sheet_payload(), race_on_compare=1)

    with _journal_connection() as connection:
        with pytest.raises(SagaExecutionError, match="changed before compare-and-write"):
            SemanticPatchExecutor(gateway, saga_journal=WriteSagaJournal(connection)).execute_transactional(
                _sheet_patch(), proposal_id="proposal", attempt_id="attempt"
            )

    assert gateway.payload["spreadsheets"]["sheet"]["ranges"]["A1"] == [
        ["third-party"]
    ]
    assert gateway.writes == []


def test_sheet_compensation_compare_and_write_preserves_concurrent_edit() -> None:
    gateway = RacingCompareGateway(_sheet_payload(), race_on_compare=2)
    connection = _journal_connection()
    try:
        executor = SemanticPatchExecutor(
            gateway, saga_journal=WriteSagaJournal(connection)
        )
        result = executor.execute_transactional(
            _sheet_patch(), proposal_id="proposal", attempt_id="attempt"
        )
        recovered = executor.rollback(result.write_saga_id)
    finally:
        connection.close()

    assert recovered.status is SagaStatus.RECOVERY_FAILED
    assert not recovered.operations[0].compensated
    assert "changed before compare-and-write" in recovered.failures[-1]
    assert gateway.payload["spreadsheets"]["sheet"]["ranges"]["A1"] == [
        ["third-party"]
    ]
    assert len(gateway.writes) == 1


def test_google_sheets_transport_fails_closed_without_issuing_request(
    monkeypatch,
) -> None:
    gateway = GoogleWorkspaceGateway("token")
    requests: list[object] = []
    monkeypatch.setattr(gateway, "_request", lambda *args, **kwargs: requests.append(args))
    monkeypatch.setattr(gateway, "get_file", lambda _: {"version": "1"})
    monkeypatch.setattr(
        gateway,
        "get_spreadsheet",
        lambda file_id, ranges=(): {"metadata": {}, "ranges": {ranges[0]: [["old"]]}},
    )

    with pytest.raises(PatchConflictError, match="atomic compare-and-write"):
        with _journal_connection() as connection:
            SemanticPatchExecutor(gateway, saga_journal=WriteSagaJournal(connection)).execute_transactional(
                _sheet_patch(), proposal_id="proposal", attempt_id="attempt"
            )

    assert requests == []


def test_document_ranges_reject_missing_tabs_outside_extents_and_text_run_gaps() -> None:
    document = {
        "tabs": [
            {
                "tabProperties": {"tabId": "tab"},
                "body": {
                    "content": [
                        {
                            "paragraph": {
                                "elements": [
                                    {
                                        "startIndex": 1,
                                        "endIndex": 3,
                                        "textRun": {"content": "AB"},
                                    },
                                    {
                                        "startIndex": 4,
                                        "endIndex": 6,
                                        "textRun": {"content": "CD"},
                                    },
                                ]
                            }
                        }
                    ]
                },
            }
        ]
    }
    with pytest.raises(ValueError, match="was not found"):
        text_in_document_range(document, "missing", 1, 2)
    with pytest.raises(ValueError, match="outside"):
        text_in_document_range(document, "tab", 0, 1)
    with pytest.raises(ValueError, match="non-text boundary"):
        text_in_document_range(document, "tab", 2, 5)


def test_document_ranges_validate_provider_utf16_extents_and_surrogate_boundaries() -> None:
    text = "A😀B"
    document = {
        "tabs": [
            {
                "tabProperties": {"tabId": "tab"},
                "body": {
                    "content": [
                        {
                            "paragraph": {
                                "elements": [
                                    {
                                        "startIndex": 0,
                                        "endIndex": utf16_code_unit_length(text),
                                        "textRun": {"content": text},
                                    }
                                ]
                            }
                        }
                    ]
                },
            }
        ]
    }
    assert text_in_document_range(document, "tab", 1, 3) == "😀"
    with pytest.raises(ValueError, match="surrogate pair"):
        text_in_document_range(document, "tab", 1, 2)
    document["tabs"][0]["body"]["content"][0]["paragraph"]["elements"][0][
        "endIndex"
    ] += 1
    with pytest.raises(ValueError, match="extent"):
        text_in_document_range(document, "tab", 0, 1)


def test_document_patch_extent_is_measured_in_utf16_code_units() -> None:
    DocumentTextPatch(
        source_id="emoji",
        file_id="doc",
        required_revision_id="1",
        tab_id="tab",
        start_index=4,
        end_index=6,
        expected_text="😀",
        replacement_text="ok",
    )
    with pytest.raises(ValueError, match="UTF-16 extent"):
        DocumentTextPatch(
            source_id="emoji",
            file_id="doc",
            required_revision_id="1",
            tab_id="tab",
            start_index=4,
            end_index=5,
            expected_text="😀",
            replacement_text="ok",
        )


class FailingMultiSheetGateway(FixtureWorkspaceGateway):
    supports_canonical_writes = True

    def __init__(self, payload):
        super().__init__(payload)
        self.fail_file = "sheet-2"

    def batch_update_sheet_values(self, file_id, data):
        if file_id == self.fail_file:
            raise RuntimeError("provider failed")
        for item in data:
            self.payload["spreadsheets"][file_id]["ranges"][item["range"]] = [
                list(row) for row in item["values"]
            ]
        return super().batch_update_sheet_values(file_id, data)


def _durable_sheet_patch_set() -> SemanticPatchSet:
    return SemanticPatchSet(
        id="patch:durable",
        base_snapshot_id="snapshot",
        sheet_patches=(
            SheetValuesPatch(
                source_id="source",
                file_id="sheet-1",
                required_version="1",
                range="A1",
                expected_values=(("old",),),
                replacement_values=(("new",),),
            ),
        ),
    )


def test_write_saga_compensates_verified_operations_in_reverse(tmp_path) -> None:
    payload = {
        "files": {"sheet-1": {"version": "1"}, "sheet-2": {"version": "1"}},
        "documents": {},
        "spreadsheets": {
            "sheet-1": {"metadata": {}, "ranges": {"A1": [["old-1"]]}},
            "sheet-2": {"metadata": {}, "ranges": {"A1": [["old-2"]]}},
        },
    }
    gateway = FailingMultiSheetGateway(payload)
    patch_set = SemanticPatchSet(
        id="patch",
        base_snapshot_id="snapshot",
        sheet_patches=tuple(
            SheetValuesPatch(
                source_id=f"source-{index}",
                file_id=f"sheet-{index}",
                required_version="1",
                range="A1",
                expected_values=((f"old-{index}",),),
                replacement_values=((f"new-{index}",),),
            )
            for index in (1, 2)
        ),
    )
    with PlayTraceStore(tmp_path / "saga.db") as store:
        executor = SemanticPatchExecutor(
            gateway, saga_journal=WriteSagaJournal(store.connection)
        )
        with pytest.raises(SagaExecutionError) as caught:
            executor.execute_transactional(
                patch_set, proposal_id="proposal", attempt_id="attempt"
            )
        assert caught.value.saga.status is SagaStatus.RECOVERED
        assert all(item.compensated for item in caught.value.saga.operations)
        assert payload["spreadsheets"]["sheet-1"]["ranges"]["A1"] == [["old-1"]]


class AcceptedThenDisconnectedGateway(FailingMultiSheetGateway):
    disconnected = False

    def batch_update_sheet_values(self, file_id, data):
        response = super().batch_update_sheet_values(file_id, data)
        if not self.disconnected:
            self.disconnected = True
            raise ConnectionError("response outcome unknown")
        return response


def test_unknown_transport_outcome_is_inspected_and_compensated(tmp_path) -> None:
    payload = {
        "files": {"sheet-1": {"version": "1"}},
        "documents": {},
        "spreadsheets": {"sheet-1": {"metadata": {}, "ranges": {"A1": [["old"]]}}},
    }
    gateway = AcceptedThenDisconnectedGateway(payload)
    gateway.fail_file = None
    with PlayTraceStore(tmp_path / "unknown.db") as store:
        executor = SemanticPatchExecutor(
            gateway, saga_journal=WriteSagaJournal(store.connection)
        )
        with pytest.raises(SagaExecutionError) as caught:
            executor.execute_transactional(
                _durable_sheet_patch_set(), proposal_id="proposal", attempt_id="attempt"
            )
        operation = caught.value.saga.operations[0]
        assert operation.write_outcome is SagaWriteOutcome.UNKNOWN
        assert not operation.applied
        assert operation.compensated
        assert payload["spreadsheets"]["sheet-1"]["ranges"]["A1"] == [["old"]]


class MutableDocumentGateway(FixtureWorkspaceGateway):
    supports_canonical_writes = True

    def batch_update_document(self, file_id, requests, required_revision_id):
        document = self.payload["documents"][file_id]
        assert document["revisionId"] == required_revision_id
        element = document["tabs"][0]["body"]["content"][0]["paragraph"]["elements"][0]
        text = element["textRun"]["content"]
        for request in requests:
            if "deleteContentRange" in request:
                value = request["deleteContentRange"]["range"]
                text = replace_text_in_utf16_range(
                    text, value["startIndex"], value["endIndex"], ""
                )
            else:
                value = request["insertText"]
                index = value["location"]["index"]
                text = replace_text_in_utf16_range(text, index, index, value["text"])
        element["textRun"]["content"] = text
        element["endIndex"] = utf16_code_unit_length(text)
        document["revisionId"] = str(int(document["revisionId"]) + 1)
        self.writes.append(
            {
                "kind": "document",
                "file_id": file_id,
                "requests": requests,
                "required_revision_id": required_revision_id,
            }
        )
        return {"revisionId": document["revisionId"]}


def _document_payload(text: str) -> dict:
    return {
        "files": {"doc": {"version": "1"}},
        "spreadsheets": {},
        "documents": {
            "doc": {
                "revisionId": "1",
                "tabs": [
                    {
                        "tabProperties": {"tabId": "tab"},
                        "body": {
                            "content": [
                                {
                                    "paragraph": {
                                        "elements": [
                                            {
                                                "startIndex": 0,
                                                "endIndex": utf16_code_unit_length(text),
                                                "textRun": {"content": text},
                                            }
                                        ]
                                    }
                                }
                            ]
                        },
                    }
                ],
            }
        },
    }


@pytest.mark.parametrize("replacement", ("OLD right", "OLD-AND-LONG", "O"))
def test_document_saga_restores_exact_before_image_for_overlapping_and_unequal_text(
    tmp_path, replacement
) -> None:
    original = "left OLD right"
    payload = _document_payload(original)
    gateway = MutableDocumentGateway(payload)
    patch_set = SemanticPatchSet(
        id=f"patch:{replacement}",
        base_snapshot_id="snapshot",
        document_patches=(
            DocumentTextPatch(
                source_id="source",
                file_id="doc",
                required_revision_id="1",
                tab_id="tab",
                start_index=5,
                end_index=8,
                expected_text="OLD",
                replacement_text=replacement,
            ),
        ),
    )
    with PlayTraceStore(tmp_path / "document-saga.db") as store:
        journal = WriteSagaJournal(store.connection)
        executor = SemanticPatchExecutor(gateway, saga_journal=journal)
        result = executor.execute_transactional(
            patch_set, proposal_id="proposal", attempt_id="attempt"
        )
        applied = journal.load(result.write_saga_id)
        image = applied.operations[0].before_image
        assert image["image_version"] == 1
        assert image["before_text"] == original
        assert image["after_text"] == f"left {replacement} right"
        restored = executor.rollback(result.write_saga_id)
        assert restored.status is SagaStatus.RECOVERED
        assert payload["documents"]["doc"]["tabs"][0]["body"]["content"][0][
            "paragraph"
        ]["elements"][0]["textRun"]["content"] == original


def _two_document_patches(revision: str = "1") -> SemanticPatchSet:
    return SemanticPatchSet(
        id="two-document-patches",
        base_snapshot_id="snapshot",
        document_patches=(
            DocumentTextPatch(
                source_id="first",
                file_id="doc",
                required_revision_id=revision,
                tab_id="tab",
                start_index=3,
                end_index=7,
                expected_text="old1",
                replacement_text="🔥",
            ),
            DocumentTextPatch(
                source_id="second",
                file_id="doc",
                required_revision_id=revision,
                tab_id="tab",
                start_index=11,
                end_index=15,
                expected_text="old2",
                replacement_text="TWO",
            ),
        ),
    )


def test_same_document_patches_share_one_revision_guarded_batch_and_rollback(tmp_path) -> None:
    original = "AA old1 BB old2 CC"
    payload = _document_payload(original)
    gateway = MutableDocumentGateway(payload)
    with PlayTraceStore(tmp_path / "document-group.db") as store:
        journal = WriteSagaJournal(store.connection)
        executor = SemanticPatchExecutor(gateway, saga_journal=journal)
        result = executor.execute_transactional(
            _two_document_patches(), proposal_id="proposal", attempt_id="group"
        )
        applied = journal.load(result.write_saga_id)
        assert result.applied_operations == 2
        assert len(applied.operations) == 1
        assert applied.operations[0].kind == "document_group"
        assert len(gateway.writes) == 1
        delete_starts = [
            item["deleteContentRange"]["range"]["startIndex"]
            for item in gateway.writes[0]["requests"]
            if "deleteContentRange" in item
        ]
        assert delete_starts == [11, 3]
        assert payload["documents"]["doc"]["tabs"][0]["body"]["content"][0][
            "paragraph"
        ]["elements"][0]["textRun"]["content"] == "AA 🔥 BB TWO CC"
        restored = executor.rollback(result.write_saga_id)
        assert restored.status is SagaStatus.RECOVERED
        assert payload["documents"]["doc"]["tabs"][0]["body"]["content"][0][
            "paragraph"
        ]["elements"][0]["textRun"]["content"] == original


def _store_legacy_saga(
    journal: WriteSagaJournal,
    saga: WriteSaga,
    status: SagaStatus,
) -> None:
    payload = saga.model_dump(mode="json")
    payload["status"] = status.value
    for operation in payload["operations"]:
        operation.pop("attempted", None)
        operation.pop("write_outcome", None)
        operation["compensated"] = False
    with journal.connection:
        journal.connection.execute(
            "INSERT OR REPLACE INTO write_sagas VALUES (?, ?, ?, ?, ?)",
            (
                saga.id,
                saga.proposal_id,
                saga.attempt_id,
                status.value,
                json.dumps(payload),
            ),
        )


def test_legacy_applied_document_journal_is_inspected_and_compensated(tmp_path) -> None:
    original = "left OLD right"
    payload = _document_payload(original)
    gateway = MutableDocumentGateway(payload)
    patch_set = SemanticPatchSet(
        id="legacy:document",
        base_snapshot_id="snapshot",
        document_patches=(
            DocumentTextPatch(
                source_id="source",
                file_id="doc",
                required_revision_id="1",
                tab_id="tab",
                start_index=5,
                end_index=8,
                expected_text="OLD",
                replacement_text="NEW",
            ),
        ),
    )
    with PlayTraceStore(tmp_path / "legacy-document.db") as store:
        journal = WriteSagaJournal(store.connection)
        executor = SemanticPatchExecutor(gateway, saga_journal=journal)
        result = executor.execute_transactional(
            patch_set, proposal_id="proposal", attempt_id="legacy-doc"
        )
        _store_legacy_saga(journal, journal.load(result.write_saga_id), SagaStatus.APPLIED)
        legacy = journal.load(result.write_saga_id)
        assert legacy.operations[0].attempted
        assert legacy.operations[0].write_outcome is SagaWriteOutcome.RETURNED
        recovered = executor.recover(result.write_saga_id)
        assert recovered.status is SagaStatus.RECOVERED
        assert recovered.operations[0].compensated
        assert payload["documents"]["doc"]["tabs"][0]["body"]["content"][0][
            "paragraph"
        ]["elements"][0]["textRun"]["content"] == original
