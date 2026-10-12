"""Persisted, review-only proposals for future engine extensions.

These records deliberately have no import, compile, registration, or execution
method. M5 may suggest code; only a later, ordinary reviewed code change can add
it to the trusted runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from playtrace.canonical import (
    CANONICAL_IDENTITY_SCHEMA,
    CANONICAL_IDENTITY_VERSION,
    canonical_digest,
)
from playtrace.models import StrictModel


_CANONICAL_DIGEST_SCHEMA = (
    f"{CANONICAL_IDENTITY_SCHEMA}/{CANONICAL_IDENTITY_VERSION}"
)
_LEGACY_DIGEST_SCHEMA = "legacy-json-sha256-v0"


class ExtensionKind(StrEnum):
    SIMULATION_PLUGIN = "simulation_plugin"
    CNL_GRAMMAR = "cnl_grammar"
    RULE_IR = "rule_ir"


class ExtensionProposalState(StrEnum):
    PROPOSED = "proposed"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISE = "revise"


class ExtensionTransition(StrictModel):
    sequence: int
    from_state: ExtensionProposalState | None
    to_state: ExtensionProposalState
    actor: str
    rationale: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def require_attribution(self) -> "ExtensionTransition":
        if not self.actor.strip() or not self.rationale.strip():
            raise ValueError("extension transitions require actor and rationale")
        return self


class GeneratedExtensionProposal(StrictModel):
    id: str
    project_id: str
    kind: ExtensionKind
    title: str
    rationale: str
    source_text: str
    proposed_tests: tuple[str, ...]
    basis_ids: tuple[str, ...]
    state: ExtensionProposalState = ExtensionProposalState.PROPOSED
    history: tuple[ExtensionTransition, ...]
    inert: Literal[True] = True
    generator: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    digest: str
    digest_schema: str = _LEGACY_DIGEST_SCHEMA
    canonical_seal: str | None = None

    @model_validator(mode="after")
    def bind_content_and_lifecycle(self) -> "GeneratedExtensionProposal":
        if self.digest_schema == _CANONICAL_DIGEST_SCHEMA:
            if self.digest != self.expected_content_digest():
                raise ValueError(
                    "extension proposal digest does not match its immutable content"
                )
            if self.canonical_seal != self.expected_canonical_seal():
                raise ValueError("extension canonical integrity seal does not match its content")
        elif self.digest_schema == _LEGACY_DIGEST_SCHEMA:
            if (
                self.canonical_seal is not None
                and self.canonical_seal != self.expected_canonical_seal()
            ):
                raise ValueError("extension canonical integrity seal does not match its content")
        else:
            raise ValueError(f"unsupported extension digest schema {self.digest_schema!r}")
        if not self.proposed_tests:
            raise ValueError("an extension proposal must include proposed tests")
        if not self.history:
            raise ValueError("extension proposal history cannot be empty")
        for index, transition in enumerate(self.history, start=1):
            if transition.sequence != index:
                raise ValueError("extension history sequence must be contiguous")
            previous = None if index == 1 else self.history[index - 2].to_state
            if transition.from_state is not previous:
                raise ValueError("extension history has a broken state chain")
            if not _transition_allowed(previous, transition.to_state):
                raise ValueError("extension history contains an illegal state transition")
        if self.history[-1].to_state is not self.state:
            raise ValueError("extension state must match its append-only history")
        return self

    def content_payload(self) -> dict[str, object]:
        """Return generator content, excluding review state and audit history."""

        return self.model_dump(
            mode="python",
            exclude={
                "digest",
                "canonical_seal",
                "state",
                "history",
                "created_at",
            },
        )

    def expected_content_digest(self) -> str:
        return canonical_digest(self.content_payload(), domain="extension-content")

    def expected_canonical_seal(self) -> str:
        return canonical_digest(
            {
                "historical_digest": self.digest,
                "content": self.content_payload(),
            },
            domain="extension-content-integrity",
        )


def extension_integrity_matches(proposal: GeneratedExtensionProposal) -> bool:
    if proposal.canonical_seal != proposal.expected_canonical_seal():
        return False
    if proposal.digest_schema == _CANONICAL_DIGEST_SCHEMA:
        return proposal.digest == proposal.expected_content_digest()
    return proposal.digest_schema == _LEGACY_DIGEST_SCHEMA


def adopt_legacy_extension(
    proposal: GeneratedExtensionProposal,
) -> GeneratedExtensionProposal:
    if proposal.digest_schema != _LEGACY_DIGEST_SCHEMA or proposal.canonical_seal is not None:
        return proposal
    return GeneratedExtensionProposal.model_validate(
        proposal.model_dump(mode="python")
        | {"canonical_seal": proposal.expected_canonical_seal()}
    )


_ALLOWED_TRANSITIONS = {
    ExtensionProposalState.PROPOSED: {ExtensionProposalState.PENDING},
    ExtensionProposalState.PENDING: {
        ExtensionProposalState.APPROVED,
        ExtensionProposalState.REJECTED,
        ExtensionProposalState.REVISE,
    },
    ExtensionProposalState.REVISE: {ExtensionProposalState.PENDING},
    ExtensionProposalState.APPROVED: set(),
    ExtensionProposalState.REJECTED: set(),
}


def propose_extension(**values: object) -> GeneratedExtensionProposal:
    payload = dict(values)
    requested_state = payload.pop("state", ExtensionProposalState.PROPOSED)
    if ExtensionProposalState(requested_state) is not ExtensionProposalState.PROPOSED:
        raise ValueError("a generator may only create a proposed inert extension")
    if "history" in payload:
        raise ValueError("a generator cannot supply extension review history")
    payload.pop("digest", None)
    payload["state"] = ExtensionProposalState.PROPOSED
    payload.setdefault("inert", True)
    payload["digest_schema"] = _CANONICAL_DIGEST_SCHEMA
    payload["canonical_seal"] = None
    actor = str(payload.get("generator", ""))
    payload["history"] = (
        ExtensionTransition(
            sequence=1,
            from_state=None,
            to_state=ExtensionProposalState.PROPOSED,
            actor=actor,
            rationale="Generated as an inert proposal; no runtime change occurred.",
        ),
    )
    digest_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"digest", "canonical_seal", "state", "history", "created_at"}
    }
    digest = canonical_digest(digest_payload, domain="extension-content")
    seal = canonical_digest(
        {"historical_digest": digest, "content": digest_payload},
        domain="extension-content-integrity",
    )
    payload["canonical_seal"] = seal
    return GeneratedExtensionProposal(**payload, digest=digest)


def transition_extension(
    proposal: GeneratedExtensionProposal,
    to_state: ExtensionProposalState,
    *,
    actor: str,
    rationale: str,
) -> GeneratedExtensionProposal:
    if to_state not in _ALLOWED_TRANSITIONS[proposal.state]:
        raise ValueError(
            f"extension cannot transition from {proposal.state.value} to {to_state.value}"
        )
    transition = ExtensionTransition(
        sequence=len(proposal.history) + 1,
        from_state=proposal.state,
        to_state=to_state,
        actor=actor,
        rationale=rationale,
    )
    payload = proposal.model_dump(mode="json")
    payload["state"] = to_state
    payload["history"] = (*proposal.history, transition)
    return GeneratedExtensionProposal.model_validate(payload)


def _transition_allowed(
    previous: ExtensionProposalState | None,
    current: ExtensionProposalState,
) -> bool:
    if previous is None:
        return current is ExtensionProposalState.PROPOSED
    return current in _ALLOWED_TRANSITIONS[previous]
