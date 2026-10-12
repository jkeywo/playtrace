from datetime import UTC, datetime

from playtrace.docs_adapter import extract_document_records
from playtrace.manifest import SheetTableContract
from playtrace.sheets_adapter import extract_sheet_records


def test_google_doc_normalization_preserves_explicit_and_derived_identity() -> None:
    document = {
        "tabs": [
            {
                "tabProperties": {"tabId": "t.0", "title": "Rules"},
                "body": {
                    "content": [
                        _paragraph(1, "[PT-ID:test:rule:turn] [WORKING] Resolve pressure after each turn.\n"),
                        _paragraph(80, "[OPEN QUESTION] Does this scale by player count?\n"),
                    ]
                },
            }
        ]
    }

    records = extract_document_records(
        document, "source:rules", "source:rules@r1", "test"
    )

    assert records.entities[0].id == "test:rule:turn"
    assert records.entities[0].attributes["identity_origin"] == "explicit"
    assert records.entities[1].id.startswith("source:rules:paragraph:")
    assert [item.authored_status.value for item in records.assertions] == [
        "working",
        "open_question",
    ]
    assert records.assertions[0].provenance.source_revision_id == "source:rules@r1"


def test_google_sheet_normalization_uses_visible_id_column() -> None:
    contract = SheetTableContract(
        sheet="Cards",
        range="Cards!A1:D10",
        entity_kind="card",
        entity_id_column="Card ID",
        name_column="Name",
        status_column="Status",
    )
    ranges = {
        "Cards!A1:D10": [
            ["Card ID", "Name", "Status", "Rules Text"],
            ["CARD-001", "Brace", "WORKING", "Prevent one Worsen."],
        ]
    }

    records = extract_sheet_records(
        ranges, (contract,), "source:cards", "source:cards@7", "test"
    )

    assert records.entities[0].id == "CARD-001"
    assert records.entities[0].attributes["identity_origin"] == "visible_column"
    assert records.assertions[0].predicate == "rules_text"
    assert records.assertions[0].provenance.location == "sheet:Cards:row:2:column:Rules Text"


def _paragraph(start: int, text: str) -> dict[str, object]:
    end = start + len(text)
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": text},
                }
            ],
        },
    }

