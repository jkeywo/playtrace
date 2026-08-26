from playtrace.semantics import (
    SemanticOrigin,
    SemanticUsage,
    audit_semantics,
)


def test_strict_audit_accepts_compiled_and_extensions_but_rejects_guesses() -> None:
    usages = (
        SemanticUsage(id="compiled", origin=SemanticOrigin.COMPILED, description="exact rule"),
        SemanticUsage(id="extension", origin=SemanticOrigin.PLUGIN_EXTENSION, description="registered game rule"),
        SemanticUsage(id="guess", origin=SemanticOrigin.PROVISIONAL_OVERRIDE, description="temporary ruling"),
        SemanticUsage(id="gap", origin=SemanticOrigin.MISSING_AMBIGUOUS, description="unknown target"),
    )

    strict = audit_semantics(usages, strict=True)
    exploratory = audit_semantics(usages, strict=False)

    assert strict.blocking_usage_ids == ("guess", "gap")
    assert not strict.passed
    assert exploratory.passed


def test_usage_preserves_execution_and_source_provenance() -> None:
    usage = SemanticUsage(
        id="compiled",
        origin=SemanticOrigin.COMPILED,
        description="exact rule",
        rule_ids=("rule:1",),
        assertion_ids=("rule:1:compiled",),
        source_revision_ids=("rules@7",),
        executed=True,
        execution_count=2,
    )

    assert usage.executed
    assert usage.execution_count == 2
    assert usage.source_revision_ids == ("rules@7",)
