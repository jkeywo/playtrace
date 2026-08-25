import pytest

from playtrace.patches import (
    DocumentTextPatch,
    PatchConflictError,
    SemanticPatchExecutor,
    SemanticPatchSet,
    SheetValuesPatch,
)
from playtrace.workspace import FixtureWorkspaceGateway


def test_semantic_patch_preflights_then_compiles_native_requests() -> None:
    gateway = FixtureWorkspaceGateway(_fixture())
    patch_set = SemanticPatchSet(
        id="patch:1",
        base_snapshot_id="snapshot:1",
        document_patches=(
            DocumentTextPatch(
                source_id="source:rules",
                file_id="doc-1",
                required_revision_id="doc-r1",
                tab_id="t.0",
                start_index=1,
                end_index=9,
                expected_text="Old text",
                replacement_text="New text",
            ),
        ),
        sheet_patches=(
            SheetValuesPatch(
                source_id="source:cards",
                file_id="sheet-1",
                required_version="7",
                range="Cards!D2:D2",
                expected_values=(("Old rule",),),
                replacement_values=(("New rule",),),
            ),
        ),
    )

    dry_run = SemanticPatchExecutor(gateway).execute(patch_set)
    applied = SemanticPatchExecutor(gateway).execute(patch_set, dry_run=False)

    assert dry_run.checked_operations == 2
    assert dry_run.applied_operations == 0
    assert applied.applied_operations == 2
    assert gateway.writes[0]["required_revision_id"] == "doc-r1"
    assert gateway.writes[1]["data"][0]["range"] == "Cards!D2:D2"


def test_semantic_patch_rejects_stale_revision_before_writing() -> None:
    gateway = FixtureWorkspaceGateway(_fixture())
    patch_set = SemanticPatchSet(
        id="patch:stale",
        base_snapshot_id="snapshot:1",
        document_patches=(
            DocumentTextPatch(
                source_id="source:rules",
                file_id="doc-1",
                required_revision_id="old-revision",
                tab_id="t.0",
                start_index=1,
                end_index=9,
                expected_text="Old text",
                replacement_text="New text",
            ),
        ),
    )

    with pytest.raises(PatchConflictError, match="revision changed"):
        SemanticPatchExecutor(gateway).execute(patch_set, dry_run=False)

    assert gateway.writes == []


def _fixture() -> dict[str, object]:
    text = "Old text\n"
    return {
        "files": {
            "doc-1": {"id": "doc-1", "version": "3"},
            "sheet-1": {"id": "sheet-1", "version": "7"},
        },
        "documents": {
            "doc-1": {
                "documentId": "doc-1",
                "revisionId": "doc-r1",
                "tabs": [
                    {
                        "tabProperties": {"tabId": "t.0", "title": "Rules"},
                        "body": {
                            "content": [
                                {
                                    "startIndex": 1,
                                    "endIndex": 1 + len(text),
                                    "paragraph": {
                                        "elements": [
                                            {
                                                "startIndex": 1,
                                                "endIndex": 1 + len(text),
                                                "textRun": {"content": text},
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
        "spreadsheets": {
            "sheet-1": {
                "metadata": {"spreadsheetId": "sheet-1"},
                "ranges": {"Cards!D2:D2": [["Old rule"]]},
            }
        },
    }

