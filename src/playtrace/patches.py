"""Revision-guarded semantic patches for native Docs and Sheets."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from playtrace.docs_adapter import (
    document_tab_text_bounds,
    replace_text_in_utf16_range,
    text_in_document_range,
    utf16_code_unit_length,
)
from playtrace.models import StrictModel
from playtrace.workspace import WorkspaceConcurrencyError, WorkspaceGateway


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
        expected_units = utf16_code_unit_length(self.expected_text)
        utf16_code_unit_length(self.replacement_text)
        if self.end_index - self.start_index != expected_units:
            raise ValueError("document range does not match expected_text UTF-16 extent")
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
    write_saga_id: str | None = None


class SagaStatus(StrEnum):
    PREPARED = "prepared"
    APPLYING = "applying"
    # Kept for journals written by the first M6 implementation.  Applied is a
    # recoverable intermediate state, never a successful terminal state.
    APPLIED = "applied"
    VALIDATING = "validating"
    COMMITTED = "committed"
    COMPENSATING = "compensating"
    RECOVERED = "recovered"
    RECOVERY_FAILED = "recovery_failed"


class SagaWriteOutcome(StrEnum):
    NOT_STARTED = "not_started"
    UNKNOWN = "unknown"
    RETURNED = "returned"


class SagaOperation(StrictModel):
    sequence: int
    kind: str
    file_id: str
    patch: dict[str, Any]
    before_image: dict[str, Any]
    attempted: bool = False
    applied: bool = False
    verified: bool = False
    write_outcome: SagaWriteOutcome = SagaWriteOutcome.NOT_STARTED
    compensated: bool = False
    response: dict[str, Any] | None = None
    error: str | None = None


class WriteSaga(StrictModel):
    id: str
    proposal_id: str
    attempt_id: str
    patch_set_id: str
    status: SagaStatus
    operations: tuple[SagaOperation, ...]
    failures: tuple[str, ...] = ()
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_operation_outcomes(cls, value: Any) -> Any:
        """Conservatively recover journals written before transport outcomes existed.

        An old operation in any post-preparation saga may have reached the
        provider even when it lacks the newer ``attempted`` fields.  Marking it
        unknown forces recovery to inspect canon; it can then restore an applied
        image, accept an already-restored image, or fail closed on anything else.
        """

        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        status = str(migrated.get("status", SagaStatus.PREPARED.value))
        post_preparation = status != SagaStatus.PREPARED.value
        operations: list[Any] = []
        for raw_operation in migrated.get("operations", ()):
            if not isinstance(raw_operation, dict):
                operations.append(raw_operation)
                continue
            operation = dict(raw_operation)
            compensated = bool(operation.get("compensated", False))
            evidence_of_write = bool(
                operation.get("applied")
                or operation.get("verified")
                or operation.get("response") is not None
            )
            evidence_of_attempt = evidence_of_write or compensated
            if "attempted" not in operation:
                operation["attempted"] = post_preparation or evidence_of_attempt
            elif evidence_of_attempt and not operation.get("attempted"):
                operation["attempted"] = True
            attempted = bool(operation.get("attempted"))
            outcome = operation.get("write_outcome")
            outcome_value = (
                outcome.value if isinstance(outcome, SagaWriteOutcome) else outcome
            )
            if (
                outcome_value not in {None, SagaWriteOutcome.NOT_STARTED.value}
                and not attempted
            ):
                # A durable transport outcome is itself proof that transport was
                # attempted, even if a corrupt/legacy attempted flag says false.
                operation["attempted"] = True
                attempted = True
            if outcome is None or (
                outcome_value == SagaWriteOutcome.NOT_STARTED.value and attempted
            ):
                operation["write_outcome"] = (
                    SagaWriteOutcome.RETURNED.value
                    if evidence_of_write
                    else SagaWriteOutcome.UNKNOWN.value
                    if attempted
                    else SagaWriteOutcome.NOT_STARTED.value
                )
            operations.append(operation)
        migrated["operations"] = operations

        # A legacy journal could previously claim recovery merely because every
        # missing ``attempted`` flag defaulted false.  Re-open it for inspection.
        if status == SagaStatus.RECOVERED.value and any(
            item.get("attempted") and not item.get("compensated", False)
            for item in operations
            if isinstance(item, dict)
        ):
            migrated["status"] = SagaStatus.RECOVERY_FAILED.value
        if status == SagaStatus.COMMITTED.value and (
            not operations
            or any(
                not item.get("attempted")
                or not item.get("applied")
                or not item.get("verified")
                or item.get("write_outcome") != SagaWriteOutcome.RETURNED.value
                or item.get("compensated", False)
                for item in operations
                if isinstance(item, dict)
            )
        ):
            # Success without durable proof of every provider write is not a
            # terminal success. Re-open it for fail-closed recovery.
            migrated["status"] = SagaStatus.RECOVERY_FAILED.value
        return migrated


class SagaExecutionError(RuntimeError):
    def __init__(self, saga: WriteSaga) -> None:
        self.saga = saga
        super().__init__("; ".join(saga.failures) or f"write saga {saga.id} failed")


class WriteSagaJournal:
    """Durable journal; each operation transition is committed before continuing."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(self, saga: WriteSaga) -> None:
        saga = WriteSaga.model_validate(saga.model_dump(mode="python"))
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO write_sagas
                   (id, proposal_id, attempt_id, status, payload) VALUES (?, ?, ?, ?, ?)""",
                (saga.id, saga.proposal_id, saga.attempt_id, saga.status.value, saga.model_dump_json()),
            )

    def load(self, saga_id: str) -> WriteSaga | None:
        row = self.connection.execute(
            "SELECT id, status, payload FROM write_sagas WHERE id = ?", (saga_id,)
        ).fetchone()
        if row is None:
            return None
        return self._adopt_rows((row,))[0]

    def all(self) -> tuple[WriteSaga, ...]:
        """Load every saga through the canonical, durable migration boundary."""

        rows = self.connection.execute(
            "SELECT id, status, payload FROM write_sagas ORDER BY id"
        ).fetchall()
        return self._adopt_rows(tuple(rows))

    def _adopt_rows(self, rows: tuple[sqlite3.Row, ...]) -> tuple[WriteSaga, ...]:
        sagas: list[WriteSaga] = []
        updates: list[tuple[str, str, str]] = []
        for row in rows:
            saga = WriteSaga.model_validate_json(row["payload"])
            canonical_payload = saga.model_dump_json()
            if row["status"] != saga.status.value or row["payload"] != canonical_payload:
                updates.append((saga.status.value, canonical_payload, str(row["id"])))
            sagas.append(saga)
        if updates:
            # Status and payload are one recovery fact. Persist every adoption
            # in one transaction so discovery cannot expose a half-migrated set.
            with self.connection:
                self.connection.executemany(
                    "UPDATE write_sagas SET status = ?, payload = ? WHERE id = ?",
                    updates,
                )
        return tuple(sagas)

    def incomplete(self) -> tuple[WriteSaga, ...]:
        # Do not filter on the denormalized status column before adoption: an old
        # row may claim RECOVERED while its canonical payload reopens it.
        return tuple(
            item
            for item in self.all()
            if item.status not in {SagaStatus.COMMITTED, SagaStatus.RECOVERED}
        )


class SemanticPatchExecutor:
    """Preflight all expected values and revisions before any native write."""

    def __init__(
        self,
        gateway: WorkspaceGateway,
        *,
        saga_journal: WriteSagaJournal | None = None,
    ) -> None:
        self.gateway = gateway
        self.saga_journal = saga_journal

    @staticmethod
    def _document_groups(
        patches: tuple[DocumentTextPatch, ...],
    ) -> tuple[tuple[DocumentTextPatch, ...], ...]:
        by_file: dict[str, list[DocumentTextPatch]] = {}
        for patch in patches:
            by_file.setdefault(patch.file_id, []).append(patch)
        groups: list[tuple[DocumentTextPatch, ...]] = []
        for file_id, values in by_file.items():
            revisions = {item.required_revision_id for item in values}
            if len(revisions) != 1:
                raise PatchConflictError(
                    f"document {file_id} patches require different revisions"
                )
            by_tab: dict[str, list[DocumentTextPatch]] = {}
            for patch in values:
                by_tab.setdefault(patch.tab_id, []).append(patch)
            for tab_id, tab_patches in by_tab.items():
                ordered = sorted(tab_patches, key=lambda item: (item.start_index, item.end_index))
                for previous, current in zip(ordered, ordered[1:]):
                    if (
                        current.start_index < previous.end_index
                        or current.start_index == previous.start_index
                    ):
                        raise PatchConflictError(
                            f"document {file_id} patches overlap in tab {tab_id}"
                        )
            groups.append(tuple(values))
        return tuple(groups)

    @staticmethod
    def _document_group_requests(
        patches: tuple[DocumentTextPatch, ...],
    ) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        # Higher indices must change first so lower edits cannot invalidate the
        # provider coordinates of later requests in the same atomic batch.
        for patch in sorted(
            patches, key=lambda item: (item.tab_id, item.start_index), reverse=True
        ):
            requests.extend(patch.google_requests())
        return requests

    @staticmethod
    def _document_group_payload(
        patches: tuple[DocumentTextPatch, ...],
    ) -> dict[str, Any]:
        return {
            "file_id": patches[0].file_id,
            "required_revision_id": patches[0].required_revision_id,
            "patches": [item.model_dump(mode="json") for item in patches],
        }

    @staticmethod
    def _patches_from_group(operation: SagaOperation) -> tuple[DocumentTextPatch, ...]:
        raw = operation.patch.get("patches")
        if not isinstance(raw, list) or not raw:
            raise ValueError("document group has no patches")
        patches = tuple(DocumentTextPatch.model_validate(item) for item in raw)
        if any(item.file_id != operation.file_id for item in patches):
            raise ValueError("document group contains a different file")
        return patches

    @staticmethod
    def _document_context_piece(
        document: dict[str, Any],
        tab_id: str,
        start_index: int,
        end_index: int,
        *,
        adjust_start: bool,
    ) -> tuple[int, str]:
        """Capture nearby text without ever splitting a UTF-16 surrogate pair."""

        boundary = start_index if adjust_start else end_index
        candidates = (boundary, boundary - 1, boundary + 1)
        last_error: ValueError | None = None
        for candidate in candidates:
            if candidate < 0:
                continue
            candidate_start = candidate if adjust_start else start_index
            candidate_end = end_index if adjust_start else candidate
            if candidate_start > candidate_end:
                continue
            try:
                text = text_in_document_range(
                    document, tab_id, candidate_start, candidate_end
                )
            except ValueError as error:
                last_error = error
                continue
            return candidate, text
        if last_error is not None:
            raise last_error
        raise ValueError("document context range is invalid")

    @classmethod
    def _capture_document_group_image(
        cls,
        document: dict[str, Any],
        patches: tuple[DocumentTextPatch, ...],
    ) -> dict[str, Any]:
        regions: list[dict[str, Any]] = []
        by_tab: dict[str, list[DocumentTextPatch]] = {}
        for patch in patches:
            by_tab.setdefault(patch.tab_id, []).append(patch)
        for tab_id, tab_patches in by_tab.items():
            range_start = min(item.start_index for item in tab_patches)
            before_range_end = max(item.end_index for item in tab_patches)
            before_text = text_in_document_range(
                document, tab_id, range_start, before_range_end
            )
            if utf16_code_unit_length(before_text) != before_range_end - range_start:
                raise PatchConflictError(
                    f"document group in tab {tab_id} crosses unsupported non-text indices"
                )
            after_text = before_text
            for patch in sorted(tab_patches, key=lambda item: item.start_index, reverse=True):
                after_text = replace_text_in_utf16_range(
                    after_text,
                    patch.start_index - range_start,
                    patch.end_index - range_start,
                    patch.replacement_text,
                )
            _, tab_end = document_tab_text_bounds(document, tab_id)
            trailing_end, trailing_text = cls._document_context_piece(
                document,
                tab_id,
                before_range_end,
                min(tab_end, before_range_end + 64),
                adjust_start=False,
            )
            regions.append(
                {
                    "source_ids": [item.source_id for item in tab_patches],
                    "tab_id": tab_id,
                    "before_start": range_start,
                    "before_end": before_range_end,
                    "after_start": range_start,
                    "after_end": range_start + utf16_code_unit_length(after_text),
                    "before_text": before_text,
                    "after_text": after_text,
                    "before_sha256": cls._text_digest(before_text),
                    "after_sha256": cls._text_digest(after_text),
                    "trailing_text": trailing_text,
                    "trailing_sha256": cls._text_digest(trailing_text),
                    "trailing_extent": trailing_end - before_range_end,
                    "before_tab_end": tab_end,
                    "after_tab_end": tab_end
                    + utf16_code_unit_length(after_text)
                    - utf16_code_unit_length(before_text),
                }
            )
        return {"image_version": 2, "regions": regions}

    @staticmethod
    def _operation_patch_count(operation: SagaOperation) -> int:
        if operation.kind == "document_group":
            raw = operation.patch.get("patches")
            return len(raw) if isinstance(raw, list) else 0
        return 1

    @classmethod
    def applied_semantic_operation_count(
        cls, operations: tuple[SagaOperation, ...]
    ) -> int:
        """Count authored patches, not provider batches, for attempt reporting."""

        return sum(cls._operation_patch_count(item) for item in operations if item.applied)

    def execute(
        self, patch_set: SemanticPatchSet, *, dry_run: bool = True
    ) -> PatchExecutionResult:
        document_groups = self._document_groups(patch_set.document_patches)
        document_payloads: dict[str, dict[str, Any]] = {}
        for patches in document_groups:
            patch = patches[0]
            document = document_payloads.setdefault(
                patch.file_id, self.gateway.get_document(patch.file_id)
            )
            current_revision = str(document.get("revisionId", ""))
            if current_revision != patch.required_revision_id:
                raise PatchConflictError(
                    f"{patch.source_id} revision changed: expected {patch.required_revision_id}, got {current_revision}"
                )
            for patch in patches:
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
        for patches in document_groups:
            patch = patches[0]
            responses.append(
                self.gateway.batch_update_document(
                    patch.file_id,
                    self._document_group_requests(patches),
                    patch.required_revision_id,
                )
            )
        sheet_groups: dict[str, list[SheetValuesPatch]] = {}
        for patch in patch_set.sheet_patches:
            sheet_groups.setdefault(patch.file_id, []).append(patch)
        for file_id, patches in sheet_groups.items():
            responses.append(
                self._compare_and_update_sheet_values(file_id, patches)
            )
        return PatchExecutionResult(
            patch_set_id=patch_set.id,
            dry_run=False,
            checked_operations=checked,
            applied_operations=checked,
            responses=tuple(responses),
        )

    def execute_transactional(
        self,
        patch_set: SemanticPatchSet,
        *,
        proposal_id: str,
        attempt_id: str,
    ) -> PatchExecutionResult:
        """Apply, verify, and compensate a multi-file patch as a durable saga."""

        if self.saga_journal is None:
            raise ValueError("transactional canonical writes require a durable saga journal")
        # Re-run all revision/value guards immediately before capturing before-images.
        preflight = self.execute(patch_set, dry_run=True)
        operations: list[SagaOperation] = []
        for patches in self._document_groups(patch_set.document_patches):
            patch = patches[0]
            document = self.gateway.get_document(patch.file_id)
            if len(patches) > 1:
                operations.append(
                    SagaOperation(
                        sequence=len(operations) + 1,
                        kind="document_group",
                        file_id=patch.file_id,
                        patch=self._document_group_payload(patches),
                        before_image=self._capture_document_group_image(document, patches),
                    )
                )
                continue
            context_size = 64
            tab_start, tab_end = document_tab_text_bounds(document, patch.tab_id)
            left_start = max(tab_start, patch.start_index - context_size)
            right_end = min(tab_end, patch.end_index + context_size)
            left_start, left_context = self._document_context_piece(
                document,
                patch.tab_id,
                left_start,
                patch.start_index,
                adjust_start=True,
            )
            right_end, right_context = self._document_context_piece(
                document,
                patch.tab_id,
                patch.end_index,
                right_end,
                adjust_start=False,
            )
            before_text = left_context + patch.expected_text + right_context
            after_text = left_context + patch.replacement_text + right_context
            before_range_end = left_start + utf16_code_unit_length(before_text)
            after_range_end = left_start + utf16_code_unit_length(after_text)
            trailing_end, trailing_text = self._document_context_piece(
                document,
                patch.tab_id,
                before_range_end,
                min(tab_end, before_range_end + context_size),
                adjust_start=False,
            )
            operations.append(
                SagaOperation(
                    sequence=len(operations) + 1,
                    kind="document",
                    file_id=patch.file_id,
                    patch=patch.model_dump(mode="json"),
                    before_image={
                        "image_version": 1,
                        "range_start": left_start,
                        "before_range_end": before_range_end,
                        "after_range_end": after_range_end,
                        "before_text": before_text,
                        "before_sha256": self._text_digest(before_text),
                        "after_text": after_text,
                        "after_sha256": self._text_digest(after_text),
                        "trailing_text": trailing_text,
                        "trailing_sha256": self._text_digest(trailing_text),
                        "trailing_extent": trailing_end - before_range_end,
                        "before_tab_end": tab_end,
                        "after_tab_end": tab_end
                        + utf16_code_unit_length(patch.replacement_text)
                        - utf16_code_unit_length(patch.expected_text),
                    },
                )
            )
        for patch in patch_set.sheet_patches:
            operations.append(
                SagaOperation(
                    sequence=len(operations) + 1,
                    kind="sheet",
                    file_id=patch.file_id,
                    patch=patch.model_dump(mode="json"),
                    before_image={"values": patch.expected_values},
                )
            )
        saga = WriteSaga(
            id=f"{attempt_id}:saga",
            proposal_id=proposal_id,
            attempt_id=attempt_id,
            patch_set_id=patch_set.id,
            status=SagaStatus.PREPARED,
            operations=tuple(operations),
        )
        self.saga_journal.save(saga)
        saga = saga.model_copy(update={"status": SagaStatus.APPLYING})
        self.saga_journal.save(saga)
        responses: list[dict[str, Any]] = []
        try:
            for index, operation in enumerate(saga.operations):
                mutable = list(saga.operations)
                # UNKNOWN is durable before transport begins: a process death or
                # transport exception may occur after the provider accepted it.
                operation = operation.model_copy(
                    update={
                        "attempted": True,
                        "write_outcome": SagaWriteOutcome.UNKNOWN,
                    }
                )
                mutable[index] = operation
                saga = saga.model_copy(
                    update={"operations": tuple(mutable), "updated_at": datetime.now(UTC)}
                )
                self.saga_journal.save(saga)
                updated, response = self._apply(operation)
                responses.append(response)
                mutable = list(saga.operations)
                mutable[index] = updated
                saga = saga.model_copy(
                    update={"operations": tuple(mutable), "updated_at": datetime.now(UTC)}
                )
                self.saga_journal.save(saga)
                verified = self._verify(updated)
                mutable = list(saga.operations)
                mutable[index] = verified
                saga = saga.model_copy(
                    update={"operations": tuple(mutable), "updated_at": datetime.now(UTC)}
                )
                self.saga_journal.save(saga)
        except Exception as error:
            mutable = list(saga.operations)
            failed_index = next(
                (i for i, item in enumerate(mutable) if not item.verified),
                len(mutable) - 1,
            )
            mutable[failed_index] = mutable[failed_index].model_copy(update={"error": str(error)})
            saga = saga.model_copy(
                update={
                    "operations": tuple(mutable),
                    "failures": (f"operation {failed_index + 1} failed: {error}",),
                }
            )
            saga = self.compensate(saga)
            raise SagaExecutionError(saga) from error
        # APPLIED means writes are verified but the caller's post-write design
        # validation has not committed them.  It is deliberately recoverable.
        saga = saga.model_copy(
            update={"status": SagaStatus.APPLIED, "updated_at": datetime.now(UTC)}
        )
        self.saga_journal.save(saga)
        return PatchExecutionResult(
            patch_set_id=patch_set.id,
            dry_run=False,
            checked_operations=preflight.checked_operations,
            applied_operations=self.applied_semantic_operation_count(saga.operations),
            responses=tuple(responses),
            write_saga_id=saga.id,
        )

    def begin_validation(self, saga_id: str) -> WriteSaga:
        """Durably enter caller-owned post-write validation."""

        saga = self._load_saga(saga_id)
        if saga.status is SagaStatus.VALIDATING:
            return saga
        if saga.status is not SagaStatus.APPLIED:
            raise ValueError(f"write saga {saga_id!r} cannot validate from {saga.status}")
        saga = saga.model_copy(
            update={"status": SagaStatus.VALIDATING, "updated_at": datetime.now(UTC)}
        )
        self.saga_journal.save(saga)  # type: ignore[union-attr]
        return saga

    def commit(self, saga_id: str) -> WriteSaga:
        """Durably make a successfully validated saga terminal."""

        saga = self._load_saga(saga_id)
        if saga.status is SagaStatus.COMMITTED:
            return saga
        if saga.status is not SagaStatus.VALIDATING:
            raise ValueError(f"write saga {saga_id!r} cannot commit from {saga.status}")
        saga = saga.model_copy(
            update={"status": SagaStatus.COMMITTED, "updated_at": datetime.now(UTC)}
        )
        self.saga_journal.save(saga)  # type: ignore[union-attr]
        # Journal adoption is authoritative: a malformed terminal claim is
        # persisted as recovery_failed and must not be returned as committed.
        return self._load_saga(saga_id)

    def _load_saga(self, saga_id: str) -> WriteSaga:
        if self.saga_journal is None:
            raise ValueError("saga transition requires a durable saga journal")
        saga = self.saga_journal.load(saga_id)
        if saga is None:
            raise KeyError(f"write saga {saga_id!r} was not found")
        return saga

    def recover(self, saga_id: str) -> WriteSaga:
        saga = self._load_saga(saga_id)
        if saga.status in {SagaStatus.COMMITTED, SagaStatus.RECOVERED}:
            return saga
        return self.compensate(saga)

    def rollback(self, saga_id: str) -> WriteSaga:
        """Compensate a fully written saga after post-write validation fails."""

        saga = self._load_saga(saga_id)
        if saga.status is SagaStatus.RECOVERED:
            return saga
        return self.compensate(saga)

    def compensate(self, saga: WriteSaga) -> WriteSaga:
        if self.saga_journal is None:
            raise ValueError("compensation requires a durable saga journal")
        saga = saga.model_copy(update={"status": SagaStatus.COMPENSATING})
        self.saga_journal.save(saga)
        mutable = list(saga.operations)
        failures = list(saga.failures)
        for index in range(len(mutable) - 1, -1, -1):
            operation = mutable[index]
            if not operation.attempted or operation.compensated:
                continue
            try:
                mutable[index] = self._compensate_and_verify(operation)
            except Exception as error:
                failures.append(f"compensation {operation.sequence} failed: {error}")
                mutable[index] = operation.model_copy(update={"error": str(error)})
            saga = saga.model_copy(
                update={"operations": tuple(mutable), "failures": tuple(failures)}
            )
            self.saga_journal.save(saga)
        recovery_failed = any(item.attempted and not item.compensated for item in mutable)
        saga = saga.model_copy(
            update={
                "status": SagaStatus.RECOVERY_FAILED if recovery_failed else SagaStatus.RECOVERED,
                "operations": tuple(mutable),
                "failures": tuple(failures),
                "updated_at": datetime.now(UTC),
            }
        )
        self.saga_journal.save(saga)
        return self._load_saga(saga.id)

    def _apply(self, operation: SagaOperation) -> tuple[SagaOperation, dict[str, Any]]:
        if operation.kind == "document_group":
            patches = self._patches_from_group(operation)
            response = self.gateway.batch_update_document(
                operation.file_id,
                self._document_group_requests(patches),
                patches[0].required_revision_id,
            )
        elif operation.kind == "document":
            patch = DocumentTextPatch.model_validate(operation.patch)
            response = self.gateway.batch_update_document(
                patch.file_id, patch.google_requests(), patch.required_revision_id
            )
        else:
            patch = SheetValuesPatch.model_validate(operation.patch)
            response = self._compare_and_update_sheet_values(patch.file_id, [patch])
        response = self._json_response(response)
        applied = operation.model_copy(
            update={
                "applied": True,
                "write_outcome": SagaWriteOutcome.RETURNED,
                "response": response,
            }
        )
        return applied, response

    def _verify(self, operation: SagaOperation) -> SagaOperation:
        if operation.kind == "document_group":
            document = self.gateway.get_document(operation.file_id)
            self._verify_document_group_image(operation, document, after=True)
        elif operation.kind == "document":
            patch = DocumentTextPatch.model_validate(operation.patch)
            document = self.gateway.get_document(patch.file_id)
            actual = text_in_document_range(
                document,
                patch.tab_id,
                patch.start_index,
                patch.start_index + utf16_code_unit_length(patch.replacement_text),
            )
            if actual != patch.replacement_text:
                raise RuntimeError(f"document verification mismatch for {patch.source_id}")
        else:
            patch = SheetValuesPatch.model_validate(operation.patch)
            payload = self.gateway.get_spreadsheet(patch.file_id, (patch.range,))
            actual = tuple(tuple(row) for row in payload["ranges"].get(patch.range, []))
            if actual != patch.replacement_values:
                raise RuntimeError(f"sheet verification mismatch for {patch.source_id}")
        return operation.model_copy(update={"verified": True})

    @staticmethod
    def _json_response(response: Any) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise TypeError("Workspace write response must be a JSON object")
        return json.loads(json.dumps(response, sort_keys=True))

    @classmethod
    def _document_group_regions(cls, operation: SagaOperation) -> tuple[dict[str, Any], ...]:
        image = operation.before_image
        if int(image.get("image_version", 0)) != 2:
            raise ValueError("document group recovery image version is invalid")
        raw_regions = image.get("regions")
        if not isinstance(raw_regions, list) or not raw_regions:
            raise ValueError("document group recovery image has no regions")
        regions: list[dict[str, Any]] = []
        for raw in raw_regions:
            if not isinstance(raw, dict):
                raise ValueError("document group recovery region is invalid")
            region = dict(raw)
            before_text = str(region["before_text"])
            after_text = str(region["after_text"])
            if cls._text_digest(before_text) != str(region["before_sha256"]):
                raise ValueError("document group before-image digest is invalid")
            if cls._text_digest(after_text) != str(region["after_sha256"]):
                raise ValueError("document group after-image digest is invalid")
            if int(region["before_end"]) - int(region["before_start"]) != utf16_code_unit_length(
                before_text
            ):
                raise ValueError("document group before-image extent is invalid")
            if int(region["after_end"]) - int(region["after_start"]) != utf16_code_unit_length(
                after_text
            ):
                raise ValueError("document group after-image extent is invalid")
            regions.append(region)
        return tuple(regions)

    @classmethod
    def _document_group_matches(
        cls,
        operation: SagaOperation,
        document: dict[str, Any],
        *,
        after: bool,
    ) -> bool:
        prefix = "after" if after else "before"
        for region in cls._document_group_regions(operation):
            try:
                actual = text_in_document_range(
                    document,
                    str(region["tab_id"]),
                    int(region[f"{prefix}_start"]),
                    int(region[f"{prefix}_end"]),
                )
            except ValueError:
                return False
            expected = str(region[f"{prefix}_text"])
            digest = str(region[f"{prefix}_sha256"])
            if actual != expected or cls._text_digest(actual) != digest:
                return False
            tab_end_key = f"{prefix}_tab_end"
            if tab_end_key in region:
                try:
                    _, actual_tab_end = document_tab_text_bounds(
                        document, str(region["tab_id"])
                    )
                except ValueError:
                    return False
                if actual_tab_end != int(region[tab_end_key]):
                    return False
            if "trailing_text" in region:
                trailing_text = str(region["trailing_text"])
                if cls._text_digest(trailing_text) != str(region["trailing_sha256"]):
                    raise ValueError("document group trailing boundary digest is invalid")
                trailing_start = int(region[f"{prefix}_end"])
                trailing_end = trailing_start + int(region["trailing_extent"])
                actual_trailing = text_in_document_range(
                    document, str(region["tab_id"]), trailing_start, trailing_end
                )
                if actual_trailing != trailing_text:
                    return False
        return True

    @classmethod
    def _verify_document_group_image(
        cls,
        operation: SagaOperation,
        document: dict[str, Any],
        *,
        after: bool,
    ) -> None:
        if not cls._document_group_matches(operation, document, after=after):
            phase = "after" if after else "before"
            raise RuntimeError(f"document group {phase}-image verification mismatch")

    @staticmethod
    def _text_replacement_requests(
        tab_id: str, start_index: int, end_index: int, replacement_text: str
    ) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        if end_index > start_index:
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {
                            "startIndex": start_index,
                            "endIndex": end_index,
                            "tabId": tab_id,
                        }
                    }
                }
            )
        if replacement_text:
            requests.append(
                {
                    "insertText": {
                        "location": {"index": start_index, "tabId": tab_id},
                        "text": replacement_text,
                    }
                }
            )
        return requests

    def _compensate_document_group(self, operation: SagaOperation) -> SagaOperation:
        document = self.gateway.get_document(operation.file_id)
        after_matches = self._document_group_matches(operation, document, after=True)
        before_matches = self._document_group_matches(operation, document, after=False)
        if before_matches and after_matches:
            if (
                operation.write_outcome is SagaWriteOutcome.UNKNOWN
                and "trailing_text" not in self._document_group_regions(operation)[0]
            ):
                raise PatchConflictError(
                    "document group outcome is ambiguous; refusing compensation"
                )
            # The complete before-image is present. A returned/verified write
            # may already have been compensated before the journal caught up.
            return operation.model_copy(update={"compensated": True})
        if not after_matches:
            if before_matches:
                return operation.model_copy(update={"compensated": True})
            raise PatchConflictError(
                "document group changed after saga failure; refusing compensation"
            )
        revision = str(document.get("revisionId", ""))
        regions = sorted(
            self._document_group_regions(operation),
            key=lambda item: (str(item["tab_id"]), int(item["after_start"])),
            reverse=True,
        )
        requests: list[dict[str, Any]] = []
        for region in regions:
            requests.extend(
                self._text_replacement_requests(
                    str(region["tab_id"]),
                    int(region["after_start"]),
                    int(region["after_end"]),
                    str(region["before_text"]),
                )
            )
        self.gateway.batch_update_document(operation.file_id, requests, revision)
        restored = self.gateway.get_document(operation.file_id)
        self._verify_document_group_image(operation, restored, after=False)
        return operation.model_copy(update={"compensated": True})

    def _compensate_and_verify(self, operation: SagaOperation) -> SagaOperation:
        if operation.kind == "document_group":
            return self._compensate_document_group(operation)
        if operation.kind == "document":
            patch = DocumentTextPatch.model_validate(operation.patch)
            document = self.gateway.get_document(patch.file_id)
            revision = str(document.get("revisionId", ""))
            (
                range_start,
                before_range_end,
                after_range_end,
                before_text,
                after_text,
                before_digest,
                after_digest,
            ) = self._document_recovery_images(operation, patch)

            try:
                current_after = text_in_document_range(
                    document, patch.tab_id, range_start, after_range_end
                )
                after_matches = (
                    current_after == after_text
                    and self._text_digest(current_after) == after_digest
                    and self._document_trailing_matches(
                        document, patch.tab_id, after_range_end, operation.before_image
                    )
                )
            except ValueError:
                after_matches = False
            try:
                current_before = text_in_document_range(
                    document, patch.tab_id, range_start, before_range_end
                )
                before_matches = (
                    current_before == before_text
                    and self._text_digest(current_before) == before_digest
                    and self._document_trailing_matches(
                        document, patch.tab_id, before_range_end, operation.before_image
                    )
                )
            except ValueError:
                before_matches = False
            try:
                _, actual_tab_end = document_tab_text_bounds(document, patch.tab_id)
            except ValueError:
                actual_tab_end = -1
            if "after_tab_end" in operation.before_image:
                after_matches = after_matches and actual_tab_end == int(
                    operation.before_image["after_tab_end"]
                )
            if "before_tab_end" in operation.before_image:
                before_matches = before_matches and actual_tab_end == int(
                    operation.before_image["before_tab_end"]
                )
            if before_matches and after_matches:
                if (
                    operation.write_outcome is SagaWriteOutcome.UNKNOWN
                    and "trailing_text" not in operation.before_image
                ):
                    raise PatchConflictError(
                        "document write outcome is ambiguous; refusing compensation"
                    )
                return operation.model_copy(update={"compensated": True})
            if after_matches:
                self.gateway.batch_update_document(
                    patch.file_id,
                    self._text_replacement_requests(
                        patch.tab_id, range_start, after_range_end, before_text
                    ),
                    revision,
                )
                restored = self.gateway.get_document(patch.file_id)
                restored_text = text_in_document_range(
                    restored,
                    patch.tab_id,
                    range_start,
                    before_range_end,
                )
                if (
                    restored_text != before_text
                    or self._text_digest(restored_text) != before_digest
                ):
                    raise RuntimeError("document compensation verification failed")
            else:
                if before_matches:
                    return operation.model_copy(update={"compensated": True})
                raise PatchConflictError("document changed after saga failure; refusing compensation")
        else:
            patch = SheetValuesPatch.model_validate(operation.patch)
            payload = self.gateway.get_spreadsheet(patch.file_id, (patch.range,))
            actual = tuple(tuple(row) for row in payload["ranges"].get(patch.range, []))
            if actual == patch.expected_values:
                return operation.model_copy(update={"compensated": True})
            if actual != patch.replacement_values:
                raise PatchConflictError("sheet changed after saga failure; refusing compensation")
            compensation_guard = patch.model_copy(
                update={
                    "expected_values": patch.replacement_values,
                    "replacement_values": patch.expected_values,
                }
            )
            self._compare_and_update_sheet_values(patch.file_id, [compensation_guard])
            restored = self.gateway.get_spreadsheet(patch.file_id, (patch.range,))
            actual = tuple(tuple(row) for row in restored["ranges"].get(patch.range, []))
            if actual != patch.expected_values:
                raise RuntimeError("sheet compensation verification failed")
        return operation.model_copy(update={"compensated": True})

    def _compare_and_update_sheet_values(
        self, file_id: str, patches: list[SheetValuesPatch]
    ) -> dict[str, Any]:
        """Use one provider-side comparison and write, or refuse the mutation."""

        expected_data = [
            {
                "range": patch.range,
                "majorDimension": "ROWS",
                "values": patch.expected_values,
            }
            for patch in patches
        ]
        try:
            return self.gateway.compare_and_batch_update_sheet_values(
                file_id,
                [patch.google_data() for patch in patches],
                expected_data,
            )
        except WorkspaceConcurrencyError as error:
            raise PatchConflictError(str(error)) from error

    @staticmethod
    def _text_digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def _document_trailing_matches(
        cls,
        document: dict[str, Any],
        tab_id: str,
        image_end: int,
        image: dict[str, Any],
    ) -> bool:
        """Use text immediately beyond an image as a length-sensitive boundary."""

        if "trailing_text" not in image:
            return True
        trailing_text = str(image["trailing_text"])
        if cls._text_digest(trailing_text) != str(image["trailing_sha256"]):
            raise ValueError("document trailing boundary digest is invalid")
        trailing_end = image_end + int(image["trailing_extent"])
        actual = text_in_document_range(document, tab_id, image_end, trailing_end)
        return actual == trailing_text

    @classmethod
    def _document_recovery_images(
        cls, operation: SagaOperation, patch: DocumentTextPatch
    ) -> tuple[int, int, int, str, str, str, str]:
        image = operation.before_image
        if int(image.get("image_version", 0)) == 1:
            range_start = int(image["range_start"])
            before_range_end = int(image["before_range_end"])
            after_range_end = int(image["after_range_end"])
            before_text = str(image["before_text"])
            after_text = str(image["after_text"])
            before_digest = str(image["before_sha256"])
            after_digest = str(image["after_sha256"])
            if cls._text_digest(before_text) != before_digest:
                raise ValueError("document saga before-image digest is invalid")
            if cls._text_digest(after_text) != after_digest:
                raise ValueError("document saga after-image digest is invalid")
            return (
                range_start,
                before_range_end,
                after_range_end,
                before_text,
                after_text,
                before_digest,
                after_digest,
            )

        # Journals produced by the initial M6 implementation contain the same
        # bounded information split into three fields.  Reconstitute exact images
        # before comparing; never infer restoration from only the expected prefix.
        range_start = int(image.get("left_start", patch.start_index))
        left = str(image.get("left_context", ""))
        right = str(image.get("right_context", ""))
        before_text = left + patch.expected_text + right
        after_text = left + patch.replacement_text + right
        before_range_end = patch.end_index + utf16_code_unit_length(right)
        after_range_end = (
            patch.start_index
            + utf16_code_unit_length(patch.replacement_text)
            + utf16_code_unit_length(right)
        )
        return (
            range_start,
            before_range_end,
            after_range_end,
            before_text,
            after_text,
            cls._text_digest(before_text),
            cls._text_digest(after_text),
        )
