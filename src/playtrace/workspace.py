"""Google Workspace transport boundary and deterministic fixture transport."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from playtrace.sheet_ranges import literal_matrix, parse_sheet_range, same_cells


class WorkspaceConcurrencyError(RuntimeError):
    """A workspace provider cannot safely satisfy a compare-and-write request."""


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

    def compare_and_batch_update_sheet_values(
        self,
        file_id: str,
        data: list[dict[str, Any]],
        expected_data: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    def get_start_page_token(self) -> str: ...

    def list_changes(self, page_token: str) -> dict[str, Any]: ...


class GoogleWorkspaceGateway:
    """Small direct Google API client with injected OAuth bearer token.

    Authentication and token refresh deliberately remain outside the adapter so
    desktop, CLI, and hosted deployments can supply credentials differently.
    """

    supports_canonical_writes = True

    def __init__(
        self, access_token: str, timeout: float = 60, *,
        sheet_write_mode: str = "atomic",
    ) -> None:
        if not access_token:
            raise ValueError("an OAuth access token is required")
        self.access_token = access_token
        self.timeout = timeout
        if sheet_write_mode not in {"atomic", "single-editor"}:
            raise ValueError("unknown sheet write mode")
        self.sheet_write_mode = sheet_write_mode

    def get_sheet_patch_values(self, file_id: str, range_name: str) -> tuple:
        target = parse_sheet_range(range_name, require_sheet=True)
        payload = self._request(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{urllib.parse.quote(file_id, safe='')}/values:batchGet",
            query=[("ranges", range_name), ("majorDimension", "ROWS"),
                   ("valueRenderOption", "FORMULA"), ("dateTimeRenderOption", "SERIAL_NUMBER")],
        )
        ranges = payload.get("valueRanges", [])
        if len(ranges) != 1:
            raise ValueError("Google Sheets omitted the requested patch range")
        return literal_matrix(ranges[0].get("values", []), target)

    def preflight_sheet_write(self, file_id: str, data: list[dict[str, Any]]) -> None:
        if self.sheet_write_mode != "single-editor":
            raise WorkspaceConcurrencyError(
                "Google Sheets has no atomic compare-and-write guard; "
                "use --sheet-write-mode single-editor only while other editors are paused"
            )
        for item in data:
            target = parse_sheet_range(item["range"], require_sheet=True)
            literal_matrix(item["values"], target)

    def observed_batch_update_sheet_values(
        self, file_id: str, data: list[dict[str, Any]],
        expected_data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Explicit single-editor read/check/write; this is NOT atomic CAS."""
        self.preflight_sheet_write(file_id, data)
        if [item["range"] for item in data] != [item["range"] for item in expected_data]:
            raise ValueError("every sheet write needs a matching expected range")
        normalized = []
        for item, expected in zip(data, expected_data, strict=True):
            target = parse_sheet_range(item["range"], require_sheet=True)
            wanted = literal_matrix(expected["values"], target)
            actual = self.get_sheet_patch_values(file_id, item["range"])
            if not same_cells(actual, wanted):
                raise WorkspaceConcurrencyError(f"sheet values changed before write in {item['range']}")
            normalized.append({"range": item["range"], "majorDimension": "ROWS",
                               "values": literal_matrix(item["values"], target)})
        response = self.batch_update_sheet_values(file_id, normalized)
        for item in normalized:
            if not same_cells(self.get_sheet_patch_values(file_id, item["range"]), item["values"]):
                raise WorkspaceConcurrencyError(f"sheet verification mismatch in {item['range']}")
        return {"provider_response": response, "sheet_write_mode": "single-editor"}

    def get_file(self, file_id: str) -> dict[str, Any]:
        fields = "id,name,mimeType,modifiedTime,version,parents,webViewLink,appProperties"
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

    def compare_and_batch_update_sheet_values(
        self,
        file_id: str,
        data: list[dict[str, Any]],
        expected_data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Fail closed because Sheets values writes have no compare-and-write guard.

        The Sheets v4 values batch-update endpoint cannot condition a mutation on
        the values or Drive version previously observed. Re-reading before the
        request merely narrows the race window, so it is deliberately not treated
        as optimistic concurrency.
        """

        del file_id, data, expected_data
        raise WorkspaceConcurrencyError(
            "Google Sheets values writes do not provide an atomic compare-and-write guard"
        )

    def get_start_page_token(self) -> str:
        payload = self._request(
            "GET",
            "https://www.googleapis.com/drive/v3/changes/startPageToken",
            query={"supportsAllDrives": "true"},
        )
        return str(payload["startPageToken"])

    def list_changes(self, page_token: str) -> dict[str, Any]:
        return self._request(
            "GET",
            "https://www.googleapis.com/drive/v3/changes",
            query={
                "pageToken": page_token,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "fields": (
                    "nextPageToken,newStartPageToken,"
                    "changes(fileId,removed,time,file(id,version,modifiedTime,appProperties))"
                ),
            },
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

    def compare_and_batch_update_sheet_values(
        self,
        file_id: str,
        data: list[dict[str, Any]],
        expected_data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Deterministic fixture implementation of the atomic provider contract."""

        available = self.payload["spreadsheets"][file_id].get("ranges", {})
        for expected in expected_data:
            range_name = str(expected["range"])
            actual = tuple(tuple(row) for row in available.get(range_name, []))
            wanted = tuple(tuple(row) for row in expected.get("values", []))
            if actual != wanted:
                raise WorkspaceConcurrencyError(
                    f"sheet values changed before compare-and-write in {range_name}"
                )
        # Fixture transports are single-threaded. This call represents the
        # provider-side atomic mutation after all predicates passed.
        return self.batch_update_sheet_values(file_id, data)

    def get_start_page_token(self) -> str:
        return str(self.payload.get("start_page_token", "fixture:0"))

    def list_changes(self, page_token: str) -> dict[str, Any]:
        return dict(
            self.payload.get("changes", {}).get(
                page_token, {"changes": [], "newStartPageToken": page_token}
            )
        )
