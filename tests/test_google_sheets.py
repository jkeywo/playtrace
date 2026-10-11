from __future__ import annotations

import argparse
import copy

import pytest

from playtrace.cli import _workspace_gateway
from playtrace.manifest import ManifestSource, ProjectManifest, SheetTableContract
from playtrace.patches import (
    PatchConflictError, SagaExecutionError, SagaStatus, SemanticPatchExecutor,
    SemanticPatchSet, SheetValuesPatch, WriteSagaJournal,
)
from playtrace.sheet_ranges import literal_matrix, parse_sheet_range
from playtrace.sheets_adapter import extract_sheet_records
from playtrace.storage import PlayTraceStore
from playtrace.tabletop import TabletopProjectAdapter
from playtrace.workspace import FixtureWorkspaceGateway, GoogleWorkspaceGateway
from playtrace.workspace_sync import WorkspaceCache, WorkspaceSynchronizer


class SheetsAPI(GoogleWorkspaceGateway):
    """Exercise production transport request/response shapes without credentials."""

    def __init__(self, values=None, *, mode="single-editor"):
        super().__init__("test-token", sheet_write_mode=mode)
        self.values = [["old"]] if values is None else values
        self.requests = []
        self.lose_response = False
        self.after_write = None

    def _request(self, method, url, query=None, body=None):
        self.requests.append((method, url, query, body))
        if "/drive/" in url:
            return {"version": "1"}
        if method == "GET":
            assert dict(query)["valueRenderOption"] == "FORMULA"
            return {"valueRanges": [{"range": "'Cards'!B4:C5", "values": copy.deepcopy(self.values)}]}
        assert body["valueInputOption"] == "RAW"
        self.values = [list(row) for row in body["data"][0]["values"]]
        if self.after_write is not None:
            self.values = self.after_write
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("response lost after provider accepted write")
        return {"totalUpdatedCells": 4}


def patch(expected=(("old",),), replacement=(("new",),), range_name="Cards!B4:C5"):
    return SemanticPatchSet(
        id="patch", base_snapshot_id="snapshot",
        sheet_patches=(SheetValuesPatch(
            source_id="cards", file_id="sheet", required_version="1", range=range_name,
            expected_values=expected, replacement_values=replacement,
        ),),
    )


def executor(gateway, store):
    return SemanticPatchExecutor(gateway, saga_journal=WriteSagaJournal(store.connection))


def test_live_sheet_literals_blanks_journal_and_recovery(tmp_path):
    gateway = SheetsAPI()
    with PlayTraceStore(tmp_path / "sheets.db") as store:
        runner = executor(gateway, store)
        result = runner.execute_transactional(
            patch(replacement=((3, False), ("", "text"))), proposal_id="p", attempt_id="a"
        )
        saga = runner.saga_journal.load(result.write_saga_id)
        assert saga.status == SagaStatus.APPLIED
        assert saga.operations[0].sheet_write_mode == "single-editor"
        assert saga.operations[0].response["sheet_write_mode"] == "single-editor"
        assert gateway.values == [[3, False], ["", "text"]]
        recovered = runner.rollback(result.write_saga_id)
        assert recovered.status == SagaStatus.RECOVERED
        assert gateway.values == [["old", ""], ["", ""]]


def test_unknown_google_write_is_observed_and_restored(tmp_path):
    gateway = SheetsAPI()
    gateway.lose_response = True
    with PlayTraceStore(tmp_path / "sheets.db") as store:
        with pytest.raises(SagaExecutionError) as caught:
            executor(gateway, store).execute_transactional(patch(), proposal_id="p", attempt_id="a")
        assert caught.value.saga.status == SagaStatus.RECOVERED
        assert gateway.values == [["old", ""], ["", ""]]


