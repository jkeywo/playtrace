"""Normalize schema-bound Google Sheet rows into stable design records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from playtrace.manifest import SheetTableContract
from playtrace.models import Assertion, AuthoredStatus, DesignEntity, ProvenanceRef


@dataclass(frozen=True)
class SheetRecords:
    entities: tuple[DesignEntity, ...]
    assertions: tuple[Assertion, ...]


def extract_sheet_records(
    ranges: dict[str, list[list[Any]]],
    contracts: tuple[SheetTableContract, ...],
    source_id: str,
    source_revision_id: str,
    extractor: str,
) -> SheetRecords:
    entities: list[DesignEntity] = []
    assertions: list[Assertion] = []
    for contract in contracts:
        rows = ranges.get(contract.range)
        if rows is None:
            raise ValueError(f"fixture/API response omitted declared range {contract.range!r}")
        if not rows:
            continue
        headers = [str(value).strip() for value in rows[0]]
        required = {contract.entity_id_column, contract.name_column}
        missing = sorted(required - set(headers))
        if missing:
            raise ValueError(f"{contract.sheet} is missing required columns: {missing}")
        for row_number, values in enumerate(rows[1:], start=2):
            record = {
                header: values[index] if index < len(values) else None
                for index, header in enumerate(headers)
            }
            raw_id = record.get(contract.entity_id_column)
            if raw_id in (None, ""):
                continue
            entity_id = str(raw_id).strip()
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
    return SheetRecords(tuple(entities), tuple(assertions))


def _parse_status(value: Any) -> AuthoredStatus:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    try:
        return AuthoredStatus(normalized)
    except ValueError:
        return AuthoredStatus.UNSPECIFIED


def _slug(value: str) -> str:
    return "_".join(value.strip().lower().replace("/", " ").replace(":", " ").split())

