"""Small command-line entrypoint for the M0 skeleton."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pydantic import ValidationError

from playtrace.canonical import canonical_digest
from playtrace.adapters import SyntheticProjectAdapter
from playtrace.analysis import SnapshotAnalyzer
from playtrace.demo import build_demo_review
from playtrace.evidence import (
    ExperimentEvidence,
    ExperimentEvidenceBundle,
    ab_comparison_evidence,
    simulation_evidence,
    tension_batch_evidence,
)
from playtrace.knowledge import load_knowledge
from playtrace.knowledge import promote_applied_proposal
from playtrace.manifest import load_manifest
from playtrace.models import DesignSnapshot, EvidenceRelation, Hypothesis, Intent, SourceMedium
from playtrace.reviewing import (
    CriticRouter,
    HumanReviewDecision,
    Phase2ReviewEngine,
    ReviewDecisionState,
)
from playtrace.review_server import serve
from playtrace.plugins import registry as simulation_plugins
from playtrace.simulation import (
    DockerSimulationRunner,
    InProcessSimulationRunner,
    SimulationConfig,
    SimulationJsonInputError,
    SimulationRun,
    load_bounded_json_file,
)
from playtrace.simulation_report import write_simulation_report
from playtrace.tension import ABComparison, TensionBatch, build_batch, compare_batches
from playtrace.tension_report import write_tension_report
from playtrace.storage import PlayTraceStore
from playtrace.extensions import (
    ExtensionProposalState,
    GeneratedExtensionProposal,
    transition_extension,
)
from playtrace.patches import SemanticPatchExecutor, WriteSagaJournal
from playtrace.proposals import (
    ProposalOrchestrator,
    ProposalRecord,
    ProposalState,
    RegressionOutcome,
    transition_proposal,
)
from playtrace.regressions import (
    BehavioralRegressionCase,
    BehavioralRegressionRunner,
    BehavioralRegressionSuite,
    RegressionCaseKind,
)
from playtrace.tabletop import TabletopProjectAdapter
from playtrace.workspace import (
    FixtureWorkspaceGateway,
    GoogleWorkspaceGateway,
    WorkspaceGateway,
)
from playtrace.container_contract import verify_container_contract
from playtrace.migrations import DatabaseVersionError
from playtrace.workspace_sync import WorkspaceCache, WorkspaceSynchronizer
from playtrace.critics import CriticExecutorSpec, ExecutorCriticPipeline
from playtrace.llm import CliModelExecutor, HttpModelExecutor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="playtrace")
    subparsers = parser.add_subparsers(dest="command", required=True)

    telemetry_check = subparsers.add_parser("validate-telemetry", help="validate local Unreal JSONL evidence without importing it")
    telemetry_check.add_argument("capture", type=Path)

    demo = subparsers.add_parser("demo", help="analyse and persist the synthetic M0 snapshot")
    demo.add_argument("--medium", choices=[item.value for item in SourceMedium], default="synthetic")
    demo.add_argument("--db", type=Path, default=Path(".playtrace/playtrace.db"))
    demo.add_argument("--output", type=Path)

    review = subparsers.add_parser("review", help="serve the claim-linked review shell")
    review.add_argument("--host", default="127.0.0.1")
    review.add_argument("--port", type=int, default=8765)
    review.add_argument("--file", type=Path, help="serve a generated review payload")

    serve_local = subparsers.add_parser(
        "serve", help="serve the unified local review projector and bounded job API"
    )
    serve_local.add_argument("--host", default="127.0.0.1")
    serve_local.add_argument("--port", type=int, default=8765)
    serve_local.add_argument("--db", type=Path, required=True)
    serve_local.add_argument("--project-id")

    migrate = subparsers.add_parser("migrate", help="adopt or migrate a Play Trace database")
    migrate.add_argument("--db", type=Path, required=True)

    doctor = subparsers.add_parser("doctor", help="run local database and container diagnostics")
    doctor.add_argument("--db", type=Path, required=True)
    doctor.add_argument("--root", type=Path, default=Path.cwd())

    sync = subparsers.add_parser("sync", help="refresh authoritative Workspace metadata and cache")
    sync.add_argument("project", type=Path)
    sync_source = sync.add_mutually_exclusive_group(required=True)
    sync_source.add_argument("--fixture", type=Path)
    sync_source.add_argument("--access-token-env", metavar="ENV_VAR")
    sync.add_argument("--db", type=Path, required=True)
    sync.add_argument("--app-property", action="append", default=[], metavar="KEY=VALUE")
    sync.add_argument("--output", type=Path)

    recover = subparsers.add_parser("recover", help="retry reverse compensation for a write saga")
    recover.add_argument("--saga-id", required=True)
    recover.add_argument("--db", type=Path, required=True)
    recover.add_argument("--access-token-env", metavar="ENV_VAR", required=True)
    recover.set_defaults(fixture=None)
    recover.add_argument("--output", type=Path)
    recover.add_argument("--sheet-write-mode", choices=("atomic", "single-editor"), default="atomic",
                         help="single-editor permits observed Sheets writes while other editors are paused")

    container = subparsers.add_parser(
        "container-doctor", help="verify the pinned isolated simulation container contract"
    )
    container.add_argument("--root", type=Path, default=Path.cwd())
    container.add_argument("--smoke", action="store_true")
    container.add_argument("--image")
    container.add_argument("--image-digest")

    critics = subparsers.add_parser(
        "run-critics", help="run typed critics through local, CLI, then API fallback"
    )
    critics.add_argument("--snapshot", type=Path, required=True)
    critics.add_argument("--intent", type=Path, required=True)
    critics.add_argument("--db", type=Path, required=True)
    critics.add_argument("--output", type=Path)
    critics.add_argument("--timeout", type=float, default=60)
    critics.add_argument("--local-command", nargs="+")
    critics.add_argument("--local-model", default="local")
    critics.add_argument("--cli-command", nargs="+")
    critics.add_argument("--cli-model", default="subscription-cli")
    critics.add_argument("--api-endpoint")
    critics.add_argument("--api-model")
    critics.add_argument("--api-key-env")
    critics.add_argument(
        "--allow-metered-api",
        action="store_true",
        help="explicitly permit calls to the configured metered API fallback",
    )
    critics.add_argument(
        "--api-call-budget",
        type=int,
        default=0,
        help="maximum metered API attempts for this invocation",
    )

    snapshot = subparsers.add_parser(
        "snapshot", help="build a Workspace or local Unreal snapshot from a game repository"
    )
    snapshot.add_argument("project", type=Path)
    source = snapshot.add_mutually_exclusive_group(required=True)
    source.add_argument("--local", action="store_true", help="read a v2 unreal-local manifest without Google access")
    source.add_argument("--fixture", type=Path, help="replay a captured Workspace payload")
    source.add_argument(
        "--access-token-env",
        metavar="ENV_VAR",
        help="read a Google OAuth bearer token from this environment variable",
    )
    snapshot.add_argument("--db", type=Path, default=Path(".playtrace/playtrace.db"))
    snapshot.add_argument("--output", type=Path)

    assess = subparsers.add_parser(
        "assess", help="build a claim-linked Phase 2 design review"
    )
    assess.add_argument("project", type=Path)
    assess_source = assess.add_mutually_exclusive_group(required=True)
    assess_source.add_argument("--fixture", type=Path)
    assess_source.add_argument("--access-token-env", metavar="ENV_VAR")
    assess.add_argument("--intent", type=Path, required=True)
    assess.add_argument("--shared-root", type=Path, default=Path.cwd())
    assess.add_argument("--db", type=Path)
    assess.add_argument("--output", type=Path)
    assess.add_argument(
        "--experiment-evidence",
        type=Path,
        action="append",
        default=[],
        help="include a recorded ExperimentEvidence or ExperimentEvidenceBundle JSON file",
    )

    simulate = subparsers.add_parser(
        "simulate", help="run a deterministic tabletop simulation experiment"
    )
    simulate.add_argument("project", type=Path)
    simulate_source = simulate.add_mutually_exclusive_group(required=True)
    simulate_source.add_argument("--fixture", type=Path)
    simulate_source.add_argument("--access-token-env", metavar="ENV_VAR")
    simulate.add_argument("--config", type=Path, required=True)
    simulate.add_argument("--runner", choices=("in_process", "docker"), default="in_process")
    simulate.add_argument("--image")
    simulate.add_argument("--image-digest")
    simulate.add_argument("--db", type=Path)
    simulate.add_argument("--output", type=Path)

    report = subparsers.add_parser(
        "simulation-report",
        help="build an interactive HTML report from simulation results",
    )
    report.add_argument("--run", type=Path, action="append", required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--card-images", type=Path, help="snapshot-bound manifest of rendered card faces to embed")

    batch = subparsers.add_parser(
        "simulate-batch", help="measure tension for all Crisis Wardens character combinations"
    )
    batch.add_argument("project", type=Path)
    batch.add_argument("--fixture", type=Path, required=True)
    batch.add_argument("--config", type=Path, required=True)
    batch.add_argument("--seeds", default="1-20")
    batch.add_argument("--id", required=True)
    batch.add_argument("--output", type=Path, required=True)

    compare = subparsers.add_parser(
        "compare-batches", help="compare paired tension batches by character combination and seed"
    )
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)

    tension_report = subparsers.add_parser(
        "tension-report", help="build an HTML report for a paired tension A/B test"
    )
    tension_report.add_argument("--baseline", type=Path, required=True)
    tension_report.add_argument("--candidate", type=Path, required=True)
    tension_report.add_argument("--output", type=Path, required=True)

    record = subparsers.add_parser(
        "record-experiment",
        help="record simulation, tension or Unreal telemetry as hypothesis-linked evidence",
    )
    record.add_argument("project", type=Path)
    record.add_argument("--snapshot", type=Path, required=True)
    record.add_argument("--hypothesis", type=Path, required=True)
    result = record.add_mutually_exclusive_group(required=True)
    result.add_argument("--simulation", type=Path)
    result.add_argument("--batch", type=Path)
    result.add_argument("--comparison", type=Path)
    result.add_argument("--telemetry", type=Path, help="import a versioned Unreal server JSONL capture")
    record.add_argument("--baseline", type=Path)
    record.add_argument("--candidate", type=Path)
    record.add_argument(
        "--relation",
        choices=[item.value for item in EvidenceRelation],
        required=True,
    )
    record.add_argument("--rationale", required=True)
    record.add_argument("--assumption", action="append", default=[])
    record.add_argument("--db", type=Path)
    record.add_argument("--output", type=Path)

    decide = subparsers.add_parser(
        "decide-review", help="record a human decision without applying canonical changes"
    )
    decide.add_argument("--review-id", required=True)
    decide.add_argument("--db", type=Path, required=True)
    decide.add_argument(
        "--state",
        choices=[item.value for item in ReviewDecisionState if item is not ReviewDecisionState.PENDING],
        required=True,
    )
    decide.add_argument("--rationale", required=True)
    decide.add_argument("--decided-by", required=True)
    decide.add_argument("--output", type=Path)

    propose = subparsers.add_parser("propose", help="persist a typed design proposal record")
    propose.add_argument("--record", type=Path, required=True)
    propose.add_argument("--db", type=Path, required=True)
    propose.add_argument("--output", type=Path)

    submit = subparsers.add_parser("submit-proposal", help="submit a draft for approval")
    _proposal_transition_arguments(submit)

    proposal_decision = subparsers.add_parser(
        "decide-proposal", help="approve, reject, or return a submitted proposal for revision"
    )
    _proposal_transition_arguments(proposal_decision)
    proposal_decision.add_argument(
        "--state",
        choices=(ProposalState.APPROVED.value, ProposalState.REJECTED.value, ProposalState.REVISE.value),
        required=True,
    )

    dry_run = subparsers.add_parser(
        "dry-run-proposal", help="recheck an approved proposal without canonical writes"
    )
    dry_run.add_argument("project", type=Path)
    dry_source = dry_run.add_mutually_exclusive_group(required=True)
    dry_source.add_argument("--fixture", type=Path)
    dry_source.add_argument("--access-token-env", metavar="ENV_VAR")
    _proposal_action_arguments(dry_run)

    apply = subparsers.add_parser(
        "apply-proposal", help="apply an approved proposal to a live writable workspace"
    )
    apply.add_argument("project", type=Path)
    apply.add_argument("--access-token-env", metavar="ENV_VAR", required=True)
    apply.set_defaults(fixture=None)
    apply.add_argument("--sheet-write-mode", choices=("atomic", "single-editor"), default="atomic",
                       help="single-editor permits observed Sheets writes while other editors are paused")
    _proposal_action_arguments(apply)

    promote = subparsers.add_parser(
        "promote-precedent", help="explicitly promote an applied proposal into its game repository"
    )
    promote.add_argument("project", type=Path)
    promote.add_argument("--proposal-id", required=True)
    promote.add_argument("--db", type=Path, required=True)
    promote.add_argument("--decision", required=True)
    promote.add_argument("--rationale", required=True)
    promote.add_argument("--consequence", action="append", default=[])

    regress = subparsers.add_parser(
        "run-regressions", help="run an accepted deterministic behavioral regression suite"
    )
    regress.add_argument("--suite", type=Path, required=True)
    regress.add_argument("--snapshot", type=Path, required=True)
    regress.add_argument("--db", type=Path, required=True)
    regress.add_argument("--output", type=Path)

    accept_case = subparsers.add_parser(
        "accept-regression-case",
        help="accept a proposed case after verifying its applied-proposal origin",
    )
    accept_case.add_argument("--case", type=Path, required=True)
    accept_case.add_argument("--proposal-id")
    accept_case.add_argument("--db", type=Path, required=True)
    accept_case.add_argument("--actor", required=True)
    accept_case.add_argument("--rationale", required=True)
    accept_case.add_argument("--output", type=Path)

    extension = subparsers.add_parser(
        "propose-extension", help="persist an inert generated plugin, grammar, or RuleIR proposal"
    )
    extension.add_argument("--proposal", type=Path, required=True)
    extension.add_argument("--db", type=Path, required=True)
    extension.add_argument("--output", type=Path)

    submit_extension = subparsers.add_parser(
        "submit-extension", help="submit an inert extension proposal for human review"
    )
    _extension_transition_arguments(submit_extension)
    decide_extension = subparsers.add_parser(
        "decide-extension", help="approve, reject, or request revision of an extension"
    )
    _extension_transition_arguments(decide_extension)
    decide_extension.add_argument(
        "--state",
        choices=(
            ExtensionProposalState.APPROVED.value,
            ExtensionProposalState.REJECTED.value,
            ExtensionProposalState.REVISE.value,
        ),
        required=True,
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.command == "validate-telemetry":
        from collections import Counter
        from playtrace.unreal import load_unreal_capture
        try:
            capture = load_unreal_capture(args.capture)
        except (OSError, ValueError) as error:
            raise SystemExit(f"Telemetry rejected: {error}") from error
        print(json.dumps({"run_id": capture.events[0].run_id, "artifact_digest": capture.artifact_digest,
            "run_kind": capture.events[0].data["run_kind"], "outcome": capture.events[-1].data["outcome"],
            "event_count": len(capture.events), "event_counts": dict(Counter(e.event_type for e in capture.events))}, indent=2))
        return

    if args.command == "migrate":
        try:
            with PlayTraceStore(args.db) as store:
                payload = {"database": str(args.db), "schema_version": store.schema_version}
        except DatabaseVersionError as error:
            raise SystemExit(str(error)) from error
        print(json.dumps(payload, indent=2))
        return

    if args.command == "container-doctor":
        result = verify_container_contract(
            args.root,
            smoke=args.smoke,
            image=args.image,
            image_digest=args.image_digest,
        )
        print(result.model_dump_json(indent=2))
        if not result.ok:
            raise SystemExit(1)
        return

    if args.command == "doctor":
        checks: dict[str, object] = {}
        ok = True
        if not args.db.is_file():
            ok = False
            checks["database"] = {
                "ok": False,
                "error": f"database does not exist or is not a regular file: {args.db}",
            }
        else:
            try:
                with PlayTraceStore(args.db) as store:
                    integrity = store.connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()[0]
                    foreign_key_rows = store.connection.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                    incomplete_sagas = WriteSagaJournal(store.connection).incomplete()
                    database_ok = (
                        integrity == "ok"
                        and not foreign_key_rows
                        and not incomplete_sagas
                    )
                    checks["database"] = {
                        "ok": database_ok,
                        "schema_version": store.schema_version,
                        "integrity": integrity,
                        "foreign_key_violations": len(foreign_key_rows),
                        "foreign_key_details": [list(row) for row in foreign_key_rows[:100]],
                        "incomplete_sagas": len(incomplete_sagas),
                        "incomplete_saga_ids": [item.id for item in incomplete_sagas],
                    }
                    ok = ok and database_ok
            except Exception as error:
                ok = False
                checks["database"] = {"ok": False, "error": str(error)}
        try:
            diagnostic = verify_container_contract(args.root)
            checks["container"] = diagnostic.model_dump(mode="json")
            ok = ok and diagnostic.ok
        except Exception as error:
            ok = False
            checks["container"] = {"ok": False, "error": str(error)}
        print(json.dumps({"ok": ok, "checks": checks}, indent=2, default=str))
        if not ok:
            raise SystemExit(1)
        return

    if args.command == "sync":
        gateway = _workspace_gateway(args)
        manifest = load_manifest(args.project / "playtrace.toml")
        expected: dict[str, str] = {}
        for item in args.app_property:
            if "=" not in item:
                raise SystemExit("--app-property must use KEY=VALUE")
            key, value = item.split("=", 1)
            if not key:
                raise SystemExit("--app-property key cannot be empty")
            expected[key] = value
        with PlayTraceStore(args.db) as store:
            result = WorkspaceSynchronizer(
                gateway, WorkspaceCache(store.connection)
            ).sync(manifest, expected_app_properties=expected)
        _emit_model(result, args.output)
        return

    if args.command == "recover":
        gateway = _workspace_gateway(args)
        with PlayTraceStore(args.db) as store:
            saga = SemanticPatchExecutor(
                gateway, saga_journal=WriteSagaJournal(store.connection)
            ).recover(args.saga_id)
        _emit_model(saga, args.output)
        if saga.status.value == "recovery_failed":
            raise SystemExit(2)
        return

    if args.command == "run-critics":
        snapshot = _load_model(args.snapshot, DesignSnapshot, "snapshot")
        intent = _load_model(args.intent, Intent, "intent")
        if intent.project_id != snapshot.project_id:
            raise SystemExit("critic intent project does not match its snapshot")
        executors = []
        if args.local_command:
            executors.append(
                (
                    CriticExecutorSpec(
                        tier="local",
                        name=args.local_command[0],
                        model=args.local_model,
                        timeout_seconds=args.timeout,
                    ),
                    CliModelExecutor(
                        tuple(args.local_command), args.local_model, timeout=args.timeout
                    ),
                )
            )
        if args.cli_command:
            executors.append(
                (
                    CriticExecutorSpec(
                        tier="cli",
                        name=args.cli_command[0],
                        model=args.cli_model,
                        timeout_seconds=args.timeout,
                    ),
                    CliModelExecutor(
                        tuple(args.cli_command), args.cli_model, timeout=args.timeout
                    ),
                )
            )
        if args.api_endpoint or args.api_model:
            if not args.api_endpoint or not args.api_model:
                raise SystemExit("--api-endpoint and --api-model must be supplied together")
            if not args.allow_metered_api:
                raise SystemExit("API fallback requires --allow-metered-api")
            if args.api_call_budget < 1:
                raise SystemExit("API fallback requires a positive --api-call-budget")
            if not args.api_key_env:
                raise SystemExit("API fallback requires --api-key-env")
            api_key = os.environ.get(args.api_key_env, "")
            if not api_key:
                raise SystemExit(
                    f"API credential environment variable {args.api_key_env!r} is empty"
                )
            executors.append(
                (
                    CriticExecutorSpec(
                        tier="api",
                        name=args.api_endpoint,
                        model=args.api_model,
                        timeout_seconds=args.timeout,
                        metered=True,
                    ),
                    HttpModelExecutor(
                        args.api_endpoint,
                        args.api_model,
                        api_key,
                        timeout=args.timeout,
                    ),
                )
            )
        if not executors:
            raise SystemExit("run-critics requires at least one configured executor")
        runs = ExecutorCriticPipeline(
            executors,
            allow_metered_api=args.allow_metered_api,
            metered_api_budget=args.api_call_budget,
        ).run(
            snapshot, intent, CriticRouter().route(snapshot, intent)
        )
        with PlayTraceStore(args.db) as store:
            store.save_snapshot(snapshot)
            for run in runs:
                store.save_critic_run(run)
        rendered = json.dumps(
            {"runs": [run.model_dump(mode="json") for run in runs]},
            indent=2,
            default=str,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return

    if args.command == "serve":
        serve(
            args.host,
            args.port,
            db_path=args.db,
            project_id=args.project_id,
        )
        return

    if args.command == "propose":
        record = _load_model(args.record, ProposalRecord)
        with PlayTraceStore(args.db) as store:
            store.save_proposal(record)
        _emit_model(record, args.output)
        return

    if args.command in {"submit-proposal", "decide-proposal"}:
        with PlayTraceStore(args.db) as store:
            record = store.load_proposal(args.proposal_id)
            if record is None:
                raise SystemExit(f"proposal {args.proposal_id!r} was not found")
            target = (
                ProposalState.PENDING
                if args.command == "submit-proposal"
                else ProposalState(args.state)
            )
            record = transition_proposal(
                record, target, actor=args.actor, rationale=args.rationale
            )
            store.save_proposal(record)
        _emit_model(record, args.output)
        return

    if args.command in {"dry-run-proposal", "apply-proposal"}:
        gateway = _workspace_gateway(args)
        adapter = TabletopProjectAdapter.from_path(args.project / "playtrace.toml", gateway)
        snapshot = adapter.snapshot()
        with PlayTraceStore(args.db) as store:
            record = store.load_proposal(args.proposal_id)
            if record is None:
                raise SystemExit(f"proposal {args.proposal_id!r} was not found")

            def run_referenced(proposal, rebuilt):
                cases = []
                for case_id in proposal.regression_case_ids:
                    case = store.load_regression_case(case_id)
                    if case is None:
                        return (
                            RegressionOutcome(
                                suite_id=f"{proposal.id}:referenced-regressions",
                                passed=False,
                                case_ids=(case_id,),
                                failed_case_ids=(case_id,),
                                digest=canonical_digest(
                                    {"missing": case_id}, domain="regression-outcome"
                                ),
                            ),
                        )
                    try:
                        case = store.validate_regression_case(case)
                    except ValueError as error:
                        return (
                            RegressionOutcome(
                                suite_id=f"{proposal.id}:referenced-regressions",
                                passed=False,
                                case_ids=(case_id,),
                                failed_case_ids=(case_id,),
                                digest=canonical_digest(
                                    {"invalid_case_origin": case_id, "error": str(error)},
                                    domain="regression-outcome",
                                ),
                            ),
                        )
                    expected = proposal.basis.regression_records.get(case_id)
                    if case.model_dump(mode="json") != expected:
                        return (
                            RegressionOutcome(
                                suite_id=f"{proposal.id}:referenced-regressions",
                                passed=False,
                                case_ids=(case_id,),
                                failed_case_ids=(case_id,),
                                digest=canonical_digest(
                                    {"basis_mismatch": case_id, "actual": case},
                                    domain="regression-outcome",
                                ),
                            ),
                        )
                    cases.append(case)
                if not cases:
                    return ()
                suite = BehavioralRegressionSuite(
                    id=f"{proposal.id}:referenced-regressions",
                    project_id=proposal.project_id,
                    cases=tuple(cases),
                )
                result = BehavioralRegressionRunner(simulation_plugins).run(suite, rebuilt)
                return (
                    RegressionOutcome(
                        suite_id=suite.id,
                        passed=result.passed,
                        case_ids=tuple(item.case_id for item in result.case_results),
                        failed_case_ids=tuple(
                            item.case_id for item in result.case_results if not item.passed
                        ),
                        digest=result.digest,
                    ),
                )

            orchestrator = ProposalOrchestrator(
                SemanticPatchExecutor(
                    gateway,
                    saga_journal=(
                        WriteSagaJournal(store.connection)
                        if args.command == "apply-proposal"
                        else None
                    ),
                ),
                adapter.snapshot,
                run_regressions=run_referenced,
            )
            record = orchestrator.execute(
                record,
                snapshot,
                dry_run=args.command == "dry-run-proposal",
                actor=args.actor,
            )
            store.save_proposal(record)
        _emit_model(record, args.output)
        return

    if args.command == "promote-precedent":
        with PlayTraceStore(args.db) as store:
            record = store.load_proposal(args.proposal_id)
            if record is None:
                raise SystemExit(f"proposal {args.proposal_id!r} was not found")
        manifest = load_manifest(args.project / "playtrace.toml")
        if manifest.project_id != record.proposal.project_id:
            raise SystemExit("proposal project does not match the target game repository")
        precedent, path = promote_applied_proposal(
            record,
            game_root=args.project,
            decision=args.decision,
            rationale=args.rationale,
            consequences=args.consequence,
        )
        print(json.dumps({"precedent": precedent.model_dump(mode="json"), "path": str(path)}, indent=2, default=str))
        return

    if args.command == "run-regressions":
        suite = _load_model(args.suite, BehavioralRegressionSuite)
        snapshot = _load_model(args.snapshot, DesignSnapshot, "snapshot")
        with PlayTraceStore(args.db) as store:
            verified_cases = tuple(
                store.validate_regression_case(case) for case in suite.cases
            )
            verified_suite = suite.model_copy(update={"cases": verified_cases})
            result = BehavioralRegressionRunner(simulation_plugins).run(
                verified_suite, snapshot
            )
            for case in verified_cases:
                store.save_regression_case(case)
            store.save_regression_run(result)
        _emit_model(result, args.output)
        return

    if args.command == "accept-regression-case":
        case = _load_model(args.case, BehavioralRegressionCase)
        with PlayTraceStore(args.db) as store:
            if case.kind is RegressionCaseKind.APPROVED_BEHAVIOR:
                if not args.proposal_id:
                    raise SystemExit(
                        "approved behavior acceptance requires --proposal-id"
                    )
            elif args.proposal_id:
                raise SystemExit("adversarial cases do not accept --proposal-id")
            accepted = store.accept_regression_case(
                case,
                actor=args.actor,
                rationale=args.rationale,
                proposal_id=args.proposal_id,
            )
        _emit_model(accepted, args.output)
        return

    if args.command == "propose-extension":
        proposal = _load_model(args.proposal, GeneratedExtensionProposal)
        if proposal.state.value != "proposed":
            raise SystemExit("propose-extension only accepts a newly proposed inert record")
        with PlayTraceStore(args.db) as store:
            store.save_extension_proposal(proposal)
        _emit_model(proposal, args.output)
        return

    if args.command in {"submit-extension", "decide-extension"}:
        with PlayTraceStore(args.db) as store:
            proposal = store.load_extension_proposal(args.proposal_id)
            if proposal is None:
                raise SystemExit(f"extension proposal {args.proposal_id!r} was not found")
            target = (
                ExtensionProposalState.PENDING
                if args.command == "submit-extension"
                else ExtensionProposalState(args.state)
            )
            proposal = transition_extension(
                proposal, target, actor=args.actor, rationale=args.rationale
            )
            store.save_extension_proposal(proposal)
        _emit_model(proposal, args.output)
        return
    if args.command == "review":
        serve(args.host, args.port, args.file)
        return

    if args.command == "decide-review":
        with PlayTraceStore(args.db) as store:
            review_bundle = store.load_review(args.review_id)
            if review_bundle is None:
                raise SystemExit(f"review {args.review_id!r} was not found")
            decision = HumanReviewDecision.decided(
                ReviewDecisionState(args.state),
                rationale=args.rationale,
                decided_by=args.decided_by,
            )
            review_bundle = review_bundle.model_copy(update={"decision": decision})
            store.save_review(review_bundle)
        payload = {
            "review": review_bundle.model_dump(mode="json"),
            "canonical_mutation": False,
        }
        rendered = json.dumps(payload, indent=2, default=str)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return

    if args.command == "record-experiment":
        snapshot = _load_model(args.snapshot, DesignSnapshot, "snapshot")
        hypothesis = _load_model(args.hypothesis, Hypothesis, "hypothesis")
        common = {
            "project_id": snapshot.project_id,
            "snapshot": snapshot,
            "hypothesis": hypothesis,
            "relation": EvidenceRelation(args.relation),
            "rationale": args.rationale,
            "assumptions": tuple(args.assumption),
        }
        if args.telemetry:
            from playtrace.unreal import load_unreal_capture, unreal_evidence
            try:
                bundle = unreal_evidence(snapshot, load_unreal_capture(args.telemetry), hypothesis,
                                         EvidenceRelation(args.relation), args.rationale)
            except (OSError, ValueError) as error:
                raise SystemExit(f"Telemetry rejected: {error}") from error
            if args.assumption:
                record = bundle.evidence.model_copy(update={"assumptions": (*bundle.evidence.assumptions, *args.assumption)})
                bundle = ExperimentEvidenceBundle(hypothesis=hypothesis, evidence=record, claim=record.to_claim())
        elif args.simulation:
            run = _load_model(args.simulation, SimulationRun, "simulation")
            bundle = simulation_evidence(
                **common, run=run, artifact_path=args.simulation
            )
        elif args.batch:
            batch_result = _load_model(args.batch, TensionBatch)
            bundle = tension_batch_evidence(
                **common, batch=batch_result, artifact_path=args.batch
            )
        else:
            if not args.baseline or not args.candidate:
                raise SystemExit("--comparison requires --baseline and --candidate")
            comparison = _load_model(args.comparison, ABComparison)
            baseline = _load_model(args.baseline, TensionBatch)
            candidate = _load_model(args.candidate, TensionBatch)
            bundle = ab_comparison_evidence(
                **common,
                baseline=baseline,
                candidate=candidate,
                comparison=comparison,
                artifact_path=args.comparison,
            )
        db_path = args.db or args.project / ".playtrace" / "playtrace.db"
        output_path = args.output or args.project / ".playtrace" / "experiment-evidence.json"
        with PlayTraceStore(db_path) as store:
            store.save_snapshot(snapshot)
            store.save_hypothesis(snapshot.id, hypothesis)
            store.save_experiment_evidence(bundle.evidence)
            store.save_claim(snapshot.id, bundle.claim)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = bundle.model_dump_json(indent=2)
        output_path.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return

    if args.command == "simulation-report":
        runs = []
        for path in args.run:
            payload = json.loads(path.read_text(encoding="utf-8"))
            runs.append(SimulationRun.model_validate(payload.get("simulation", payload)))
        write_simulation_report(runs, args.output, card_images=args.card_images)
        print(args.output.resolve())
        return

    if args.command == "compare-batches":
        baseline = TensionBatch.model_validate_json(args.baseline.read_text(encoding="utf-8"))
        candidate = TensionBatch.model_validate_json(args.candidate.read_text(encoding="utf-8"))
        comparison = compare_batches(baseline, candidate)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(comparison.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(comparison.model_dump_json(indent=2))
        return

    if args.command == "tension-report":
        baseline = TensionBatch.model_validate_json(args.baseline.read_text(encoding="utf-8"))
        candidate = TensionBatch.model_validate_json(args.candidate.read_text(encoding="utf-8"))
        write_tension_report(
            baseline, candidate, compare_batches(baseline, candidate), args.output
        )
        print(args.output.resolve())
        return

    if args.command == "simulate-batch":
        gateway = FixtureWorkspaceGateway.from_file(args.fixture)
        snapshot = TabletopProjectAdapter.from_path(
            args.project / "playtrace.toml", gateway
        ).snapshot()
        template = _load_simulation_config(args.config)
        if len(template.character_ids) < 2:
            raise SystemExit("batch config must list both current character IDs")
        combinations = (
            (template.character_ids[0],),
            (template.character_ids[1],),
            tuple(template.character_ids[:2]),
        )
        seeds = _parse_seeds(args.seeds)
        try:
            plugin = simulation_plugins.for_project(snapshot.project_id)
        except KeyError as error:
            raise SystemExit(str(error)) from error
        runner = InProcessSimulationRunner()
        runs = []
        for characters in combinations:
            for seed in seeds:
                config = template.model_copy(
                    update={
                        "id": f"{template.id}:{'+'.join(characters)}:seed-{seed}",
                        "character_ids": characters,
                        "seed": seed,
                    }
                )
                runs.append(runner.run(plugin, snapshot, config))
        batch = build_batch(args.id, runs)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(batch.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(batch.model_dump_json(indent=2))
        return

    if args.command == "assess":
        gateway = _workspace_gateway(args)
        snapshot = TabletopProjectAdapter.from_path(
            args.project / "playtrace.toml", gateway
        ).snapshot()
        intent = Intent.model_validate_json(args.intent.read_text(encoding="utf-8"))
        if intent.project_id != snapshot.project_id:
            raise SystemExit(
                f"intent project {intent.project_id!r} does not match snapshot project {snapshot.project_id!r}"
            )
        recorded_evidence: list[ExperimentEvidence] = []
        recorded_hypotheses: list[Hypothesis] = []
        for path in args.experiment_evidence:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if "evidence" in payload and "hypothesis" in payload:
                bundle = ExperimentEvidenceBundle.model_validate(payload)
                recorded_evidence.append(bundle.evidence)
                recorded_hypotheses.append(bundle.hypothesis)
            else:
                raise SystemExit(
                    f"{path} is standalone experiment evidence; supply an "
                    "ExperimentEvidenceBundle so its linked hypothesis is included"
                )
        review_bundle = Phase2ReviewEngine(args.shared_root, args.project).review(
            snapshot,
            intent,
            experiment_evidence=recorded_evidence,
            experiment_hypotheses=recorded_hypotheses,
        )
        analysis = SnapshotAnalyzer().analyze(snapshot)
        payload = {
            "snapshot": snapshot.model_dump(mode="json"),
            "analysis": analysis.model_dump(mode="json"),
            "review": review_bundle.model_dump(mode="json"),
        }
        db_path = args.db or args.project / ".playtrace" / "playtrace.db"
        output_path = args.output or args.project / ".playtrace" / "review.json"
        with PlayTraceStore(db_path) as store:
            store.save_snapshot(snapshot)
            store.save_knowledge(
                snapshot.project_id, load_knowledge(args.shared_root, args.project)
            )
            for claim in review_bundle.claims:
                store.save_claim(snapshot.id, claim)
            for hypothesis in review_bundle.hypotheses:
                store.save_hypothesis(snapshot.id, hypothesis)
            for evidence_item in review_bundle.experiment_evidence:
                store.save_experiment_evidence(evidence_item)
            store.save_review(review_bundle)
            payload["proposals"] = [
                item.model_dump(mode="json")
                for item in store.list_proposals(snapshot.project_id)
            ]
            payload["regression_runs"] = [
                item.model_dump(mode="json")
                for item in store.list_regression_runs(snapshot.project_id)
            ]
            payload["extension_proposals"] = [
                item.model_dump(mode="json")
                for item in store.list_extension_proposals(snapshot.project_id)
            ]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2, default=str))
        return

    if args.command == "simulate":
        gateway = _workspace_gateway(args)
        snapshot = TabletopProjectAdapter.from_path(
            args.project / "playtrace.toml", gateway
        ).snapshot()
        config = _load_simulation_config(args.config)
        try:
            plugin = simulation_plugins.for_project(snapshot.project_id)
        except KeyError as error:
            raise SystemExit(str(error)) from error
        if args.runner == "docker":
            if not args.image or not args.image_digest:
                raise SystemExit("--image and --image-digest are required for the docker runner")
            runner = DockerSimulationRunner(args.image, image_digest=args.image_digest)
        else:
            runner = InProcessSimulationRunner()
        run = runner.run(plugin, snapshot, config)
        claim = run.evidence_claim()
        payload = {
            "snapshot_id": snapshot.id,
            "simulation": run.model_dump(mode="json"),
            "claim": claim.model_dump(mode="json"),
        }
        db_path = args.db or args.project / ".playtrace" / "playtrace.db"
        output_path = args.output or args.project / ".playtrace" / "simulation.json"
        with PlayTraceStore(db_path) as store:
            store.save_snapshot(snapshot)
            store.save_simulation(run)
            store.save_claim(snapshot.id, claim)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        output_path.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return

    if args.command == "snapshot":
        manifest_path = args.project / "playtrace.toml"
        if args.local:
            from playtrace.unreal import UnrealLocalAdapter
            try:
                snapshot = UnrealLocalAdapter(args.project).snapshot()
            except (OSError, ValueError) as error:
                raise SystemExit(f"Local snapshot rejected: {error}") from error
        else:
            gateway = _workspace_gateway(args)
            snapshot = TabletopProjectAdapter.from_path(manifest_path, gateway).snapshot()
        report = SnapshotAnalyzer().analyze(snapshot)
        with PlayTraceStore(args.db) as store:
            store.save_snapshot(snapshot)
        rendered = json.dumps(
            {
                "snapshot": snapshot.model_dump(mode="json"),
                "analysis": report.model_dump(mode="json"),
            },
            indent=2,
            default=str,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return

    medium = SourceMedium(args.medium)
    snapshot = SyntheticProjectAdapter(medium).snapshot()
    report = SnapshotAnalyzer().analyze(snapshot)
    with PlayTraceStore(args.db) as store:
        store.save_snapshot(snapshot)
        counts = store.export_counts()
    payload = build_demo_review(medium)
    payload["storage"] = counts
    rendered = json.dumps(payload, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(json.dumps(report.source_neutral_result(), indent=2))


def _workspace_gateway(args: argparse.Namespace) -> WorkspaceGateway:
    if args.fixture is not None:
        return FixtureWorkspaceGateway.from_file(args.fixture)
    access_token = os.environ.get(args.access_token_env, "")
    if not access_token:
        raise SystemExit(
            f"environment variable {args.access_token_env!r} is empty or unset"
        )
    return GoogleWorkspaceGateway(
        access_token, sheet_write_mode=getattr(args, "sheet_write_mode", "atomic")
    )


def _proposal_transition_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--output", type=Path)


def _proposal_action_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--output", type=Path)


def _extension_transition_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--output", type=Path)


def _emit_model(model: object, output: Path | None) -> None:
    rendered = model.model_dump_json(indent=2)  # type: ignore[attr-defined]
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def _parse_seeds(value: str) -> tuple[int, ...]:
    seeds: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if end < start:
                raise SystemExit("seed ranges must be ascending")
            seeds.extend(range(start, end + 1))
        elif part:
            seeds.append(int(part))
    if not seeds:
        raise SystemExit("at least one seed is required")
    return tuple(dict.fromkeys(seeds))


def _load_simulation_config(path: Path) -> SimulationConfig:
    try:
        payload = load_bounded_json_file(path)
        return SimulationConfig.model_validate(payload)
    except SimulationJsonInputError as error:
        raise SystemExit(f"invalid simulation JSON input ({error.category}): {error.message}") from error
    except ValidationError as error:
        raise SystemExit(f"invalid simulation configuration: {error}") from error


def _load_model(path: Path, model: type, wrapper: str | None = None):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if wrapper and isinstance(payload, dict):
        payload = payload.get(wrapper, payload)
    return model.model_validate(payload)
