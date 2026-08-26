# M6B — Observation-based write sagas

M6B makes canonical Google Docs and Sheets application recoverable without
guessing what a provider accepted. A live application now requires a SQLite
write-saga journal. The journal table belongs to the base Play Trace schema, so
the safety boundary does not depend on the later M6C operational migrations.

Each provider operation records a before-image, then durably transitions through
`unknown`, `returned`, and `verified`. `unknown` is written before transport: if
the process or connection dies after the provider accepted a request, recovery
observes canon and compares it with the exact before/after images. Compensation
runs in reverse order and verifies every restoration. Ambiguous or externally
changed canon remains `recovery_failed`; it is never guessed back into shape.

Sheets mutations require a provider-side atomic compare-and-write over every
target range. The fixture transport implements that contract for deterministic
testing. Google Sheets v4 exposes no value or Drive-version precondition on its
values update endpoint, so the live Google transport fails closed before issuing
a write. A read immediately before a write is not accepted as concurrency
control because it still leaves an observation/write race. Compensation uses
the same guard and therefore cannot overwrite an edit made after application.

Google Docs coordinates are interpreted strictly as UTF-16 code units. Patch
ranges must identify an existing tab, land on valid code-unit boundaries, match
the expected text extent, and stay within contiguous text runs. Recovery images
also preserve tab extents and trailing boundaries so shortening, expansion, and
overlapping text cannot be mistaken for an already-restored document.

`committed` and `recovered` are accepted as terminal only when their operation
facts prove the terminal claim. Legacy or corrupt terminal rows are atomically
adopted as `recovery_failed` for inspection. If a process commits a saga and dies
before saving the proposal attempt, the next application reconciles the durable
commit into one applied attempt before stale-basis checks; it does not write the
provider a second time.

## Verification

`tests/test_m6_write_sagas.py` covers the independently available base journal,
fail-closed live application, interrupted-commit reconciliation, terminal-state
adoption, strict tab/range validation, and UTF-16 surrogate handling. The older
M6 hardening tests continue to exercise multi-file compensation, unknown
transport outcomes, exact document restoration, and grouped same-document
writes.
