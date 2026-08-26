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


def utf16_code_unit_length(text: str) -> int:
    """Return the number of UTF-16 code units used by Google Docs indices.

    Google Docs ranges are not Python string offsets: non-BMP characters occupy
    two provider indices.  Lone surrogate code points are rejected because they
    cannot represent valid document text safely.
    """

    units = 0
    for character in text:
        value = ord(character)
        if 0xD800 <= value <= 0xDFFF:
            raise ValueError("document text contains an unpaired UTF-16 surrogate")
        units += 2 if value > 0xFFFF else 1
    return units


def python_offset_to_utf16_index(text: str, offset: int) -> int:
    """Translate a Python character offset to a UTF-16 code-unit offset."""

    if offset < 0 or offset > len(text):
        raise ValueError("Python text offset is outside the string")
    return utf16_code_unit_length(text[:offset])


def utf16_index_to_python_offset(text: str, index: int) -> int:
    """Translate a UTF-16 code-unit offset to an exact Python boundary."""

    if index < 0:
        raise ValueError("UTF-16 text index cannot be negative")
    units = 0
    for offset, character in enumerate(text):
        value = ord(character)
        if 0xD800 <= value <= 0xDFFF:
            raise ValueError("document text contains an unpaired UTF-16 surrogate")
        if units == index:
            return offset
        width = 2 if value > 0xFFFF else 1
        if units < index < units + width:
            raise ValueError("UTF-16 text index splits a surrogate pair")
        units += width
    if units == index:
        return len(text)
    raise ValueError("UTF-16 text index is outside the string")


def replace_text_in_utf16_range(text: str, start_index: int, end_index: int, value: str) -> str:
    """Replace an exact UTF-16 range in a Python string."""

    start = utf16_index_to_python_offset(text, start_index)
    end = utf16_index_to_python_offset(text, end_index)
    return text[:start] + value + text[end:]


def document_tab_text_bounds(document: dict[str, Any], tab_id: str) -> tuple[int, int]:
    """Return the exact provider-index extent occupied by text in one tab."""

    runs = _document_tab_text_runs(document, tab_id)
    if not runs:
        raise ValueError(f"Google Docs tab {tab_id!r} has no text ranges")
    return runs[0][0], runs[-1][1]


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
            end = int(structural.get("endIndex", start + utf16_code_unit_length(text)))
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
    if start_index < 0 or end_index < start_index:
        raise ValueError("invalid Google Docs UTF-16 range")
    runs = _document_tab_text_runs(document, tab_id)
    if not runs:
        raise ValueError(f"Google Docs tab {tab_id!r} has no text ranges")

    if start_index == end_index:
        if not any(start <= start_index <= end for start, end, _ in runs):
            raise ValueError("Google Docs UTF-16 range is outside the tab text")
        for element_start, element_end, text in runs:
            if element_start <= start_index <= element_end:
                utf16_index_to_python_offset(text, start_index - element_start)
                return ""

    chunks: list[str] = []
    cursor = start_index
    for element_start, element_end, text in runs:
        overlap_start = max(start_index, element_start)
        overlap_end = min(end_index, element_end)
        if overlap_start >= overlap_end:
            continue
        if overlap_start != cursor:
            raise ValueError("Google Docs UTF-16 range crosses a non-text boundary")
        relative_start = utf16_index_to_python_offset(text, overlap_start - element_start)
        relative_end = utf16_index_to_python_offset(text, overlap_end - element_start)
        chunks.append(text[relative_start:relative_end])
        cursor = overlap_end
    if cursor != end_index:
        raise ValueError("Google Docs UTF-16 range is outside the tab text")
    return "".join(chunks)


def _document_tab_text_runs(
    document: dict[str, Any], tab_id: str
) -> tuple[tuple[int, int, str], ...]:
    tabs = document.get("tabs") or []
    matches = []
    for tab in tabs:
        properties = tab.get("tabProperties", tab)
        if properties.get("tabId", tab.get("tabId")) == tab_id:
            matches.append(tab)
    if len(matches) != 1:
        if not matches:
            raise ValueError(f"Google Docs tab {tab_id!r} was not found")
        raise ValueError(f"Google Docs tab {tab_id!r} is ambiguous")

    runs: list[tuple[int, int, str]] = []
    for structural in (matches[0].get("body") or {}).get("content", []):
        for element in structural.get("paragraph", {}).get("elements", []):
            text = element.get("textRun", {}).get("content", "")
            if not text:
                continue
            element_start = int(element.get("startIndex", structural.get("startIndex", 0)))
            text_end = element_start + utf16_code_unit_length(text)
            element_end = int(element.get("endIndex", text_end))
            if element_end != text_end:
                raise ValueError("Google Docs textRun extent does not match its UTF-16 text")
            runs.append((element_start, element_end, text))
    runs.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(runs, runs[1:]):
        if current[0] < previous[1]:
            raise ValueError("Google Docs tab contains overlapping text ranges")
    return tuple(runs)


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
