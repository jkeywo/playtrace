# Approved precedent cases

This directory contains genuinely cross-project, approved precedent cases.
Game-local cases belong in the game's `design/cases` directory.

A case is JSON matching `playtrace.knowledge.PrecedentCase`. It must include a
stable ID, snapshot, question, at least two alternatives, criteria, evidence,
critic disagreement, decision, rationale, consequences, applicability scope,
and an accepted/proposed/rejected/deferred state. Retrieval uses only accepted
cases and preserves the distinction between precedent and cross-medium analogy.
