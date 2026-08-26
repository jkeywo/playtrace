"""Normalize native Google Docs structures into traceable design records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from playtrace.canonical import canonical_digest
from playtrace.models import (
    Assertion,
    AuthoredStatus,
    DesignEntity,
    ProvenanceRef,
)


STATUS_PATTERN = re.compile(
    r"^\[(CONFIRMED|WORKING|PLACEHOLDER|OPEN QUESTION|DEFERRED)\]\s*",
    re.IGNORECASE,
)
STABLE_ID_PATTERN = re.compile(r"\[PT-ID:([A-Za-z0-9:._-]+)\]\s*")

STATUS_MAP = {
    "CONFIRMED": AuthoredStatus.CONFIRMED,
    "WORKING": AuthoredStatus.WORKING,
    "PLACEHOLDER": AuthoredStatus.PLACEHOLDER,
    "OPEN QUESTION": AuthoredStatus.OPEN_QUESTION,
    "DEFERRED": AuthoredStatus.DEFERRED,
}


@dataclass(frozen=True)
class DocumentRecords:
    entities: tuple[DesignEntity, ...]
    assertions: tuple[Assertion, ...]
    cnl_lines: tuple[str, ...]


def extract_document_records(
    document: dict[str, Any],
    source_id: str,
    source_revision_id: str,
    extractor: str,
) -> DocumentRecords:
    entities: list[DesignEntity] = []
    assertions: list[Assertion] = []
    cnl_lines: list[str] = []

    tabs = document.get("tabs") or [
        {"tabProperties": {"tabId": "legacy", "title": "Document"}, "body": document.get("body")}
    ]
    for tab in tabs:
        properties = tab.get("tabProperties", tab)
        tab_id = properties.get("tabId", tab.get("tabId", "legacy"))
        tab_title = properties.get("title", tab.get("title", "Document"))
        body = tab.get("body") or {}
        content = body.get("content") or _paragraph_fixture_to_content(tab.get("paragraphs", []))
        for structural in content:
            paragraph = structural.get("paragraph")
            if not paragraph:
                continue
            text = "".join(
                element.get("textRun", {}).get("content", "")
                for element in paragraph.get("elements", [])
            ).rstrip("\n")
            if not text.strip():
                continue
            start = int(structural.get("startIndex", 0))
            end = int(structural.get("endIndex", start + len(text)))
            named_style = paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
            explicit_match = STABLE_ID_PATTERN.search(text)
            if explicit_match:
                entity_id = explicit_match.group(1)
                identity_origin = "explicit"
                text = STABLE_ID_PATTERN.sub("", text, count=1)
            else:
                fingerprint = canonical_digest(
                    {
                        "source_id": source_id,
                        "tab_id": tab_id,
                        "text": text.strip(),
                    },
                    domain="document-paragraph-id",
                )[:16]
                entity_id = f"{source_id}:paragraph:{fingerprint}"
                identity_origin = "derived"

            status_match = STATUS_PATTERN.match(text)
            authored_status = AuthoredStatus.UNSPECIFIED
            statement = text.strip()
            if status_match:
                authored_status = STATUS_MAP[status_match.group(1).upper()]
                statement = text[status_match.end() :].strip()

            kind = "heading" if named_style.startswith("HEADING") or named_style == "TITLE" else "passage"
            if statement.startswith("RULE "):
                kind = "rule"
                cnl_lines.append(statement)
            location = f"doc:{source_id}:tab:{tab_id}:range:{start}-{end}"
            entities.append(
                DesignEntity(
                    id=entity_id,
                    kind=kind,
                    name=statement[:120],
                    attributes={
                        "text": statement,
                        "authored_status": authored_status.value,
                        "identity_origin": identity_origin,
                        "tab_id": tab_id,
                        "tab_title": tab_title,
                        "start_index": start,
                        "end_index": end,
                        "named_style": named_style,
                    },
                )
            )
            if authored_status is not AuthoredStatus.UNSPECIFIED or kind == "rule":
                assertions.append(
                    Assertion(
                        id=f"{entity_id}:assertion",
                        subject_id=entity_id,
                        predicate="states",
                        value=statement,
                        authored_status=authored_status,
                        provenance=ProvenanceRef(
                            source_revision_id=source_revision_id,
                            location=location,
                            extractor=extractor,
                        ),
                    )
                )

    return DocumentRecords(tuple(entities), tuple(assertions), tuple(cnl_lines))


def text_in_document_range(
    document: dict[str, Any], tab_id: str, start_index: int, end_index: int
) -> str:
    chunks: list[str] = []
    tabs = document.get("tabs") or []
    for tab in tabs:
        properties = tab.get("tabProperties", tab)
        if properties.get("tabId", tab.get("tabId")) != tab_id:
            continue
        for structural in (tab.get("body") or {}).get("content", []):
            for element in structural.get("paragraph", {}).get("elements", []):
                text = element.get("textRun", {}).get("content", "")
                element_start = int(element.get("startIndex", structural.get("startIndex", 0)))
                element_end = int(element.get("endIndex", element_start + len(text)))
                overlap_start = max(start_index, element_start)
                overlap_end = min(end_index, element_end)
                if overlap_start < overlap_end:
                    chunks.append(text[overlap_start - element_start : overlap_end - element_start])
    return "".join(chunks)


def _paragraph_fixture_to_content(paragraphs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "startIndex": item["startIndex"],
            "endIndex": item["endIndex"],
            "paragraph": {
                "paragraphStyle": {"namedStyleType": item.get("namedStyleType", "NORMAL_TEXT")},
                "elements": [
                    {
                        "startIndex": item["startIndex"],
                        "endIndex": item["endIndex"],
                        "textRun": {"content": item["text"] + "\n"},
                    }
                ],
            },
        }
        for item in paragraphs
    ]

