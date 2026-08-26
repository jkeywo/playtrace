# Reasoning workflow

Play Trace uses a typed workflow rather than letting a model redefine the
process for each request.

## Default flow

1. **Capture intent.** Record the request, type, scope, persistence, desired
   outcome, and what it does not imply.
2. **Resolve the project repository and revisions.** Load game-specific
   configuration from that game's Git repository, then establish which canonical
   source revisions the work will reason about.
3. **Build or select a snapshot.** Include relevant assertions, entities,
   relationships, conflicts, and known omissions.
4. **Determine mandatory coverage.** Load applicable intent, hard constraints,
   policy, and evidence before qualitative criticism.
5. **State claims or hypotheses.** Separate observations, interpretations,
   recommendations, and testable predictions.
6. **Gather evidence.** Attach supporting, contradicting, and contextual
   material at claim level; label unsupported judgment.
7. **Preserve alternatives and disagreement.** Do not average away legitimate
   trade-offs.
8. **Create a ChangeSet only when requested or warranted.** Diagnosis may end
   with findings.
9. **Validate and review.** Show semantic effects, affected entities, evidence,
   assumptions, and source revisions.
10. **Apply only after explicit approval.** Re-check canonical revisions, make
    bounded changes, rebuild, and verify.

## Ambiguity rule

- If ambiguity changes executable meaning, stop that branch and seek a source
  clarification. Do not silently choose a mechanic.
- If uncertainty is a legitimate design trade-off, continue far enough to show
  the alternatives and their evidence, then leave the choice for review.

## Request persistence

An ordinary request is transactional. For example, “try resolving a crisis card
once per round” may create an experiment or proposal. It does not create the
policy “all cooperative games should use round-based pressure.” Project policy,
shared policy, and precedent require explicit separately reviewed artefacts.

## Review gates

### Analysis review

Used for claims, hypotheses, alternatives, and suggested direction. It can end
without a change.

Experiment results enter this review as explicitly hypothesis-linked evidence.
Recorded dependency revisions and digests determine whether that evidence is
current or stale. Stale evidence remains visible but cannot satisfy current
evidence coverage.

A human evidence decision (`pending`, `accepted`, `rejected`, or `revise`) is
distinct from design-change approval. It records how the evidence should inform
future reasoning and does not mutate canonical sources, approve a hypothesis,
or create a ChangeSet.

### Design-change review

Used for a proposed semantic change to canonical design content. Approval is
specific to the ChangeSet, semantic patch, complete evidence/policy basis, and
known source revisions. The basis is content-digested; changing any embedded
record requires a new approval. Proposal transitions and application attempts
are append-only.

Approval does not write. An explicit apply operation rechecks the basis and
native revisions, dry-runs every patch, then writes. It rebuilds the snapshot
and runs required postconditions and behavioral regressions. Write failures and
post-write validation failures remain distinct because the latter means canon
changed but did not pass the gate.

### Implementation review

Used when executable code or generated artefacts implement an approved design.
It verifies that implementation matches the accepted direction. Design approval
does not imply implementation approval.

## Durable learning

Only an applied-and-validated proposal can be promoted into accepted precedent,
and promotion is an explicit operation into the owning game's repository.
Approved and adversarial behavioral cases are deterministic regressions;
rejected cases are excluded. Generated plugin, grammar, and RuleIR suggestions
remain inert proposals until implemented through the normal reviewed code path.

## Initial implementation boundary

M0 introduced the graph and immutable workflow-run state. M5 now orchestrates
approval-gated bounded writes, rebuild validation, regressions, and precedent
promotion. Multi-file compensating transactions, production synchronization,
and hosted operations remain M6 concerns.
