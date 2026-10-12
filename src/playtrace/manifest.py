"""Per-game Play Trace manifest loading and validation."""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from playtrace.models import StrictModel


class WorkspaceSourceKind(StrEnum):
    GOOGLE_DOC = "google_doc"
    GOOGLE_SHEET = "google_sheet"


class SheetTableContract(StrictModel):
    sheet: str
    range: str
    entity_kind: str
    entity_id_column: str
    name_column: str = "Name"
    status_column: str | None = "Status"


class ManifestSource(StrictModel):
    id: str
    file_id: str
    kind: WorkspaceSourceKind
    role: str
    title: str
    schema_id: str
    scope: tuple[str, ...] = ()
    required: bool = True
    tables: tuple[SheetTableContract, ...] = ()

    @model_validator(mode="after")
    def tables_match_source_kind(self) -> "ManifestSource":
        if self.kind is WorkspaceSourceKind.GOOGLE_DOC and self.tables:
            raise ValueError("Google Doc sources cannot declare sheet tables")
        if self.kind is WorkspaceSourceKind.GOOGLE_SHEET and not self.tables:
            raise ValueError("Google Sheet sources must declare at least one table")
        return self


class ProjectManifest(StrictModel):
    manifest_version: int = 1
    project_id: str
    name: str
    repository: str
    drive_folder_id: str
    drive_manifest_file_id: str | None = None
    drive_manifest_range: str = "Manifest!A4:J100"
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    sources: tuple[ManifestSource, ...]

    @model_validator(mode="after")
    def require_unique_source_ids(self) -> "ProjectManifest":
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("manifest source IDs must be unique")
        file_ids = [source.file_id for source in self.sources]
        if len(file_ids) != len(set(file_ids)):
            raise ValueError("each canonical file may appear only once in the manifest")
        return self


def load_manifest(path: str | Path) -> ProjectManifest:
    manifest_path = Path(path)
    with manifest_path.open("rb") as handle:
        data = tomllib.load(handle)
    return ProjectManifest.model_validate(data)
