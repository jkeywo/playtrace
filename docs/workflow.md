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

### Design-change review

Used for a proposed semantic change to canonical design content. Approval is
specific to the ChangeSet and known source revisions.

### Implementation review

Used when executable code or generated artefacts implement an approved design.
It verifies that implementation matches the accepted direction. Design approval
does not imply implementation approval.

## M0 implementation boundary

M0 implements the graph, immutable workflow-run state, and transition
validation. It does not yet orchestrate source synchronization, critics,
approval persistence, or canonical writes. The next integration milestone will
exercise the graph across the Play Trace/game-repository boundary; Drive
mutation remains outside that slice.
