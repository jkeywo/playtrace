"""Normalize schema-bound Google Sheet rows into stable design records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from playtrace.manifest import SheetTableContract
from playtrace.models import Assertion, AuthoredStatus, DesignEntity, ProvenanceRef
from playtrace.sheet_ranges import parse_sheet_range


@dataclass(frozen=True)
class SheetRecords:
    entities: tuple[DesignEntity, ...]
    assertions: tuple[Assertion, ...]
    omissions: tuple[str, ...] = ()


def extract_sheet_records(
    ranges: dict[str, list[list[Any]]],
    contracts: tuple[SheetTableContract, ...],
    source_id: str,
    source_revision_id: str,
    extractor: str,
) -> SheetRecords:
    entities: list[DesignEntity] = []
    assertions: list[Assertion] = []
    omissions: list[str] = []
    seen_ids: set[str] = set()
    for contract in contracts:
        target = parse_sheet_range(contract.range, require_sheet=True)
        if target.sheet != contract.sheet:
            raise ValueError("table sheet name does not match its A1 range")
        rows = ranges.get(contract.range)
        if rows is None:
            raise ValueError(f"fixture/API response omitted declared range {contract.range!r}")
        if not rows:
            continue
        headers = [str(value).strip() for value in rows[0]]
        if len(headers) > target.shape[1]:
            raise ValueError(f"{contract.sheet} has headers outside its declared range")
        if any(not header for header in headers) or len(headers) != len(set(headers)):
            raise ValueError(f"{contract.sheet} has blank or duplicate column headers")
        slugs = [_slug(header) for header in headers]
        if len(slugs) != len(set(slugs)):
            raise ValueError(f"{contract.sheet} has ambiguous column identities")
        required = {contract.entity_id_column, contract.name_column}
        missing = sorted(required - set(headers))
        if missing:
            raise ValueError(f"{contract.sheet} is missing required columns: {missing}")
        for row_number, values in enumerate(rows[1:], start=target.row + 1):
            if row_number > target.end_row:
                raise ValueError(f"{contract.sheet} has values outside its declared table")
            if any(value not in (None, "") for value in values[len(headers):]):
                omissions.append(
                    f"{source_id}: sheet {contract.sheet}, row {row_number} has "
                    "unheaded trailing values omitted from the declared table."
                )
            record = {
                header: values[index] if index < len(values) else None
                for index, header in enumerate(headers)
            }
            raw_id = record.get(contract.entity_id_column)
            if raw_id in (None, ""):
                continue
            entity_id = str(raw_id).strip()
            if not entity_id or entity_id in seen_ids:
                raise ValueError(f"{contract.sheet} has an empty or duplicate entity ID: {entity_id!r}")
            seen_ids.add(entity_id)
            name = str(record.get(contract.name_column) or entity_id).strip()
            status = _parse_status(record.get(contract.status_column)) if contract.status_column else AuthoredStatus.UNSPECIFIED
            entities.append(
                DesignEntity(
                    id=entity_id,
                    kind=contract.entity_kind,
                    name=name,
                    attributes={
                        "fields": record,
                        "identity_origin": "visible_column",
                        "sheet": contract.sheet,
                        "row": row_number,
                        "authored_status": status.value,
                    },
                )
            )
            for column, value in record.items():
                if column in {contract.entity_id_column, contract.name_column, contract.status_column}:
                    continue
                if value in (None, ""):
                    continue
                assertions.append(
                    Assertion(
                        id=f"{entity_id}:field:{_slug(column)}",
                        subject_id=entity_id,
                        predicate=_slug(column),
                        value=value,
                        authored_status=status,
                        provenance=ProvenanceRef(
                            source_revision_id=source_revision_id,
                            location=f"sheet:{contract.sheet}:row:{row_number}:column:{column}",
                            extractor=extractor,
                        ),
                    )
                )
    return SheetRecords(tuple(entities), tuple(assertions), tuple(omissions))


def _parse_status(value: Any) -> AuthoredStatus:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    try:
        return AuthoredStatus(normalized)
    except ValueError:
        return AuthoredStatus.UNSPECIFIED


def _slug(value: str) -> str:
    return "_".join(value.strip().lower().replace("/", " ").replace(":", " ").split())
