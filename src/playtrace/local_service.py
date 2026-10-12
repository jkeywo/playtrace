"""Typed local review projection and bounded in-process job service."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import Field

from playtrace.models import StrictModel
from playtrace.models import DesignSnapshot
from playtrace.analysis import SnapshotAnalyzer
from playtrace.patches import WriteSagaJournal
from playtrace.migrations import LATEST_SCHEMA_VERSION, validate_database_version


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobEvent(StrictModel):
    sequence: int
    message: str
    progress: float
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LocalJob(StrictModel):
    id: str
    kind: str
    status: JobStatus
    events: tuple[JobEvent, ...] = ()
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_requested: bool = False


class ProjectReviewJobRequest(StrictModel):
    kind: Literal["project-review"]
    id: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9:._-]+$")
    project_id: str | None = Field(default=None, min_length=1, max_length=200)


JobFunction = Callable[[Callable[[str, float], None], threading.Event], dict[str, Any]]


class BoundedJobManager:
    """Small cancellable executor with hard queue and worker bounds."""

    def __init__(
        self,
        *,
        max_workers: int = 2,
        max_jobs: int = 32,
        max_terminal_jobs: int = 64,
        max_events_per_job: int = 200,
    ) -> None:
        if (
            not 1 <= max_workers <= 8
            or not 1 <= max_jobs <= 1024
            or not 1 <= max_terminal_jobs <= 4096
            or not 1 <= max_events_per_job <= 10_000
        ):
            raise ValueError("invalid local job bounds")
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="playtrace")
        self.max_jobs = max_jobs
        self.max_terminal_jobs = max_terminal_jobs
        self.max_events_per_job = max_events_per_job
        # A Future may finish while its done callback is registered during
        # submission; re-entrancy keeps that cleanup path deadlock-free.
        self._lock = threading.RLock()
        self._jobs: dict[str, LocalJob] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._futures: dict[str, Future[None]] = {}
        self._event_sequence: dict[str, int] = {}

    def submit(self, job_id: str, kind: str, function: JobFunction) -> LocalJob:
        with self._lock:
            active = sum(
                item.status in {JobStatus.QUEUED, JobStatus.RUNNING}
                for item in self._jobs.values()
            )
            if active >= self.max_jobs:
                raise RuntimeError("local job queue is full")
            if job_id in self._jobs:
                raise ValueError(f"job {job_id!r} already exists")
            job = LocalJob(id=job_id, kind=kind, status=JobStatus.QUEUED)
            cancel = threading.Event()
            self._jobs[job_id] = job
            self._cancel[job_id] = cancel
            self._event_sequence[job_id] = 0
            future = self.pool.submit(self._run, job_id, function, cancel)
            self._futures[job_id] = future
            future.add_done_callback(lambda item, key=job_id: self._future_done(key, item))
            return job

    def get(self, job_id: str) -> LocalJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> LocalJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            cancel = self._cancel.get(job_id)
            if cancel is None:
                return job
            cancel.set()
            future = self._futures.get(job_id)
            if future and future.cancel():
                job = job.model_copy(update={"status": JobStatus.CANCELLED})
            else:
                job = job.model_copy(update={"cancel_requested": True})
            self._jobs[job_id] = job
            self._prune_terminal_locked()
            return job

    def close(self) -> None:
        with self._lock:
            for event in self._cancel.values():
                event.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _run(self, job_id: str, function: JobFunction, cancel: threading.Event) -> None:
        self._update(job_id, status=JobStatus.RUNNING)

        def progress(message: str, value: float) -> None:
            if not 0 <= value <= 1:
                raise ValueError("job progress must be between zero and one")
            with self._lock:
                job = self._jobs[job_id]
                sequence = self._event_sequence.get(job_id, 0) + 1
                self._event_sequence[job_id] = sequence
                event = JobEvent(
                    sequence=sequence, message=message, progress=value
                )
                self._jobs[job_id] = job.model_copy(
                    update={
                        "events": (job.events + (event,))[-self.max_events_per_job :]
                    }
                )

        try:
            result = function(progress, cancel)
            status = JobStatus.CANCELLED if cancel.is_set() else JobStatus.SUCCEEDED
            self._update(job_id, status=status, result=result)
        except Exception as error:
            self._update(
                job_id,
                status=JobStatus.CANCELLED if cancel.is_set() else JobStatus.FAILED,
                error=f"{type(error).__name__}: {error}"[:2000],
            )

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            if job_id not in self._jobs:
                return
            self._jobs[job_id] = self._jobs[job_id].model_copy(update=values)
            self._prune_terminal_locked()

    def _future_done(self, job_id: str, future: Future[None]) -> None:
        with self._lock:
            if self._futures.get(job_id) is future:
                self._futures.pop(job_id, None)
                self._cancel.pop(job_id, None)

    def _prune_terminal_locked(self) -> None:
        terminal = {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
        completed = [key for key, item in self._jobs.items() if item.status in terminal]
        for job_id in completed[: max(0, len(completed) - self.max_terminal_jobs)]:
            self._jobs.pop(job_id, None)
            self._cancel.pop(job_id, None)
            future = self._futures.pop(job_id, None)
            if future is not None and not future.done():
                future.cancel()
            self._event_sequence.pop(job_id, None)


class LocalReviewProjector:
    """Project the local review, proposal, evidence, saga, and job state together."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def preflight(self) -> dict[str, Any]:
        """Verify a ready, supported database through a read-only connection."""

        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"review database does not exist or is not a regular file: {self.db_path}"
            )
        connection = self._connect_read_only()
        try:
            version = validate_database_version(connection)
            if version != LATEST_SCHEMA_VERSION:
                raise RuntimeError(
                    f"review database schema {version} is not ready; "
                    f"run migrate to schema {LATEST_SCHEMA_VERSION}"
                )
            required = {
                "snapshots",
                "design_reviews",
                "proposal_records",
                "write_sagas",
                "critic_runs",
                "artifact_integrity",
                "identity_metadata",
            }
            actual = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            missing = sorted(required - actual)
            if missing:
                raise RuntimeError(
                    "review database is not migrated; missing tables: " + ", ".join(missing)
                )
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != "ok" or foreign_keys:
                raise RuntimeError(
                    "review database failed readiness checks: "
                    f"integrity={integrity}, foreign_key_violations={len(foreign_keys)}"
                )
            return {
                "ready": True,
                "schema_version": version,
                "latest_schema_version": LATEST_SCHEMA_VERSION,
                "database": str(self.db_path),
                "integrity": integrity,
                "foreign_key_violations": 0,
            }
        finally:
            connection.close()

    def _connect_read_only(self) -> sqlite3.Connection:
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def project(self, project_id: str | None = None) -> dict[str, Any]:
        self.preflight()
        # Projection has no user-directed mutation surface. The journal reader
        # may nevertheless perform M6B's required atomic adoption of a legacy
        # saga payload, so use a normal connection only after read-only preflight
        # has proved that the path exists and the schema is supported.
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        validate_database_version(connection)
        try:
            def payloads(table: str, where: str = "", params: tuple[Any, ...] = ()) -> list[Any]:
                rows = connection.execute(
                    f"SELECT payload FROM {table} {where} ORDER BY rowid", params
                ).fetchall()
                return [json.loads(row["payload"]) for row in rows]

            project_where = "WHERE project_id = ?" if project_id else ""
            project_params = (project_id,) if project_id else ()
            reviews = payloads("design_reviews")
            if project_id:
                reviews = [item for item in reviews if item.get("project_id") == project_id]
            proposal_rows = connection.execute(
                f"SELECT id, payload FROM proposal_records {project_where} ORDER BY rowid",
                project_params,
            ).fetchall()
            proposals = []
            proposal_ids: set[str] = set()
            for row in proposal_rows:
                proposal_ids.add(str(row["id"]))
                transitions = payloads(
                    "proposal_transitions", "WHERE proposal_id = ?", (row["id"],)
                )
                attempts = payloads(
                    "application_attempts", "WHERE proposal_id = ?", (row["id"],)
                )
                proposals.append(
                    {
                        "proposal": json.loads(row["payload"]),
                        "history": transitions,
                        "attempts": attempts,
                    }
                )
            regression_runs = payloads("regression_runs")
            if project_id:
                regression_runs = [
                    item for item in regression_runs if item.get("project_id") == project_id
                ]
            write_sagas = [
                item.model_dump(mode="json") for item in WriteSagaJournal(connection).all()
            ]
            if project_id:
                write_sagas = [
                    item for item in write_sagas if item.get("proposal_id") in proposal_ids
                ]
            projection = {
                "project_id": project_id,
                "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
                "reviews": reviews,
                "proposals": proposals,
                "experiment_evidence": payloads(
                    "experiment_evidence", project_where, project_params
                ),
                "regression_runs": regression_runs,
                "extension_proposals": payloads(
                    "extension_proposals", project_where, project_params
                ),
                "write_sagas": write_sagas,
                "critic_runs": payloads("critic_runs", project_where, project_params),
            }
            if reviews:
                selected_review = reviews[-1]
                snapshot_row = connection.execute(
                    "SELECT payload FROM snapshots WHERE id = ?",
                    (selected_review["snapshot_id"],),
                ).fetchone()
                if snapshot_row:
                    snapshot = DesignSnapshot.model_validate_json(snapshot_row["payload"])
                    projection.update(
                        {
                            "snapshot": snapshot.model_dump(mode="json"),
                            "analysis": SnapshotAnalyzer().analyze(snapshot).model_dump(
                                mode="json"
                            ),
                            "review": selected_review,
                        }
                    )
            return projection
        finally:
            connection.close()
