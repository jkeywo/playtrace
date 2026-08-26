"""Google Workspace transport boundary and deterministic fixture transport."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol


class WorkspaceGateway(Protocol):
    supports_canonical_writes: bool

    def get_file(self, file_id: str) -> dict[str, Any]: ...

    def get_document(self, file_id: str) -> dict[str, Any]: ...

    def get_spreadsheet(
        self, file_id: str, ranges: tuple[str, ...] = ()
    ) -> dict[str, Any]: ...

    def batch_update_document(
        self,
        file_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str,
    ) -> dict[str, Any]: ...

    def batch_update_sheet_values(
        self,
        file_id: str,
        data: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


class GoogleWorkspaceGateway:
    """Small direct Google API client with injected OAuth bearer token.

    Authentication and token refresh deliberately remain outside the adapter so
    desktop, CLI, and hosted deployments can supply credentials differently.
    """

    supports_canonical_writes = True

    def __init__(self, access_token: str, timeout: float = 60) -> None:
        if not access_token:
            raise ValueError("an OAuth access token is required")
        self.access_token = access_token
        self.timeout = timeout

    def get_file(self, file_id: str) -> dict[str, Any]:
        fields = "id,name,mimeType,modifiedTime,version,parents,webViewLink"
        return self._request(
            "GET",
            f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(file_id)}",
            query={"fields": fields, "supportsAllDrives": "true"},
        )

    def get_document(self, file_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"https://docs.googleapis.com/v1/documents/{urllib.parse.quote(file_id)}",
            query={"includeTabsContent": "true"},
        )

    def get_spreadsheet(
        self, file_id: str, ranges: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(file_id)
        metadata = self._request(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{encoded_id}",
            query={"fields": "spreadsheetId,properties,sheets(properties)"},
        )
        values: dict[str, list[list[Any]]] = {}
        if ranges:
            payload = self._request(
                "GET",
                f"https://sheets.googleapis.com/v4/spreadsheets/{encoded_id}/values:batchGet",
                query=[
                    ("majorDimension", "ROWS"),
                    ("valueRenderOption", "FORMATTED_VALUE"),
                    *(("ranges", item) for item in ranges),
                ],
            )
            returned = payload.get("valueRanges", [])
            if len(returned) != len(ranges):
                raise ValueError("Google Sheets returned an unexpected number of ranges")
            for requested, value_range in zip(ranges, returned, strict=True):
                # Preserve the manifest's spelling. Google may canonicalise A1
                # notation (especially quotes around tab names) in its response.
                values[requested] = value_range.get("values", [])
        return {"metadata": metadata, "ranges": values}

    def batch_update_document(
        self,
        file_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"https://docs.googleapis.com/v1/documents/{urllib.parse.quote(file_id)}:batchUpdate",
            body={
                "requests": requests,
                "writeControl": {"requiredRevisionId": required_revision_id},
            },
        )

    def batch_update_sheet_values(
        self,
        file_id: str,
        data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"https://sheets.googleapis.com/v4/spreadsheets/{urllib.parse.quote(file_id)}/values:batchUpdate",
            body={"valueInputOption": "RAW", "data": data},
        )

    def _request(
        self,
        method: str,
        url: str,
        query: dict[str, str] | list[tuple[str, str]] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if query:
            url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


class FixtureWorkspaceGateway:
    """Replay a project-owned Workspace capture without network credentials."""

    supports_canonical_writes = False

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.writes: list[dict[str, Any]] = []

    @classmethod
    def from_file(cls, path: str | Path) -> "FixtureWorkspaceGateway":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def get_file(self, file_id: str) -> dict[str, Any]:
        return dict(self.payload["files"][file_id])

    def get_document(self, file_id: str) -> dict[str, Any]:
        return dict(self.payload["documents"][file_id])

    def get_spreadsheet(
        self, file_id: str, ranges: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        sheet = self.payload["spreadsheets"][file_id]
        available = sheet.get("ranges", {})
        if not ranges:
            selected = dict(available)
        else:
            selected = {item: available[item] for item in ranges}
        return {"metadata": sheet["metadata"], "ranges": selected}

    def batch_update_document(
        self,
        file_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str,
    ) -> dict[str, Any]:
        write = {
            "kind": "document",
            "file_id": file_id,
            "requests": requests,
            "required_revision_id": required_revision_id,
        }
        self.writes.append(write)
        return write

    def batch_update_sheet_values(
        self,
        file_id: str,
        data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        write = {"kind": "sheet", "file_id": file_id, "data": data}
        self.writes.append(write)
        return write
