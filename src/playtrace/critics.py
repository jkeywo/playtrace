"""Typed, bounded critic execution with explicit fallback provenance."""

from __future__ import annotations

import json
import queue
import threading
import uuid
from datetime import UTC, datetime
from time import monotonic
from typing import Iterable

from pydantic import Field, model_validator

from playtrace.canonical import canonical_digest
from playtrace.llm import LLMExecutor, LLMTask
from playtrace.models import DesignSnapshot, Intent, StrictModel
from playtrace.reviewing import CriticFinding, CriticRoute


class CriticResponse(StrictModel):
    findings: tuple[CriticFinding, ...] = ()

    @model_validator(mode="after")
    def findings_match_task(self) -> "CriticResponse":
        ids = [item.id for item in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("critic response finding IDs must be unique")
        return self


class CriticExecutorSpec(StrictModel):
    tier: str
    name: str
    model: str
    timeout_seconds: float = 60
    metered: bool = False

    @model_validator(mode="after")
    def bounded_timeout(self) -> "CriticExecutorSpec":
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("critic timeout must be between 0 and 300 seconds")
        if self.metered and self.tier != "api":
            raise ValueError("only API critic executors may be marked as metered")
        return self


class CriticAttempt(StrictModel):
    tier: str
    executor: str
    model: str
    succeeded: bool
    duration_ms: int
    error: str | None = None
    metered: bool = False
    usage: dict[str, int | float | str] = Field(default_factory=dict)


class CriticRun(StrictModel):
    id: str
    occurrence_id: str = Field(min_length=1)
    project_id: str
    snapshot_id: str
    critic_id: str
    task_digest: str
    attempts: tuple[CriticAttempt, ...]
    findings: tuple[CriticFinding, ...] = ()
    completed: bool
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def adopt_legacy_occurrence_id(cls, value: object) -> object:
        if isinstance(value, dict) and "occurrence_id" not in value:
            adopted = dict(value)
            run_id = str(adopted.get("id", ""))
            adopted["occurrence_id"] = run_id.rsplit(":run:", 1)[-1] or "legacy"
            return adopted
        return value


class ExecutorCriticPipeline:
    """Try local, subscription CLI, then metered API; never owns coverage."""

    VALID_TIERS = ("local", "cli", "api")

    def __init__(
        self,
        executors: Iterable[tuple[CriticExecutorSpec, LLMExecutor]],
        *,
        max_output_bytes: int = 250_000,
        allow_metered_api: bool = False,
        metered_api_budget: int = 0,
    ) -> None:
        self.executors = tuple(executors)
        ranks = [self.VALID_TIERS.index(spec.tier) for spec, _ in self.executors]
        if ranks != sorted(ranks):
            raise ValueError("critic executors must be ordered local -> cli -> api")
        if not 1 <= max_output_bytes <= 1_000_000:
            raise ValueError("critic output bound must be between 1 and 1000000 bytes")
        metered = [spec for spec, _ in self.executors if spec.metered]
        if metered and (not allow_metered_api or metered_api_budget < 1):
            raise ValueError(
                "metered API critics require explicit opt-in and a positive call budget"
            )
        missing_credentials = [
            spec.name
            for spec, executor in self.executors
            if spec.metered and not getattr(executor, "api_key", None)
        ]
        if missing_credentials:
            raise ValueError(
                "metered API critic credentials must be preflighted: "
                + ", ".join(missing_credentials)
            )
        if metered_api_budget < 0 or metered_api_budget > 10_000:
            raise ValueError("metered API call budget must be between 0 and 10000")
        self.max_output_bytes = max_output_bytes
        self.allow_metered_api = allow_metered_api
        self.metered_api_budget = metered_api_budget
        self._metered_attempts = 0

    def run(
        self,
        snapshot: DesignSnapshot,
        intent: Intent,
        routes: Iterable[CriticRoute],
    ) -> tuple[CriticRun, ...]:
        self._metered_attempts = 0
        return tuple(self._run_one(snapshot, intent, route) for route in routes)

    def _run_one(
        self, snapshot: DesignSnapshot, intent: Intent, route: CriticRoute
    ) -> CriticRun:
        allowed_basis_ids = {
            snapshot.id,
            intent.id,
            *(item.id for item in snapshot.source_revisions),
            *(item.source_id for item in snapshot.source_revisions),
            *(item.id for item in snapshot.entities),
            *(item.id for item in snapshot.assertions),
            *(item.upstream_id for item in snapshot.dependencies),
            *(item.downstream_id for item in snapshot.dependencies),
        }
        # This executor task currently supplies a snapshot and intent, not a
        # DesignReview's material claims.  Therefore claim links must remain
        # empty rather than letting a model invent plausible-looking IDs.
        allowed_claim_ids: set[str] = set()
        context = {
            "critic_id": route.critic_id,
            "route_reason": route.reason,
            "intent": intent.model_dump(mode="json"),
            "snapshot": snapshot.model_dump(mode="json"),
            "grounding": {
                "basis_ids": sorted(allowed_basis_ids),
                "claim_ids": sorted(allowed_claim_ids),
            },
        }
        task = LLMTask(
            id=f"{snapshot.id}:{route.critic_id}",
            purpose="typed_design_critic",
            instructions=(
                "Return JSON matching {findings: CriticFinding[]}. Ground every finding "
                "in supplied IDs or set unsupported_judgment=true. Do not decide coverage."
            ),
            context=context,
            response_schema=CriticResponse.model_json_schema(),
        )
        attempts: list[CriticAttempt] = []
        for spec, executor in self.executors:
            started = monotonic()
            try:
                if spec.metered:
                    if self._metered_attempts >= self.metered_api_budget:
                        raise RuntimeError("metered API critic call budget exhausted")
                    self._metered_attempts += 1
                result = self._execute_with_timeout(executor, task, spec.timeout_seconds)
                if result.task_id != task.id:
                    raise ValueError(
                        f"executor returned task {result.task_id!r}; expected {task.id!r}"
                    )
                encoded = result.content.encode("utf-8")
                if len(encoded) > self.max_output_bytes:
                    raise ValueError("critic output exceeded configured bound")
                response = CriticResponse.model_validate(json.loads(result.content))
                wrong = [
                    item.id for item in response.findings if item.critic_id != route.critic_id
                ]
                if wrong:
                    raise ValueError(f"findings assigned to the wrong critic: {wrong}")
                invented_basis = sorted(
                    {
                        basis_id
                        for item in response.findings
                        for basis_id in item.basis_ids
                        if basis_id not in allowed_basis_ids
                    }
                )
                invented_claims = sorted(
                    {
                        claim_id
                        for item in response.findings
                        for claim_id in item.claim_ids
                        if claim_id not in allowed_claim_ids
                    }
                )
                if invented_basis or invented_claims:
                    raise ValueError(
                        "critic output referenced IDs not supplied by the task: "
                        f"basis={invented_basis}, claims={invented_claims}"
                    )
                attempts.append(
                    CriticAttempt(
                        tier=spec.tier,
                        executor=result.executor,
                        model=result.model,
                        succeeded=True,
                        duration_ms=int((monotonic() - started) * 1000),
                        metered=spec.metered,
                        usage=_usage_provenance(result.raw),
                    )
                )
                return self._result(snapshot, route, task, attempts, response.findings)
            except Exception as error:
                attempts.append(
                    CriticAttempt(
                        tier=spec.tier,
                        executor=spec.name,
                        model=spec.model,
                        succeeded=False,
                        duration_ms=int((monotonic() - started) * 1000),
                        error=f"{type(error).__name__}: {error}"[:1000],
                        metered=spec.metered,
                    )
                )
        return self._result(snapshot, route, task, attempts, ())

    @staticmethod
    def _execute_with_timeout(executor: LLMExecutor, task: LLMTask, timeout: float):
        """Bound even custom/local executors without trusting their implementation."""

        results: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def target() -> None:
            try:
                results.put((True, executor.execute(task)))
            except BaseException as error:  # delivered and re-raised on the caller thread
                results.put((False, error))

        thread = threading.Thread(target=target, daemon=True, name=f"critic:{task.id}")
        thread.start()
        try:
            succeeded, value = results.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError(f"critic executor exceeded {timeout:g}s") from error
        if not succeeded:
            raise value  # type: ignore[misc]
        return value

    @staticmethod
    def _result(
        snapshot: DesignSnapshot,
        route: CriticRoute,
        task: LLMTask,
        attempts: list[CriticAttempt],
        findings: tuple[CriticFinding, ...],
    ) -> CriticRun:
        digest = canonical_digest(task, domain="critic-task")
        occurrence_id = uuid.uuid4().hex
        return CriticRun(
            id=f"{task.id}:run:{occurrence_id}",
            occurrence_id=occurrence_id,
            project_id=snapshot.project_id,
            snapshot_id=snapshot.id,
            critic_id=route.critic_id,
            task_digest=digest,
            attempts=tuple(attempts),
            findings=findings,
            completed=any(item.succeeded for item in attempts),
        )


def _usage_provenance(raw: object) -> dict[str, int | float | str]:
    """Retain only small scalar provider usage fields in durable provenance."""

    if not isinstance(raw, dict) or not isinstance(raw.get("usage"), dict):
        return {}
    usage: dict[str, int | float | str] = {}
    for key, value in sorted(raw["usage"].items(), key=lambda item: str(item[0]))[:50]:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        usage[str(key)[:100]] = value[:200] if isinstance(value, str) else value
    return usage