def test_observed_external_change_is_never_compensated(tmp_path):
    gateway = SheetsAPI()
    gateway.after_write = [["someone else's edit"]]
    with PlayTraceStore(tmp_path / "sheets.db") as store:
        with pytest.raises(SagaExecutionError) as caught:
            executor(gateway, store).execute_transactional(patch(), proposal_id="p", attempt_id="a")
        assert caught.value.saga.status == SagaStatus.RECOVERY_FAILED
        assert gateway.values == gateway.after_write
        assert len([r for r in gateway.requests if r[0] == "POST"]) == 1


def test_changed_value_between_preflight_and_write_is_rechecked(tmp_path):
    class RacingAPI(SheetsAPI):
        reads = 0

        def get_sheet_patch_values(self, file_id, range_name):
            self.reads += 1
            if self.reads == 2:
                self.values = [["concurrent edit"]]
            return super().get_sheet_patch_values(file_id, range_name)

    gateway = RacingAPI()
    with PlayTraceStore(tmp_path / "sheets.db") as store:
        with pytest.raises(SagaExecutionError, match="changed before write") as caught:
            executor(gateway, store).execute_transactional(patch(), proposal_id="p", attempt_id="a")
        assert caught.value.saga.status == SagaStatus.RECOVERY_FAILED
    assert not any(r[0] == "POST" for r in gateway.requests)
    assert gateway.values == [["concurrent edit"]]


def test_recovery_requires_original_explicit_mode_after_restart(tmp_path):
    gateway = SheetsAPI()
    database = tmp_path / "sheets.db"
    with PlayTraceStore(database) as store:
        result = executor(gateway, store).execute_transactional(patch(), proposal_id="p", attempt_id="a")
    gateway.sheet_write_mode = "atomic"
    with PlayTraceStore(database) as store:
        runner = executor(gateway, store)
        assert runner.recover(result.write_saga_id).status == SagaStatus.RECOVERY_FAILED
        gateway.sheet_write_mode = "single-editor"
        assert runner.recover(result.write_saga_id).status == SagaStatus.RECOVERED


@pytest.mark.parametrize("actual,expected", [([[True]], ((1,),)), ([["changed"]], (("old",),))])
def test_literal_preflight_rejects_changed_values_and_bool_number_collision(tmp_path, actual, expected):
    gateway = SheetsAPI(actual)
    with PlayTraceStore(tmp_path / "sheets.db") as store:
        with pytest.raises(PatchConflictError, match="values changed"):
            executor(gateway, store).execute_transactional(
                patch(expected=expected), proposal_id="p", attempt_id="a"
            )
    assert not any(r[0] == "POST" for r in gateway.requests)


@pytest.mark.parametrize("values", [[["=SUM(A1:A2)"]], [[None]], [[float("nan")]], [[2**53]]])
def test_unsupported_cell_values_fail_before_writing(tmp_path, values):
    gateway = SheetsAPI(values)
    with PlayTraceStore(tmp_path / "sheets.db") as store:
        with pytest.raises(ValueError):
            executor(gateway, store).execute_transactional(patch(), proposal_id="p", attempt_id="a")
    assert not any(r[0] == "POST" for r in gateway.requests)


def test_default_mode_and_unjournaled_execution_cannot_write(tmp_path):
    gateway = SheetsAPI(mode="atomic")
    with PlayTraceStore(tmp_path / "sheets.db") as store:
        with pytest.raises(PatchConflictError, match="single-editor"):
            executor(gateway, store).execute_transactional(patch(), proposal_id="p", attempt_id="a")
    assert gateway.requests == []
    gateway.sheet_write_mode = "single-editor"
    with pytest.raises(ValueError, match="durable saga"):
        SemanticPatchExecutor(gateway).execute(patch(), dry_run=False)
    assert not any(r[0] == "POST" for r in gateway.requests)


def test_overlapping_patches_fail_before_any_writes(tmp_path):
    gateway = SheetsAPI()
    original = patch()
    overlapping = original.model_copy(update={"sheet_patches": original.sheet_patches * 2})
    with PlayTraceStore(tmp_path / "sheets.db") as store:
        with pytest.raises(PatchConflictError, match="overlap"):
            executor(gateway, store).execute_transactional(overlapping, proposal_id="p", attempt_id="a")
    assert gateway.requests == []


