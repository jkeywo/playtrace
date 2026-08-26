from __future__ import annotations

import http.client
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from playtrace.adapters import SyntheticProjectAdapter
from playtrace.cli import main
from playtrace.container_contract import verify_container_contract
from playtrace.critics import CriticExecutorSpec, ExecutorCriticPipeline
from playtrace.llm import LLMExecutor, LLMResult
from playtrace.local_service import BoundedJobManager, LocalReviewProjector
from playtrace.manifest import ManifestSource, ProjectManifest, WorkspaceSourceKind
from playtrace.migrations import DatabaseVersionError, LATEST_SCHEMA_VERSION
from playtrace.models import Intent, IntentKind, SourceMedium
from playtrace.review_server import ReviewHandler, serve
from playtrace.reviewing import CriticRoute, CriticRouter
from playtrace.simulation import DockerSimulationRunner, SimulationConfig
from playtrace.storage import PlayTraceStore
from playtrace.workspace import FixtureWorkspaceGateway
from playtrace.workspace_sync import WorkspaceCache, WorkspaceCursor, WorkspaceSynchronizer


def _manifest(*file_ids: str) -> ProjectManifest:
    return ProjectManifest(
        project_id="game",
        name="Game",
        repository="repo",
        drive_folder_id="folder",
        sources=tuple(
            ManifestSource(
                id=f"source:{file_id}",
                file_id=file_id,
                kind=WorkspaceSourceKind.GOOGLE_DOC,
                role="rules",
                title=file_id,
                schema_id="rules-v1",
            )
            for file_id in file_ids
        ),
    )


class FailingSecondFileGateway(FixtureWorkspaceGateway):
    def get_file(self, file_id):
        if file_id == "second":
            raise RuntimeError("partial provider failure")
        return super().get_file(file_id)


def test_workspace_cursor_contains_token_and_complete_manifest_and_never_partially_advances(
    tmp_path,
) -> None:
    payload = {
        "files": {
            "first": {"id": "first", "version": "11"},
            "second": {"id": "second", "version": "22"},
        },
        "documents": {"first": {"documentId": "first"}, "second": {"documentId": "second"}},
        "spreadsheets": {},
        "changes": {
            "old-token": {"nextPageToken": "page-2", "changes": []},
            "page-2": {"newStartPageToken": "new-token", "changes": []},
        },
    }
    with PlayTraceStore(tmp_path / "sync.db") as store:
        cache = WorkspaceCache(store.connection)
        old = WorkspaceCursor(page_token="old-token", manifest_versions={"first": "10"})
        cache.set_cursor("game", old)
        result = WorkspaceSynchronizer(
            FixtureWorkspaceGateway(payload), cache
        ).sync(_manifest("first", "second"))
        assert result.cursor_before == old
        assert result.cursor_after.page_token == "new-token"
        assert result.cursor_after.manifest_versions == {"first": "11", "second": "22"}

        cache.set_cursor("game", old)
        with pytest.raises(RuntimeError, match="partial provider failure"):
            WorkspaceSynchronizer(FailingSecondFileGateway(payload), cache).sync(
                _manifest("first", "second")
            )
        assert cache.cursor("game") == old


def test_v3_migration_has_both_identity_tables_and_all_direct_readers_refuse_newer(
    tmp_path,
) -> None:
    path = tmp_path / "state.db"
    with PlayTraceStore(path) as store:
        names = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"artifact_integrity", "identity_metadata"} <= names
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()
    with pytest.raises(DatabaseVersionError, match="newer"):
        LocalReviewProjector(path).project()
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(DatabaseVersionError, match="newer"):
            WorkspaceCache(connection)
    finally:
        connection.close()


def test_doctor_does_not_create_a_missing_database(tmp_path, monkeypatch, capsys) -> None:
    missing = tmp_path / "missing" / "state.db"
    monkeypatch.setattr(
        sys,
        "argv",
        ["playtrace", "doctor", "--db", str(missing), "--root", str(tmp_path)],
    )
    with pytest.raises(SystemExit):
        main()
    payload = json.loads(capsys.readouterr().out)
    assert not payload["checks"]["database"]["ok"]
    assert "does not exist" in payload["checks"]["database"]["error"]
    assert not missing.exists()


