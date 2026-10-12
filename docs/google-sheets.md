# Google Drive and Sheets support

Play Trace reads native Google Sheets registered in a game's `playtrace.toml`,
captures Drive revision metadata, and turns explicitly declared tables into
traceable design records. Approved literal-cell changes can be applied in an
explicit single-editor mode with a durable recovery journal.

## Connect a game

Enable the Google Drive and Google Sheets APIs in the OAuth application's Google
Cloud project. Supply a valid OAuth bearer token through an environment variable;
tokens and refresh credentials do not belong in the manifest or repository.
Reading requires access to the relevant files and read scopes for Drive metadata
and Sheets. Applying changes additionally requires the `spreadsheets` write scope
and edit access. A game using Google Docs also needs the corresponding Docs scope.
Token acquisition and refresh remain the responsibility of the calling application.

Example game-owned `playtrace.toml` (replace the uppercase identifiers):

```toml
manifest_version = 1
project_id = "my-game"
name = "My Game"
repository = "."
drive_folder_id = "DRIVE_FOLDER_ID"

[[sources]]
id = "cards"
file_id = "SPREADSHEET_ID"
kind = "google_sheet"
role = "cards"
title = "Card Library"
schema_id = "cards-v1"

[[sources.tables]]
sheet = "Card Library"
range = "'Card Library'!A4:D100"
entity_kind = "card"
entity_id_column = "Card ID"
name_column = "Name"
status_column = "Status"
```

The first row of each declared range is its header. This example expects
`Card ID`, `Name`, `Status`, and a fourth field such as `Rules Text` in row 4.
Data begins in row 5, and provenance retains those actual worksheet row numbers.
Use bounded, tab-qualified A1 ranges of at most 10,000 cells. Tab names containing
apostrophes use doubled apostrophes inside the quotes. IDs and column identities
must be unambiguous; duplicate IDs, duplicate headers and colliding field names
fail ingestion instead of silently overwriting records.
Legacy captures with unheaded trailing values retain the declared fields and
record the excluded values as explicit snapshot omissions.

```powershell
uv run playtrace snapshot C:\Games\my-game --access-token-env PLAYTRACE_GOOGLE_TOKEN `
  --db C:\Games\my-game\.playtrace\playtrace.db `
  --output C:\Games\my-game\.playtrace\snapshot.json
uv run playtrace sync C:\Games\my-game --access-token-env PLAYTRACE_GOOGLE_TOKEN `
  --db C:\Games\my-game\.playtrace\playtrace.db
```

Snapshots check provider metadata before and after each source fetch, retrying
up to three times if a revision moves. Sync also fetches newly declared ranges
when the local manifest changes without a Google file revision change. Snapshot
table content uses displayed values; formula execution remains Google's concern.

## Apply an approved change

Use the existing `propose`, `submit-proposal`, `decide-proposal`, and
`dry-run-proposal` lifecycle described in [M5](M5.md). A `SheetValuesPatch` binds
the source ID, spreadsheet ID, Drive version, exact A1 range, expected values,
and replacement values to the approved proposal. Numeric and boolean patch
values use native JSON types, not their formatted display strings.

After reviewing and approving the proposal, pause other human and automated
editors of the spreadsheet for the application and any recovery:

```powershell
uv run playtrace apply-proposal C:\Games\my-game `
  --proposal-id PROPOSAL_ID --actor designer `
  --db C:\Games\my-game\.playtrace\playtrace.db `
  --access-token-env PLAYTRACE_GOOGLE_TOKEN --sheet-write-mode single-editor
```

The default mode (`atomic`) continues to refuse live Sheets writes. Google
provides no expected-value or revision precondition for
[values.batchUpdate](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/batchUpdate).
Single-editor mode checks cells immediately before the write and verifies them
afterward, but cannot prevent an intervening edit. It is not a lock and does not
provide an atomic compare-and-write guarantee. This limitation also applies to
compensation. The selected mode is persisted before transport in each saga operation.

Supported writes are literal strings, finite numbers within the supported JSON
number range, booleans and blank cells. Values are sent as `RAW`, preserving
types without interpreting formulas. Formula cells and strings beginning with
`=` are rejected. Formatting is preserved. Patches replace their whole bounded
rectangle: omitted trailing cells become explicit empty strings, so clearing a
cell and restoring a blank before-image work reliably. `null`, overlapping
patches, whole-column ranges and unqualified live patch ranges are rejected.

## Recover an interrupted application

```powershell
uv run playtrace recover --saga-id SAGA_ID `
  --db C:\Games\my-game\.playtrace\playtrace.db `
  --access-token-env PLAYTRACE_GOOGLE_TOKEN --sheet-write-mode single-editor
```

Recovery requires the write mode originally recorded in the journal. It observes
the current cells, accepts an exact already-restored before-image, or restores
the before-image only when the current cells still match the intended after-image.
An observed external edit leaves the saga `recovery_failed` for inspection.
Unknown transport outcomes follow the same process; they are not blindly retried.

Tests exercise production Google request/response shapes with synthetic data,
including type preservation, blanks, stale content, formula rejection, lost
responses and recovery after restart. They do not assert live account connectivity.
