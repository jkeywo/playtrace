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

from playtrace.models import StrictModel
from playtrace.simulation import content_digest


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

    @model_validator(mode="after")
    def bind_content_and_lifecycle(self) -> "GeneratedExtensionProposal":
        if self.digest != content_digest(self.content_payload()):
            raise ValueError("extension proposal digest does not match its immutable content")
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
            mode="json",
            exclude={"digest", "state", "history", "created_at"},
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
        if key not in {"state", "history", "created_at"}
    }
    return GeneratedExtensionProposal(**payload, digest=content_digest(digest_payload))


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
