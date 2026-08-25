import pytest

from playtrace.cli import build_parser


def test_snapshot_cli_selects_fixture_or_live_workspace() -> None:
    parser = build_parser()

    fixture = parser.parse_args(["snapshot", "game", "--fixture", "capture.json"])
    live = parser.parse_args(
        ["snapshot", "game", "--access-token-env", "PLAYTRACE_GOOGLE_TOKEN"]
    )

    assert fixture.fixture.name == "capture.json"
    assert live.access_token_env == "PLAYTRACE_GOOGLE_TOKEN"
    with pytest.raises(SystemExit):
        parser.parse_args(["snapshot", "game"])


def test_assess_cli_requires_intent_and_workspace_source() -> None:
    args = build_parser().parse_args(
        [
            "assess",
            "game",
            "--fixture",
            "capture.json",
            "--intent",
            "intent.json",
        ]
    )

    assert args.command == "assess"
    assert args.intent.name == "intent.json"


def test_simulate_cli_requires_config_and_workspace_source() -> None:
    args = build_parser().parse_args(
        [
            "simulate",
            "game",
            "--fixture",
            "capture.json",
            "--config",
            "experiment.json",
        ]
    )

    assert args.command == "simulate"
    assert args.runner == "in_process"
    assert args.config.name == "experiment.json"


def test_simulation_report_cli_accepts_multiple_runs() -> None:
    args = build_parser().parse_args(
        [
            "simulation-report",
            "--run",
            "solo.json",
            "--run",
            "duo.json",
            "--output",
            "report.html",
        ]
    )

    assert [path.name for path in args.run] == ["solo.json", "duo.json"]
    assert args.output.name == "report.html"


def test_tension_batch_cli_requires_reproducible_inputs() -> None:
    args = build_parser().parse_args(
        [
            "simulate-batch", "game", "--fixture", "capture.json",
            "--config", "tension.json", "--id", "test-batch",
            "--output", "batch.json",
        ]
    )

    assert args.seeds == "1-20"
    assert args.id == "test-batch"
