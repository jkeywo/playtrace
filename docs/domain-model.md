# Domain model

This is the conceptual contract for M0. Names may change during implementation,
but their distinctions must survive.

## Traceability chain

```text
Canonical source
  -> observed SourceRevision
    -> source-authored Assertion
      -> derived DesignSnapshot
        -> EvidenceLink -> Claim or Hypothesis
          -> proposed ChangeSet
            -> human Decision
```

Every arrow must be inspectable. A later record may summarise an earlier one,
but must not erase its identity or status.

## Core records

### Project

Identifies the game or design backend being reasoned about and declares relevant
capabilities. A project label such as `tabletop` is a profile, not a license to
scatter medium-specific conditionals throughout the core.

Minimum fields: stable ID, name, canonical-source policy, capabilities, and
project-repository identity. Project-local policy layers are loaded from that
game's repository, never stored as working state in the Play Trace repository.

### Source

An authoritative or evidential artefact. Authority is scoped: the Crisis
Wardens rulebook can be authoritative evidence for what the rulebook says while
remaining provisional as a rules specification.

Minimum fields: stable ID, role, canonical locator, medium/MIME type, authority
scope, and project.

### SourceRevision

One observed state of a Source. It records provider identity and observed
revision information rather than assuming the latest file is the file that was
analysed.

Minimum fields: source ID, provider file ID, provider revision or modified time,
capture time, content digest when available, and extraction version.

### SourceLocation

An address within a SourceRevision: named range, stable row/entity metadata,
sheet and row, document heading and paragraph, rule ID, card ID, or another
medium-appropriate anchor. Human-friendly labels may accompany but not replace
stable anchors when those exist.

### Assertion

A normalized statement that a source makes. An assertion is not automatically
true and not automatically endorsed by Play Trace.

Minimum fields: stable ID, subject, predicate, value/object, source revision,
source location, verbatim or faithful source excerpt, authored status, and
extraction method.

### AuthoredStatus

The source author's expressed design state:

| Status | Meaning |
|---|---|
| `confirmed` | Current design commitment; still revisable through evidence and decision. |
| `working` | Current hypothesis or active rule being tested. |
| `placeholder` | Temporary content or mechanism that makes a prototype possible. |
| `open_question` | A known unresolved decision, not a defective assertion. |
| `deferred` | Intentionally excluded from the current design slice. |
| `unspecified` | The source makes no explicit status claim. |

This field must remain separate from extraction confidence, evidence strength,
current applicability, and human approval.

### DesignEntity

A stable thing the design reasons about: a game, scenario, character, card,
keyword, rule, design pillar, design question, or subsystem. Different sources
may assert different things about the same entity.

### DesignSnapshot

An immutable, derived view of selected entities, assertions, relationships, and
capabilities at known source revisions. It records omissions and extraction
limits. It is rebuildable and never canonical game truth.

### Conflict

A detected incompatibility or meaningful discrepancy between assertions. A
conflict records its participants, kind, and detection method. It does not
silently select a winner. Staleness is one possible explanation, not the default
verdict.

### EvidenceItem

Material that may bear on a claim: a source assertion, user request, design
policy, precedent, playtest observation, experiment, simulation result,
telemetry result, invariant, screenshot/replay, external reference, or explicit
designer/critic judgment.

### Claim

A conclusion offered for review. Material claims enumerate their evidence links
and scope. A Claim is not strengthened merely by having many weak links.

### EvidenceLink

Relates one EvidenceItem to one Claim as:

- `supports` — increases reason to accept the claim;
- `contradicts` — supplies counter-evidence;
- `context` — affects interpretation or applicability without deciding it; or
- `judgment` — identifies an unsupported or irreducibly qualitative judgment.

The link may state assumptions, applicability, and limits.

### Hypothesis

An inspectable prediction or design proposition, including how it could be
tested and what evidence would count for or against it. Hypotheses may contain
mechanical and experiential parts with different evaluation methods.

### Intent

A typed interpretation of the user's current request. Minimum fields include
request type, scope, persistence, desired outcome, and explicit exclusions.
Persistence defaults to `transactional`.

### ChangeSet

A proposed group of semantic changes with rationale, affected entities, claims,
evidence, expected consequences, and validation. Creation of a ChangeSet does
not edit a canonical source.

### Decision

A human review result—accepted, rejected, or deferred—linked to the exact
proposal and evidence state reviewed. Durable policy or precedent is created
only by an explicit accepted artefact intended for that purpose.

## Separate dimensions that must not be collapsed

| Dimension | Question |
|---|---|
| Authored status | How settled did the source author say this was? |
| Extraction confidence | How reliably did Play Trace read or normalize it? |
| Evidence relation | How does this item bear on this claim? |
| Applicability | Does the item still apply to the current entities and assumptions? |
| Decision state | Has a human accepted, rejected, or deferred a proposal? |
| Persistence | Is this request transactional, project policy, or shared policy? |

## Pilot identity convention

M0 uses readable namespaced IDs in examples:

```text
project:crisis-wardens
cw:scenario:the-flooded-vale
cw:character:orra
cw:card:SCD-001
cw:rule:improve-worsen
cw:question:activated-card-disposition
```

Provider IDs remain attached to sources and revisions. They are not reused as
domain IDs because the same entity may be discussed across several files.

These example IDs would be defined in the Crisis Wardens repository. They are
shown here to document the shared namespacing convention, not to make this
repository their owner.
