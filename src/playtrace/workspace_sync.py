"""Revision-aware local Workspace cache with provider metadata as authority."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, Mapping

from pydantic import Field

from playtrace.manifest import ProjectManifest, WorkspaceSourceKind
from playtrace.migrations import validate_database_version
from playtrace.models import StrictModel
from playtrace.workspace import WorkspaceGateway


class MetadataMismatchError(RuntimeError):
    pass


class SyncedSource(StrictModel):
    source_id: str
    file_id: str
    revision: str
    cache_hit: bool
    metadata: dict[str, Any]
    payload: dict[str, Any]


class WorkspaceCursor(StrictModel):
    """Durable provider position plus the complete observed manifest revision map."""

    page_token: str = Field(min_length=1, max_length=10_000)
    manifest_versions: dict[str, str]


class WorkspaceSyncResult(StrictModel):
    project_id: str
    cursor_before: WorkspaceCursor | None = None
    cursor_after: WorkspaceCursor
    sources: tuple[SyncedSource, ...]
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorkspaceCache:
    """Cache immutable source payloads by provider-observed revision."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        validate_database_version(connection)
        self.connection = connection

    def get(self, project_id: str, file_id: str, revision: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT payload FROM workspace_cache
               WHERE project_id = ? AND file_id = ? AND revision = ?""",
            (project_id, file_id, revision),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(
        self,
        project_id: str,
        file_id: str,
        revision: str,
        metadata: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO workspace_cache
                   (project_id, file_id, revision, metadata, payload, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    file_id,
                    revision,
                    json.dumps(dict(metadata), sort_keys=True),
                    json.dumps(dict(payload), sort_keys=True),
                    now,
                ),
            )

    def cursor(self, project_id: str) -> WorkspaceCursor | None:
        row = self.connection.execute(
            "SELECT cursor FROM workspace_cursors WHERE project_id = ?", (project_id,)
        ).fetchone()
        if not row:
            return None
        raw = str(row[0])
        try:
            return WorkspaceCursor.model_validate_json(raw)
        except (ValueError, TypeError):
            if raw.lstrip().startswith(("{", "[")):
                raise ValueError("stored Workspace cursor is malformed") from None
            # A pre-M6C cursor contained only the Drive token. It is safe to
            # consume, but it carries no claim about source revisions.
            return WorkspaceCursor(page_token=raw, manifest_versions={})

    def set_cursor(self, project_id: str, cursor: WorkspaceCursor) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO workspace_cursors(project_id, cursor, observed_at)
                   VALUES (?, ?, ?)""",
                (project_id, cursor.model_dump_json(), datetime.now(UTC).isoformat()),
            )


class WorkspaceSynchronizer:
    """Always fetch fresh metadata; reuse content only for the same revision."""

    def __init__(self, gateway: WorkspaceGateway, cache: WorkspaceCache) -> None:
        self.gateway = gateway
        self.cache = cache

    def sync(
        self,
        manifest: ProjectManifest,
        *,
        expected_app_properties: Mapping[str, str] | None = None,
    ) -> WorkspaceSyncResult:
        cursor_before = self.cache.cursor(manifest.project_id)
        provider_cursor: str
        if cursor_before is None:
            provider_cursor = self.gateway.get_start_page_token()
        else:
            page_token = cursor_before.page_token
            # Consume every page so the retained cursor cannot skip changes. Fresh
            # metadata below remains authoritative even when the change feed lags.
            for _ in range(10_000):
                page = self.gateway.list_changes(page_token)
                next_page = page.get("nextPageToken")
                if next_page:
                    page_token = str(next_page)
                    continue
                provider_cursor = str(page.get("newStartPageToken") or page_token)
                break
            else:
                raise RuntimeError("Workspace change feed exceeded the page safety bound")
        synced: list[SyncedSource] = []
        newest: dict[str, str] = {}
        for source in manifest.sources:
            # Metadata is read on both sides of content retrieval.  A provider
            # revision that moves while content is in flight invalidates that
            # payload; never cache or return it under the earlier revision.
            for attempt in range(3):
                metadata_before = self.gateway.get_file(source.file_id)
                self._validate_metadata(metadata_before, expected_app_properties)
                revision = self._revision(source.id, metadata_before)
                payload = self.cache.get(manifest.project_id, source.file_id, revision)
                hit = payload is not None
                if payload is None:
                    if source.kind is WorkspaceSourceKind.GOOGLE_DOC:
                        payload = self.gateway.get_document(source.file_id)
                    else:
                        ranges = tuple(table.range for table in source.tables)
                        payload = self.gateway.get_spreadsheet(source.file_id, ranges)
                metadata = self.gateway.get_file(source.file_id)
                self._validate_metadata(metadata, expected_app_properties)
                revision_after = self._revision(source.id, metadata)
                if revision_after == revision:
                    if not hit:
                        self.cache.put(
                            manifest.project_id, source.file_id, revision, metadata, payload
                        )
                    break
            else:
                raise MetadataMismatchError(
                    f"{source.id} changed revision during three content fetch attempts"
                )
            newest[source.file_id] = revision
            synced.append(
                SyncedSource(
                    source_id=source.id,
                    file_id=source.file_id,
                    revision=revision,
                    cache_hit=hit,
                    metadata=metadata,
                    payload=payload,
                )
            )
        cursor_after = WorkspaceCursor(
            page_token=provider_cursor,
            manifest_versions=dict(sorted(newest.items())),
        )
        self.cache.set_cursor(manifest.project_id, cursor_after)
        return WorkspaceSyncResult(
            project_id=manifest.project_id,
            cursor_before=cursor_before,
            cursor_after=cursor_after,
            sources=tuple(synced),
        )

    @staticmethod
    def _revision(source_id: str, metadata: Mapping[str, Any]) -> str:
        revision = str(metadata.get("version") or metadata.get("modifiedTime") or "")
        if not revision:
            raise MetadataMismatchError(f"{source_id} has no provider revision metadata")
        return revision

    @staticmethod
    def _validate_metadata(
        metadata: Mapping[str, Any], expected: Mapping[str, str] | None
    ) -> None:
        if not expected:
            return
        actual = metadata.get("appProperties") or {}
        failures = {
            key: (value, actual.get(key))
            for key, value in expected.items()
            if actual.get(key) != value
        }
        if failures:
            raise MetadataMismatchError(f"provider appProperties mismatch: {failures}")
