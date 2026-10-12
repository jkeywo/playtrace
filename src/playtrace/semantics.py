"""Inspectable provenance and strictness for executable game semantics."""

from __future__ import annotations

from enum import StrEnum

from playtrace.models import StrictModel


class SemanticOrigin(StrEnum):
    COMPILED = "compiled"
    PLUGIN_EXTENSION = "plugin_extension"
    PROVISIONAL_OVERRIDE = "provisional_override"
    MISSING_AMBIGUOUS = "missing_ambiguous"


class SemanticUsage(StrictModel):
    id: str
    origin: SemanticOrigin
    description: str
    entity_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    assertion_ids: tuple[str, ...] = ()
    source_revision_ids: tuple[str, ...] = ()
    executed: bool = False
    execution_count: int = 0


class SemanticAudit(StrictModel):
    strict: bool
    usages: tuple[SemanticUsage, ...] = ()
    blocking_usage_ids: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.blocking_usage_ids


def audit_semantics(
    usages: tuple[SemanticUsage, ...], *, strict: bool
) -> SemanticAudit:
    blocked_origins = {
        SemanticOrigin.PROVISIONAL_OVERRIDE,
        SemanticOrigin.MISSING_AMBIGUOUS,
    }
    blocking = (
        tuple(item.id for item in usages if item.origin in blocked_origins)
        if strict
        else ()
    )
    return SemanticAudit(strict=strict, usages=usages, blocking_usage_ids=blocking)