class UsageExecutor(LLMExecutor):
    api_key = "test-only-key"

    def execute(self, task):
        finding = {
            "id": "finding",
            "critic_id": task.context["critic_id"],
            "title": "Finding",
            "statement": "Grounded.",
            "stance": "risk",
            "basis_ids": [task.context["snapshot"]["id"]],
        }
        return LLMResult(
            task_id=task.id,
            executor="http",
            model="metered",
            content=json.dumps({"findings": [finding]}),
            raw={"usage": {"prompt_tokens": 10, "completion_tokens": 3}},
        )


def test_critic_occurrences_persist_separately_and_metered_budget_is_opt_in(tmp_path) -> None:
    snapshot = SyntheticProjectAdapter(SourceMedium.SYNTHETIC).snapshot()
    intent = Intent(
        id="intent",
        project_id=snapshot.project_id,
        kind=IntentKind.QUESTION,
        original_prompt="Review.",
    )
    spec = CriticExecutorSpec(
        tier="api", name="api", model="metered", metered=True
    )
    with pytest.raises(ValueError, match="explicit opt-in"):
        ExecutorCriticPipeline(((spec, UsageExecutor()),))
    pipeline = ExecutorCriticPipeline(
        ((spec, UsageExecutor()),), allow_metered_api=True, metered_api_budget=1
    )
    route = CriticRoute(critic_id="critic:coherence", reason="mandatory", mandatory=True)
    first = pipeline.run(snapshot, intent, (route,))[0]
    second = pipeline.run(snapshot, intent, (route,))[0]
    assert first.id != second.id
    assert first.task_digest == second.task_digest
    assert first.attempts[0].metered
    assert first.attempts[0].usage == {"prompt_tokens": 10, "completion_tokens": 3}
    with PlayTraceStore(tmp_path / "critics.db") as store:
        store.save_critic_run(first)
        store.save_critic_run(second)
        assert len(store.list_critic_runs(snapshot.project_id)) == 2


def test_mandatory_critic_routing_is_independent_of_executor_availability() -> None:
    snapshot = SyntheticProjectAdapter(SourceMedium.SYNTHETIC).snapshot()
    intent = Intent(
        id="intent",
        project_id=snapshot.project_id,
        kind=IntentKind.QUESTION,
        original_prompt="Review.",
    )
    before = CriticRouter().route(snapshot, intent)
    failed = ExecutorCriticPipeline(()).run(snapshot, intent, before)
    after = CriticRouter().route(snapshot, intent)
    assert before == after
    assert all(route.mandatory for route in before[:3])
    assert all(not run.completed for run in failed)


