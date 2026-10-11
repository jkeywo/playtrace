"""Production tabletop ProjectAdapter over Google Docs and Sheets."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playtrace.canonical import canonical_digest
from playtrace.cnl import RuleIR, TabletopRuleCompiler
from playtrace.docs_adapter import extract_document_records
from playtrace.manifest import ProjectManifest, WorkspaceSourceKind, load_manifest
from playtrace.models import (
    Assertion,
    AuthoredStatus,
    DesignEntity,
    DesignSnapshot,
    ProvenanceRef,
    SourceMedium,
    SourceRevision,
)
from playtrace.sheets_adapter import extract_sheet_records
from playtrace.workspace import WorkspaceGateway


class TabletopProjectAdapter:
    adapter_id = "google-workspace-tabletop-v1"

    def __init__(self, manifest: ProjectManifest, gateway: WorkspaceGateway) -> None:
        self.manifest = manifest
        self.gateway = gateway
        self.compiler = TabletopRuleCompiler()

    @classmethod
    def from_path(
        cls, manifest_path: str | Path, gateway: WorkspaceGateway
    ) -> "TabletopProjectAdapter":
        return cls(load_manifest(manifest_path), gateway)

    def snapshot(self) -> DesignSnapshot:
        revisions: list[SourceRevision] = []
        entities: list[DesignEntity] = []
        assertions: list[Assertion] = []
        sheet_omissions: list[str] = []

        manifest_revision = self._validate_drive_manifest()
        if manifest_revision is not None:
            revisions.append(manifest_revision)

        for source in self.manifest.sources:
            metadata, payload = self._stable_read(
                source.file_id, source.kind,
                tuple(table.range for table in source.tables),
            )
            if source.kind is WorkspaceSourceKind.GOOGLE_DOC:
                provider_revision = str(payload.get("revisionId") or metadata.get("version") or metadata["modifiedTime"])
            else:
                provider_revision = str(metadata.get("version") or metadata["modifiedTime"])
            revision = self._source_revision(source.id, source.file_id, metadata, provider_revision, payload)
            revisions.append(revision)

            if source.kind is WorkspaceSourceKind.GOOGLE_DOC:
                records = extract_document_records(
                    payload, source.id, revision.id, self.adapter_id
                )
                entities.extend(records.entities)
                assertions.extend(records.assertions)
                self._append_compiled_rules(
                    records.cnl_lines, revision.id, entities, assertions
                )
            else:
                records = extract_sheet_records(
                    payload["ranges"],
                    source.tables,
                    source.id,
                    revision.id,
                    self.adapter_id,
                )
                sheet_omissions.extend(records.omissions)
                entities.extend(records.entities)
                assertions.extend(records.assertions)

        snapshot_digest = canonical_digest(
            [
                {"id": item.id, "content_digest": item.content_digest}
                for item in revisions
            ],
            domain="tabletop-snapshot-id",
        )[:20]
        return DesignSnapshot(
            id=f"{self.manifest.project_id}:snapshot:{snapshot_digest}",
            project_id=self.manifest.project_id,
            adapter_id=self.adapter_id,
            source_revisions=tuple(revisions),
            entities=tuple(entities),
            assertions=tuple(assertions),
            capabilities=self.manifest.capabilities,
            omissions=(
                "Only explicitly status-marked Doc passages become assertions.",
                "Unmarked prose remains traceable as entities but is not compiled as mechanics.",
                *sheet_omissions,
            ),
        )

    def _validate_drive_manifest(self) -> SourceRevision | None:
        """Ensure the game-owned Drive registry agrees with local adapter config."""
        file_id = self.manifest.drive_manifest_file_id
        if file_id is None:
            return None
        metadata, payload = self._stable_read(
            file_id, WorkspaceSourceKind.GOOGLE_SHEET, (self.manifest.drive_manifest_range,)
        )
        values = payload["ranges"][self.manifest.drive_manifest_range]
        if not values:
            raise ValueError("Drive manifest is empty")
        headers = [str(value).strip() for value in values[0]]
        required = {"Source ID", "File ID", "Kind", "Role", "Schema"}
        missing = required.difference(headers)
        if missing:
            raise ValueError(f"Drive manifest is missing columns: {sorted(missing)}")
        rows: dict[str, dict[str, str]] = {}
        for raw in values[1:]:
            row = {
                header: str(raw[index]).strip() if index < len(raw) else ""
                for index, header in enumerate(headers)
            }
            if row["Source ID"]:
                rows[row["Source ID"]] = row
        for source in self.manifest.sources:
            row = rows.get(source.id)
            if row is None:
                raise ValueError(f"Drive manifest has no row for {source.id}")
            expected = {
                "File ID": source.file_id,
                "Kind": source.kind.value,
                "Role": source.role,
                "Schema": source.schema_id,
            }
            mismatches = {
                key: (row.get(key, ""), value)
                for key, value in expected.items()
                if row.get(key, "") != value
            }
            if mismatches:
                raise ValueError(
                    f"Drive manifest disagrees with local config for {source.id}: {mismatches}"
                )
        provider_revision = str(metadata.get("version") or metadata["modifiedTime"])
        return self._source_revision(
            f"{self.manifest.project_id}:manifest",
            file_id,
            metadata,
            provider_revision,
            payload,
        )

    def _stable_read(self, file_id: str, kind: WorkspaceSourceKind, ranges: tuple[str, ...]) -> tuple[dict, dict]:
        for _ in range(3):
            before = self.gateway.get_file(file_id)
            revision = str(before.get("version") or before.get("modifiedTime") or "")
            if not revision:
                raise ValueError(f"{file_id} has no provider revision metadata")
            payload = (
                self.gateway.get_document(file_id) if kind is WorkspaceSourceKind.GOOGLE_DOC
                else self.gateway.get_spreadsheet(file_id, ranges)
            )
            after = self.gateway.get_file(file_id)
            if str(after.get("version") or after.get("modifiedTime") or "") == revision:
                return after, payload
        raise ValueError(f"{file_id} changed revision during three snapshot fetch attempts")

    def _append_compiled_rules(
        self,
        lines: tuple[str, ...],
        revision_id: str,
        entities: list[DesignEntity],
        assertions: list[Assertion],
    ) -> None:
        if not lines:
            return
        compiled = self.compiler.compile("\n".join(lines))
        for rule in compiled:
            entity_id = rule.id
            existing_index = next(
                (index for index, entity in enumerate(entities) if entity.id == entity_id),
                None,
            )
            if existing_index is None:
                entities.append(
                    DesignEntity(
                        id=entity_id,
                        kind="compiled_rule",
                        name=rule.id,
                        attributes={"ir": rule.model_dump(mode="json"), "identity_origin": "cnl"},
                    )
                )
            else:
                existing = entities[existing_index]
                entities[existing_index] = existing.model_copy(
                    update={
                        "attributes": {
                            **existing.attributes,
                            "ir": rule.model_dump(mode="json"),
                        }
                    }
                )
            assertions.append(
                Assertion(
                    id=f"{entity_id}:compiled",
                    subject_id=entity_id,
                    predicate="compiles_to",
                    value=rule.model_dump(mode="json"),
                    authored_status=AuthoredStatus.UNSPECIFIED,
                    provenance=ProvenanceRef(
                        source_revision_id=revision_id,
                        location=f"cnl:{rule.id}",
                        extractor=f"{self.adapter_id}:cnl-v1",
                    ),
                )
            )

    @staticmethod
    def _source_revision(
        source_id: str,
        file_id: str,
        metadata: dict[str, Any],
        provider_revision: str,
        payload: dict[str, Any],
    ) -> SourceRevision:
        modified = metadata.get("modifiedTime")
        observed = datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else datetime.now(UTC)
        digest = canonical_digest(payload, domain="source-revision-content")
        return SourceRevision(
            id=f"{source_id}@{provider_revision}",
            source_id=source_id,
            medium=SourceMedium.DRIVE,
            locator=str(metadata.get("webViewLink") or f"https://drive.google.com/open?id={file_id}"),
            revision=provider_revision,
            observed_at=observed,
            content_digest=digest,
        )
