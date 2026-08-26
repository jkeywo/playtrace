# M6A — deterministic identity

M6A gives every durable Play Trace identity one canonical encoding. It removes
Python hash-order, mapping insertion-order, operating-system path spelling and
local timezone representation from approval, evidence and simulation digests.

## Canonical envelope

`playtrace.canonical` encodes an identity as a compact UTF-8 JSON envelope:

```json
{
  "domain": "proposal-approval",
  "schema": "playtrace.identity",
  "value": {"t": "map", "v": []},
  "version": 1
}
```

The SHA-256 digest is taken over the complete envelope. The required domain
separates identities with different meanings: identical content used as a
simulation configuration and as an approval basis does not receive the same
digest. Changing the envelope rules requires a new version rather than an
in-place reinterpretation.

The `value` is a tagged algebra rather than ordinary JSON. Every scalar and
container has an explicit tag: `none`, `bool`, `int`, `float`, `str`,
`datetime`, `date`, `uuid`, `path`, `bytes`, `enum`, `model`, `map`,
`list`, `tuple`, `set`, or `frozenset`. User maps are encoded as tagged,
ordered key/value entries, so a user-authored object such as
`{"t":"datetime","v":"..."}` cannot impersonate an encoder tag.

The encoder provides these guarantees:

- mapping keys and unordered sets are sorted by canonical content;
- Pydantic models and enums bind their qualified class as well as their value;
- lists, tuples, sets, and frozensets are intentionally distinct, while every
  typed Pydantic field recovers its declared collection after persistence;
- aware datetimes are converted to UTC and paths use `/` separators;
- datetime, UUID, path, bytes, enum, and model nodes cannot collide with plain
  user JSON that happens to contain the same spelling;
- NaN, infinity, naive datetimes, all standalone `time` values, non-string
  mapping keys, cycles, unsupported objects and lone Unicode surrogates are
  rejected. Date-free times are deliberately unsupported because timezone
  normalization can depend on the missing date.

Cross-process tests exercise multiple `PYTHONHASHSEED` values. JSON and SQLite
round-trip tests establish that persisted models retain their identities.

## Domains

Stable design and runtime identities now use named domains including:

- `analysis-semantic`, `source-revision-content`, `tabletop-snapshot-id`,
  `design-snapshot` and `document-paragraph-id`;
- `simulation-config`, `simulation-run-id`, `simulation-rng-seed` and
  `simulation-run-artifact`;
- `tension-batch-artifact` and `tension-comparison-artifact`;
- `proposal-basis`, `proposal-approval` and
  `proposal-approval-integrity`;
- `regression-case`, `regression-case-content`, `regression-suite`,
  `regression-run` and `regression-run-integrity`;
- `extension-content` and `extension-content-integrity`.

The old `simulation.content_digest` function remains as a compatibility wrapper
using the `generic-content` domain. New durable identities should call
`canonical_digest` with a specific domain.

External byte-integrity hashes are intentionally unchanged. In particular,
container dependency locks and exact Google Workspace before/after text images
remain ordinary SHA-256 hashes because they verify external bytes rather than
assigning Play Trace semantic identity.

## Historical approvals and accepted records

An old proposal's `basis_digest` is an audit fact: it is not silently replaced
even when its original JSON serialization included a hash-order-dependent
frozenset. Adoption is one database-wide epoch, not a side effect of loading an
individual record. When a pre-M6A database is opened, Play Trace starts one
SQLite transaction and:

1. enumerates every proposal, generated extension and regression run;
2. recomputes each legacy SHA-256 from its raw persisted JSON, preserving the
   array order written by the historical process;
3. validates every complete model and proposal/extension lifecycle before
   writing anything;
4. computes each canonical integrity seal over the historical digest and its
   identity-bound content;
5. writes all embedded seals and independent `(kind, id, schema, seal)` rows;
   and
6. only after every record succeeds, writes
   `identity_adoption_complete=playtrace.identity/1` to the separate
   `identity_metadata` table and commits.

Any failure rolls back payload rewrites, sidecars and the epoch marker as a
unit. A newly initialized database starts with the epoch already complete.
Reopening a completed database verifies records but never adopts them again.
A missing sidecar or payload seal, a digest-schema downgrade, or an unknown
adoption version is therefore corruption rather than permission to repeat
migration. If the epoch marker itself is absent, adoption proceeds only when
the database has no integrity sidecars, no other identity metadata, and no
embedded `canonical_seal` or `digest_schema` markers in any proposal,
extension, or regression run. Any evidence of partial or prior adoption fails
closed; Play Trace never attempts to repair or reseal it.

Application, persistence and precedent promotion all use the central
`approval_integrity_matches` check. Every later load requires the payload schema
and seal to match the external sidecar before it is usable. Removing the
payload seal, downgrading its schema, changing approval-bound content, changing
the historical digest, or altering the sidecar therefore fails closed instead
of reopening the one-time legacy-adoption path. The storage columns that repeat
artifact, project, suite, or digest identities must also match the sealed
payload.

Generated extension proposals and historical regression runs use the same
one-time seal pattern. Their historical content digests remain visible, while
future edits fail canonical integrity validation. Newly created records use the
version-1 canonical digest and receive their external sidecar in the same
transaction as their first write.

The base identity tables and adoption epoch belong to M6A's storage schema.
`PlayTraceStore` uses the full migration manager when M6C is installed, but can
initialize this base schema independently when that optional module is absent.
