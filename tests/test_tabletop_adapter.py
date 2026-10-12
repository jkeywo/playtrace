from playtrace.analysis import SnapshotAnalyzer
from playtrace.tabletop import TabletopProjectAdapter
from playtrace.workspace import FixtureWorkspaceGateway


def test_tabletop_adapter_snapshots_docs_sheets_and_compiled_cnl(tmp_path) -> None:
    manifest = tmp_path / "playtrace.toml"
    manifest.write_text(
        """
manifest_version = 1
project_id = "project:test-game"
name = "Test Game"
repository = "test/game"
drive_folder_id = "folder-1"
capabilities = ["controlled_language", "structured_content"]

[[sources]]
id = "source:rules"
file_id = "doc-1"
kind = "google_doc"
role = "rules"
title = "Rules"
schema_id = "docs-v1"

[[sources]]
id = "source:cards"
file_id = "sheet-1"
kind = "google_sheet"
role = "components"
title = "Cards"
schema_id = "cards-v1"

[[sources.tables]]
sheet = "Cards"
range = "Cards!A1:D10"
entity_kind = "card"
entity_id_column = "Card ID"
name_column = "Name"
status_column = "Status"
""".strip(),
        encoding="utf-8",
    )
    gateway = FixtureWorkspaceGateway(_fixture())

    snapshot = TabletopProjectAdapter.from_path(manifest, gateway).snapshot()
    report = SnapshotAnalyzer().analyze(snapshot)

    assert len(snapshot.source_revisions) == 2
    assert {entity.id for entity in snapshot.entities} >= {
        "test:rule:crisis",
        "CARD-001",
    }
    assert next(
        entity for entity in snapshot.entities if entity.id == "test:rule:crisis"
    ).attributes["ir"]["kind"] == "trigger"
    assert report.status_counts == {"unspecified": 1, "working": 2}
    assert all(item.provenance.source_revision_id in {rev.id for rev in snapshot.source_revisions} for item in snapshot.assertions)


def test_tabletop_adapter_checks_drive_manifest_before_ingestion(tmp_path) -> None:
    manifest = tmp_path / "playtrace.toml"
    manifest.write_text(
        """
project_id = "project:test-game"
name = "Test Game"
repository = "test/game"
drive_folder_id = "folder-1"
drive_manifest_file_id = "manifest-1"

[[sources]]
id = "source:rules"
file_id = "doc-1"
kind = "google_doc"
role = "rules"
title = "Rules"
schema_id = "docs-v1"
""".strip(),
        encoding="utf-8",
    )
    fixture = _fixture()
    fixture["files"]["manifest-1"] = {
        "id": "manifest-1",
        "name": "Manifest",
        "modifiedTime": "2026-01-01T00:00:00Z",
        "version": "1",
    }
    fixture["spreadsheets"]["manifest-1"] = {
        "metadata": {"spreadsheetId": "manifest-1"},
        "ranges": {
            "Manifest!A4:J100": [
                ["Source ID", "File ID", "Kind", "Role", "Schema"],
                ["source:rules", "doc-1", "google_doc", "rules", "docs-v1"],
            ]
        },
    }

    snapshot = TabletopProjectAdapter.from_path(
        manifest, FixtureWorkspaceGateway(fixture)
    ).snapshot()

    assert len(snapshot.source_revisions) == 2
    assert snapshot.source_revisions[0].source_id == "project:test-game:manifest"


def _fixture() -> dict[str, object]:
    rule = '[PT-ID:test:rule:crisis] [WORKING] RULE test:rule:crisis: WHEN "the turn ends" THEN "resolve one crisis card".\n'
    return {
        "files": {
            "doc-1": {
                "id": "doc-1",
                "name": "Rules",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "version": "3",
                "webViewLink": "https://docs.google.com/document/d/doc-1/edit",
            },
            "sheet-1": {
                "id": "sheet-1",
                "name": "Cards",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "version": "7",
                "webViewLink": "https://docs.google.com/spreadsheets/d/sheet-1/edit",
            },
        },
        "documents": {
            "doc-1": {
                "documentId": "doc-1",
                "revisionId": "doc-r1",
                "tabs": [
                    {
                        "tabProperties": {"tabId": "t.0", "title": "Rules"},
                        "body": {
                            "content": [
                                {
                                    "startIndex": 1,
                                    "endIndex": 1 + len(rule),
                                    "paragraph": {
                                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                        "elements": [
                                            {
                                                "startIndex": 1,
                                                "endIndex": 1 + len(rule),
                                                "textRun": {"content": rule},
                                            }
                                        ],
                                    },
                                }
                            ]
                        },
                    }
                ],
            }
        },
        "spreadsheets": {
            "sheet-1": {
                "metadata": {"spreadsheetId": "sheet-1"},
                "ranges": {
                    "Cards!A1:D10": [
                        ["Card ID", "Name", "Status", "Rules Text"],
                        ["CARD-001", "Brace", "WORKING", "Prevent one Worsen."],
                    ]
                },
            }
        },
    }
