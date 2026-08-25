"""Revision-guarded semantic patches for native Docs and Sheets."""

from __future__ import annotations

from typing import Any

from pydantic import model_validator

from playtrace.docs_adapter import text_in_document_range
from playtrace.models import StrictModel
from playtrace.workspace import WorkspaceGateway


class PatchConflictError(RuntimeError):
    pass


class DocumentTextPatch(StrictModel):
    source_id: str
    file_id: str
    required_revision_id: str
    tab_id: str
    start_index: int
    end_index: int
    expected_text: str
    replacement_text: str

    @model_validator(mode="after")
    def valid_range(self) -> "DocumentTextPatch":
        if self.start_index < 0 or self.end_index < self.start_index:
            raise ValueError("invalid document range")
        return self

    def google_requests(self) -> list[dict[str, Any]]:
        target = {
            "startIndex": self.start_index,
            "endIndex": self.end_index,
            "tabId": self.tab_id,
        }
        requests: list[dict[str, Any]] = []
        if self.end_index > self.start_index:
            requests.append({"deleteContentRange": {"range": target}})
        if self.replacement_text:
            requests.append(
                {
                    "insertText": {
                        "location": {"index": self.start_index, "tabId": self.tab_id},
                        "text": self.replacement_text,
                    }
                }
            )
        return requests


class SheetValuesPatch(StrictModel):
    source_id: str
    file_id: str
    required_version: str
    range: str
    expected_values: tuple[tuple[Any, ...], ...]
    replacement_values: tuple[tuple[Any, ...], ...]

    def google_data(self) -> dict[str, Any]:
        return {"range": self.range, "majorDimension": "ROWS", "values": self.replacement_values}


class SemanticPatchSet(StrictModel):
    id: str
    base_snapshot_id: str
    document_patches: tuple[DocumentTextPatch, ...] = ()
    sheet_patches: tuple[SheetValuesPatch, ...] = ()

    @model_validator(mode="after")
    def require_patch(self) -> "SemanticPatchSet":
        if not self.document_patches and not self.sheet_patches:
            raise ValueError("a semantic patch set cannot be empty")
        return self


class PatchExecutionResult(StrictModel):
    patch_set_id: str
    dry_run: bool
    checked_operations: int
    applied_operations: int
    responses: tuple[dict[str, Any], ...] = ()


class SemanticPatchExecutor:
    """Preflight all expected values and revisions before any native write."""

    def __init__(self, gateway: WorkspaceGateway) -> None:
        self.gateway = gateway

    def execute(
        self, patch_set: SemanticPatchSet, *, dry_run: bool = True
    ) -> PatchExecutionResult:
        document_payloads: dict[str, dict[str, Any]] = {}
        for patch in patch_set.document_patches:
            document = document_payloads.setdefault(
                patch.file_id, self.gateway.get_document(patch.file_id)
            )
            current_revision = str(document.get("revisionId", ""))
            if current_revision != patch.required_revision_id:
                raise PatchConflictError(
                    f"{patch.source_id} revision changed: expected {patch.required_revision_id}, got {current_revision}"
                )
            current_text = text_in_document_range(
                document, patch.tab_id, patch.start_index, patch.end_index
            )
            if current_text != patch.expected_text:
                raise PatchConflictError(
                    f"{patch.source_id} text changed at {patch.tab_id}:{patch.start_index}-{patch.end_index}"
                )

        for patch in patch_set.sheet_patches:
            metadata = self.gateway.get_file(patch.file_id)
            current_version = str(metadata.get("version", ""))
            if current_version != patch.required_version:
                raise PatchConflictError(
                    f"{patch.source_id} version changed: expected {patch.required_version}, got {current_version}"
                )
            payload = self.gateway.get_spreadsheet(patch.file_id, (patch.range,))
            actual = tuple(tuple(row) for row in payload["ranges"].get(patch.range, []))
            if actual != patch.expected_values:
                raise PatchConflictError(f"{patch.source_id} values changed in {patch.range}")

        checked = len(patch_set.document_patches) + len(patch_set.sheet_patches)
        if dry_run:
            return PatchExecutionResult(
                patch_set_id=patch_set.id,
                dry_run=True,
                checked_operations=checked,
                applied_operations=0,
            )

        responses: list[dict[str, Any]] = []
        for patch in patch_set.document_patches:
            responses.append(
                self.gateway.batch_update_document(
                    patch.file_id,
                    patch.google_requests(),
                    patch.required_revision_id,
                )
            )
        sheet_groups: dict[str, list[SheetValuesPatch]] = {}
        for patch in patch_set.sheet_patches:
            sheet_groups.setdefault(patch.file_id, []).append(patch)
        for file_id, patches in sheet_groups.items():
            responses.append(
                self.gateway.batch_update_sheet_values(
                    file_id, [patch.google_data() for patch in patches]
                )
            )
        return PatchExecutionResult(
            patch_set_id=patch_set.id,
            dry_run=False,
            checked_operations=checked,
            applied_operations=checked,
            responses=tuple(responses),
        )