def _start_review_server(path, project_id="alpha"):
    projector = LocalReviewProjector(path)
    readiness = projector.preflight()
    jobs = BoundedJobManager(max_workers=1)
    handler = partial(
        ReviewHandler,
        projector=projector,
        project_id=project_id,
        jobs=jobs,
        readiness=readiness,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, jobs


def test_local_service_health_security_malformed_length_and_project_isolation(tmp_path) -> None:
    path = tmp_path / "service.db"
    with PlayTraceStore(path) as store:
        with store.connection:
            for project in ("alpha", "beta"):
                store.connection.execute(
                    "INSERT INTO critic_runs VALUES (?, ?, ?)",
                    (f"run:{project}", project, json.dumps({"id": f"run:{project}"})),
                )
    server, jobs = _start_review_server(path)
    try:
        connection = http.client.HTTPConnection(*server.server_address)
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        health = json.loads(response.read())
        assert response.status == 200 and health["ready"] is True
        assert response.getheader("Cache-Control") == "no-store"
        assert response.getheader("X-Frame-Options") == "DENY"

        connection.request("GET", "/api/review")
        response = connection.getresponse()
        projection = json.loads(response.read())
        assert [item["id"] for item in projection["critic_runs"]] == ["run:alpha"]

        connection.putrequest("POST", "/api/jobs")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "not-a-number")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"] == "invalid Content-Length"

        body = json.dumps({"kind": "project-review", "project_id": "beta"}).encode()
        connection.request(
            "POST",
            "/api/jobs",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        jobs.close()


def test_service_preflights_before_bind_and_explicitly_rejects_ipv6(tmp_path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        serve("127.0.0.1", 0, db_path=missing)
    assert not missing.exists()
    with pytest.raises(ValueError, match="loopback"):
        serve("::1", 0, review_file=tmp_path / "unused.json")


def test_container_requires_exact_lowercase_digest_and_regular_bounded_result(
    tmp_path, monkeypatch
) -> None:
    with pytest.raises(ValueError, match="lowercase"):
        DockerSimulationRunner("image", image_digest="sha256:" + "A" * 64)
    runner = DockerSimulationRunner("image", image_digest="sha256:" + "a" * 64)
    observed = {}

    def fake_run(command, **kwargs):
        observed.update(kwargs)
        output_mount = next(
            command[index + 1]
            for index, item in enumerate(command)
            if item == "--mount" and "dst=/output" in command[index + 1]
        )
        source = next(
            part.removeprefix("src=")
            for part in output_mount.split(",")
            if part.startswith("src=")
        )
        from pathlib import Path

        (Path(source) / "result.json").mkdir()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("playtrace.simulation.subprocess.run", fake_run)
    snapshot = SyntheticProjectAdapter(SourceMedium.SYNTHETIC).snapshot()
    from playtrace.plugins import registry

    with pytest.raises(ValueError, match="regular non-symlink"):
        runner.run(
            registry.for_project("crisis-wardens"),
            snapshot,
            SimulationConfig(
                id="container-test",
                scenario_id="missing",
                character_ids=(),
                max_turns=1,
            ),
        )
    assert observed["stdout"] is subprocess.DEVNULL
    assert observed["stderr"] is subprocess.DEVNULL


def test_container_lock_hashes_exact_index_bytes_and_accepts_crlf_checkout(tmp_path) -> None:
    index_lock = subprocess.run(
        ["git", "show", ":uv.lock"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    recorded = json.loads(
        (Path("container") / "simulation-image.lock.json").read_text("utf-8")
    )
    assert hashlib.sha256(index_lock).hexdigest() == recorded["dependency_lock_sha256"]

    checkout = tmp_path / "checkout"
    (checkout / "container").mkdir(parents=True)
    shutil.copy2("container/Dockerfile", checkout / "container" / "Dockerfile")
    shutil.copy2(
        "container/simulation-image.lock.json",
        checkout / "container" / "simulation-image.lock.json",
    )
    (checkout / "uv.lock").write_bytes(index_lock.replace(b"\n", b"\r\n"))
    result = verify_container_contract(checkout)
    assert result.checks["dependency_lock_matches"]


def test_container_contract_requires_non_editable_importable_runtime(tmp_path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "container").mkdir(parents=True)
    shutil.copy2("uv.lock", checkout / "uv.lock")
    shutil.copy2(
        "container/simulation-image.lock.json",
        checkout / "container" / "simulation-image.lock.json",
    )
    dockerfile = Path("container/Dockerfile").read_text("utf-8")

    (checkout / "container" / "Dockerfile").write_text(
        dockerfile.replace(" --no-editable", ""), encoding="utf-8"
    )
    editable = verify_container_contract(checkout)
    assert not editable.checks["non_editable_package_install"]
    assert not editable.ok

    (checkout / "container" / "Dockerfile").write_text(
        dockerfile.replace(
            'RUN /app/.venv/bin/python -c "import playtrace, playtrace.sim_worker"\n',
            "",
        ),
        encoding="utf-8",
    )
    missing_import = verify_container_contract(checkout)
    assert not missing_import.checks["runtime_package_importable"]
    assert not missing_import.ok

    (checkout / "container" / "Dockerfile").write_text(
        dockerfile.replace(
            "COPY --from=builder --chown=playtrace:playtrace /app/.venv /app/.venv\n",
            "",
        ),
        encoding="utf-8",
    )
    missing_package = verify_container_contract(checkout)
    assert not missing_package.checks["runtime_venv_copied"]
    assert not missing_package.ok

    (checkout / "container" / "Dockerfile").write_text(
        dockerfile.replace(
            "WORKDIR /app\nCOPY --from=builder",
            "WORKDIR /app\nCOPY . .\nCOPY --from=builder",
        ),
        encoding="utf-8",
    )
    source_backed = verify_container_contract(checkout)
    assert not source_backed.checks["runtime_source_excluded"]
    assert not source_backed.ok
