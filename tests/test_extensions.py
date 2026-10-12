import pytest

from playtrace.extensions import (
    ExtensionKind,
    ExtensionProposalState,
    propose_extension,
    transition_extension,
)
from playtrace.plugins.registry import SimulationPluginRegistry
from playtrace.storage import PlayTraceStore


def test_generated_extension_is_persisted_but_never_loaded(tmp_path) -> None:
    registry = SimulationPluginRegistry()
    before = registry.plugin_ids
    proposal = propose_extension(
        id="extension:test",
        project_id="game:test",
        kind=ExtensionKind.SIMULATION_PLUGIN,
        title="Suggested plugin",
        rationale="A future reviewed implementation may use this sketch.",
        source_text="raise RuntimeError('must never execute')",
        proposed_tests=("reject unreviewed dynamic loading",),
        basis_ids=("finding:unsupported",),
        generator="test-generator@1",
    )

    with PlayTraceStore(tmp_path / "extensions.db") as store:
        store.save_extension_proposal(proposal)
        loaded = store.load_extension_proposal(proposal.id)

    assert loaded == proposal
    assert loaded.inert is True
    assert registry.plugin_ids == before
    assert not hasattr(loaded, "load")
    assert not hasattr(loaded, "apply")


def test_generator_cannot_self_approve_an_extension() -> None:
    with pytest.raises(ValueError, match="only create a proposed"):
        propose_extension(
            id="extension:self-approved",
            project_id="game:test",
            kind=ExtensionKind.RULE_IR,
            title="Unsafe self approval",
            rationale="Must still pass ordinary review.",
            source_text="new operation",
            proposed_tests=("test it",),
            basis_ids=("finding:1",),
            generator="generator@1",
            state=ExtensionProposalState.APPROVED,
        )


def test_extension_review_lifecycle_is_explicit_append_only_and_content_bound(tmp_path) -> None:
    proposal = propose_extension(
        id="extension:lifecycle",
        project_id="game:test",
        kind=ExtensionKind.CNL_GRAMMAR,
        title="Grammar sketch",
        rationale="A reviewer must decide whether to implement it conventionally.",
        source_text="rule := action",
        proposed_tests=("parse action",),
        basis_ids=("finding:grammar-gap",),
        generator="test-generator@1",
    )
    pending = transition_extension(
        proposal,
        ExtensionProposalState.PENDING,
        actor="designer",
        rationale="Ready for review.",
    )
    approved = transition_extension(
        pending,
        ExtensionProposalState.APPROVED,
        actor="maintainer",
        rationale="Approved for an ordinary reviewed implementation.",
    )

    assert proposal.digest == pending.digest == approved.digest
    assert [item.to_state for item in approved.history] == [
        ExtensionProposalState.PROPOSED,
        ExtensionProposalState.PENDING,
        ExtensionProposalState.APPROVED,
    ]
    with PlayTraceStore(tmp_path / "lifecycle.db") as store:
        store.save_extension_proposal(proposal)
        store.save_extension_proposal(pending)
        store.save_extension_proposal(approved)
        assert store.load_extension_proposal(proposal.id) == approved
        with pytest.raises(ValueError, match="append-only"):
            store.save_extension_proposal(pending)


def test_extension_storage_revalidates_untrusted_model_copies(tmp_path) -> None:
    proposal = propose_extension(
        id="extension:tamper",
        project_id="game:test",
        kind=ExtensionKind.RULE_IR,
        title="IR sketch",
        rationale="Keep generated material inert.",
        source_text="operation unsafe",
        proposed_tests=("reject unsafe",),
        basis_ids=("finding:1",),
        generator="test-generator@1",
    )
    tampered = proposal.model_copy(update={"source_text": "different content"})
    with PlayTraceStore(tmp_path / "tamper.db") as store:
        with pytest.raises(ValueError, match="digest"):
            store.save_extension_proposal(tampered)