def test_cli_gateway_requires_explicit_sheet_mode(monkeypatch):
    monkeypatch.setenv("TEST_SHEETS_TOKEN", "not-a-real-token")
    args = argparse.Namespace(fixture=None, access_token_env="TEST_SHEETS_TOKEN")
    assert _workspace_gateway(args).sheet_write_mode == "atomic"
    args.sheet_write_mode = "single-editor"
    assert _workspace_gateway(args).sheet_write_mode == "single-editor"


def table(range_name="'Card List'!C5:F20"):
    return SheetTableContract(sheet="Card List", range=range_name, entity_kind="card",
                              entity_id_column="ID")


def test_table_coordinates_use_actual_header_row_and_quoted_tab():
    contract = table()
    records = extract_sheet_records(
        {contract.range: [["ID", "Name", "Status", "Cost"], ["card:1", "Brace", "WORKING", 2]]},
        (contract,), "cards", "cards@1", "test",
    )
    assert records.entities[0].attributes["row"] == 6
    assert records.assertions[0].provenance.location == "sheet:Card List:row:6:column:Cost"
    assert parse_sheet_range("'Designer''s Cards'!$B$3:$C$4").sheet == "Designer's Cards"
    assert literal_matrix([], parse_sheet_range("Cards!A1:B2")) == (("", ""), ("", ""))


def test_unheaded_captured_values_are_explicit_omissions():
    contract = table()
    records = extract_sheet_records(
        {contract.range: [["ID", "Name"], ["card:1", "Brace", "unmapped note"]]},
        (contract,), "cards", "cards@1", "test",
    )
    assert len(records.entities) == 1
    assert records.assertions == ()
    assert len(records.omissions) == 1
    assert "row 6" in records.omissions[0]


@pytest.mark.parametrize("rows", [
    [["ID", "Name", "Name"]], [["ID", "Name", ""]],
    [["ID", "Name"], ["same", "A"], ["same", "B"]],
    [["ID", "Name", "A/B", "A B"]],
])
def test_ambiguous_sheet_identity_is_rejected(rows):
    contract = table()
    with pytest.raises(ValueError):
        extract_sheet_records({contract.range: rows}, (contract,), "cards", "cards@1", "test")


def manifest(contracts):
    return ProjectManifest(project_id="game", name="Game", repository="repo", drive_folder_id="folder",
        sources=(ManifestSource(id="cards", file_id="sheet", kind="google_sheet", role="cards",
                                title="Cards", schema_id="cards-v1", tables=contracts),))


def test_sync_refetches_new_manifest_ranges_without_a_provider_revision_change(tmp_path):
    first, second = table(), table("'Card List'!C22:F40")
    gateway = FixtureWorkspaceGateway({"files": {"sheet": {"version": "1"}}, "spreadsheets": {
        "sheet": {"metadata": {}, "ranges": {first.range: [], second.range: []}}}})
    with PlayTraceStore(tmp_path / "cache.db") as store:
        sync = WorkspaceSynchronizer(gateway, WorkspaceCache(store.connection))
        assert not sync.sync(manifest((first,))).sources[0].cache_hit
        expanded = sync.sync(manifest((first, second)))
        assert not expanded.sources[0].cache_hit
        assert second.range in expanded.sources[0].payload["ranges"]
        assert sync.sync(manifest((first, second))).sources[0].cache_hit


def test_snapshot_does_not_label_changed_sheet_content_with_old_revision():
    contract = table()
    class ChangingGateway(FixtureWorkspaceGateway):
        def get_spreadsheet(self, file_id, ranges=()):
            self.payload["files"][file_id]["version"] += 1
            return super().get_spreadsheet(file_id, ranges)
    gateway = ChangingGateway({"files": {"sheet": {"version": 1}}, "spreadsheets": {
        "sheet": {"metadata": {}, "ranges": {contract.range: []}}}})
    with pytest.raises(ValueError, match="three snapshot fetch attempts"):
        TabletopProjectAdapter(manifest((contract,)), gateway).snapshot()
