"""Synthetic fixtures only: local source and telemetry ingestion boundaries."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from playtrace.evidence import ExperimentApplicability, ExperimentEvidenceKind
from playtrace.models import AuthoredStatus, EvidenceRelation, Hypothesis
from playtrace.storage import PlayTraceStore
from playtrace.unreal import (
    LocalSource, UnrealLocalAdapter, load_unreal_capture, repository_fingerprint, unreal_evidence,
)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "synthetic-game"
    root.mkdir()
    def git(*args):
        subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root), *args],
                       check=True, capture_output=True)
    git("init")
    (root / ".gitignore").write_text(".playtrace/\n", encoding="utf-8")
    (root / "rules.md").write_text("# Rules\n**LOCKED:** Four scouts.\n**PROVISIONAL:** Seven supplies.\n**OPEN:** Weather?\nCONTENT TBD: Maps.\nA plain sentence.\n", encoding="utf-8")
    (root / "Game.uproject").write_text('{"FileVersion": 3, "EngineAssociation": "5.8"}', encoding="utf-8")
    (root / "playtrace.toml").write_text('''manifest_version = 2
adapter = "unreal-local"
project_id = "synthetic:scouts"
name = "Synthetic Scouts"
repository = "local"
engine_version = "5.8.2"
[[sources]]
id = "rules"
path = "rules.md"
kind = "markdown"
role = "design"
[[sources]]
id = "project"
path = "Game.uproject"
kind = "unreal_project"
''', encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Synthetic fixture")
    return root


def events_for(root):
    revision, digest, dirty = repository_fingerprint(root)
    meta = {"project_id": "synthetic:scouts", "scenario_id": "synthetic-encounter", "run_kind": "combat_sandbox",
            "seed": 42, "engine_version": "5.8.2-test", "capture_version": "0.2.0", "working_tree_dirty": dirty,
            "GameRevision": revision, "SourceDigest": digest, "GDDDigest": hashlib.sha256((root / "rules.md").read_bytes()).hexdigest(), "omissions": ["scenario_ai"]}
    data = [
        ("run.started", meta),
        ("combat.spawned", {"entity_id": "scout.0", "team": "investigator", "health": 100, "shield": 20}),
        ("combat.spawned", {"entity_id": "foe.0", "team": "enemy", "health": 10, "shield": 5}),
        ("combat.damage", {"actor_id": "scout.0", "target_id": "foe.0", "damage": 20,
                           "health_before": 10, "health_after": 0, "shield_before": 5, "shield_after": 0}),
        ("combat.killed", {"entity_id": "foe.0"}),
        ("run.ended", {"outcome": "victory"}),
    ]
    return [{"schema_version": 1, "run_id": "91a81310-909e-4485-9ad7-381bef594bcb", "sequence": n,
             "event_type": kind, "elapsed_seconds": n * .1, "authority": "server", "visibility": "developer", "data": payload}
            for n, (kind, payload) in enumerate(data)]


def write_capture(root, events):
    path = root / ".playtrace" / "events.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


def hypothesis():
    return Hypothesis(id="hypothesis:synthetic", statement="The shared capture records resolved damage.", scope=("synthetic-encounter",),
                      test_method="Validate capture", supporting_outcomes=("coherent damage",), contradicting_outcomes=("missing events",))


def shield_gain_events(root):
    events = events_for(root)
    gain = dict(events[2], event_type="combat.shield_gained", data={
        "entity_id": "foe.0", "shield_before": 5, "shield_after": 8, "amount": 3})
    events.insert(3, gain)
    events[4]["data"]["shield_before"] = 8
    for index, event in enumerate(events):
        event["sequence"] = index
        event["elapsed_seconds"] = index * .1
    return events


def test_shield_gain_updates_state_before_damage(project):
    capture = load_unreal_capture(write_capture(project, shield_gain_events(project)))
    assert capture.events[-1].data["outcome"] == "victory"


@pytest.mark.parametrize("changes", [
    {"entity_id": "unknown"}, {"shield_before": 4}, {"shield_after": 9},
    {"amount": 0}, {"amount": float("nan")}, {"shield_after": 2, "amount": -3},
])
def test_shield_gain_rejects_unknown_or_incoherent_transitions(project, changes):
    events = shield_gain_events(project)
    events[3]["data"].update(changes)
    with pytest.raises(ValueError):
        load_unreal_capture(write_capture(project, events))


def test_local_snapshot_preserves_status_and_stable_fragments(project):
    before = UnrealLocalAdapter(project).snapshot()
    assert [a.authored_status for a in before.assertions] == [AuthoredStatus.CONFIRMED, AuthoredStatus.WORKING,
        AuthoredStatus.OPEN_QUESTION, AuthoredStatus.PLACEHOLDER, AuthoredStatus.UNSPECIFIED]
    original = (project / "rules.md").read_text()
    (project / "rules.md").write_text("\n" + original)
    after = UnrealLocalAdapter(project).snapshot()
    assert [a.id for a in before.assertions] == [a.id for a in after.assertions]
    assert before.id != after.id
    assert after.assertions[0].provenance.location.endswith("#L3")
    assert after.entities[0].attributes["working_tree_dirty"] is True
    assert "unsupported" in " ".join(after.omissions)


@pytest.mark.parametrize("path", ["../outside.md", "/etc/passwd", "C:/private.txt", "..\\private.txt"])
def test_manifest_rejects_path_escape(path):
    with pytest.raises(ValueError):
        LocalSource(id="source", path=path, kind="text")


def test_missing_source_is_explicit(project):
    (project / "rules.md").unlink()
    with pytest.raises(ValueError, match="Required source missing"):
        UnrealLocalAdapter(project).snapshot()


def test_capture_roundtrip_and_database(project, tmp_path):
    snapshot = UnrealLocalAdapter(project).snapshot()
    capture = load_unreal_capture(write_capture(project, events_for(project)))
    bundle = unreal_evidence(snapshot, capture, hypothesis(), EvidenceRelation.CONTEXT, "Infrastructure observation only.")
    assert bundle.evidence.kind is ExperimentEvidenceKind.TELEMETRY_RUN
    assert bundle.evidence.applicability is ExperimentApplicability.CURRENT
    assert "whole-game balance" in bundle.evidence.assumptions[0]
    assert "scenario_ai" in " ".join(bundle.evidence.assumptions)
    with PlayTraceStore(tmp_path / "evidence.db") as store:
        store.save_snapshot(snapshot)
        store.save_hypothesis(snapshot.id, bundle.hypothesis)
        store.save_experiment_evidence(bundle.evidence)
        store.save_claim(snapshot.id, bundle.claim)
        assert store.load_experiment_evidence(bundle.evidence.id) == bundle.evidence


def test_changed_sources_and_missing_provenance_are_stale(project):
    events = events_for(project)
    (project / "rules.md").write_text("Changed working rule.\n")
    snapshot = UnrealLocalAdapter(project).snapshot()
    for revision in [events[0]["data"]["GameRevision"], "unrecorded"]:
        events[0]["data"]["GameRevision"] = revision
        capture = load_unreal_capture(write_capture(project, events))
        bundle = unreal_evidence(snapshot, capture, hypothesis(), EvidenceRelation.CONTEXT, "Recorded before edit")
        assert bundle.evidence.applicability is ExperimentApplicability.STALE


@pytest.mark.parametrize("index,key,bad", [(2, "sequence", 20), (2, "run_id", "bad"), (2, "authority", "client"),
    (2, "elapsed_seconds", -1), (2, "elapsed_seconds", float("nan")), (2, "elapsed_seconds", True),
    (2, "schema_version", True), (2, "schema_version", 99)])
def test_bad_envelopes_rejected(project, index, key, bad):
    events = events_for(project)
    events[index][key] = bad
    with pytest.raises(ValueError):
        load_unreal_capture(write_capture(project, events))


@pytest.mark.parametrize("mutation", ["damage", "outcome", "unknown_actor", "incomplete", "unknown_semantics", "repeated_start"])
def test_incoherent_combat_rejected(project, mutation):
    events = events_for(project)
    if mutation == "damage": events[3]["data"]["health_after"] = 8
    if mutation == "outcome": events[-1]["data"]["outcome"] = "defeat"
    if mutation == "unknown_actor": events[3]["data"]["actor_id"] = "missing"
    if mutation == "incomplete": events.pop()
    if mutation == "unknown_semantics": events[3]["event_type"] = "combat.secret_modifier"
    if mutation == "repeated_start": events[3]["event_type"] = "run.started"
    with pytest.raises(ValueError):
        load_unreal_capture(write_capture(project, events))


def test_wrong_project_and_duplicate_json_rejected(project):
    events = events_for(project)
    events[0]["data"]["project_id"] = "another-project"
    capture = load_unreal_capture(write_capture(project, events))
    with pytest.raises(ValueError, match="different project"):
        unreal_evidence(UnrealLocalAdapter(project).snapshot(), capture, hypothesis(), EvidenceRelation.CONTEXT, "Test")
    path = write_capture(project, events)
    path.write_text(path.read_text().replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1))
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        load_unreal_capture(path)


def test_cli_local_snapshot_and_telemetry_roundtrip(project, monkeypatch, capsys):
    from playtrace.cli import main
    capture = write_capture(project, events_for(project))
    output = project / ".playtrace" / "snapshot.json"
    database = project / ".playtrace" / "playtrace.db"
    monkeypatch.setattr("sys.argv", ["playtrace", "snapshot", str(project), "--local", "--db", str(database), "--output", str(output)])
    main()
    assert json.loads(capsys.readouterr().out)["snapshot"]["adapter_id"] == "unreal-local-v1"
    intent = project / ".playtrace" / "hypothesis.json"
    intent.write_text(hypothesis().model_dump_json())
    monkeypatch.setattr("sys.argv", ["playtrace", "record-experiment", str(project), "--snapshot", str(output),
        "--hypothesis", str(intent), "--telemetry", str(capture), "--relation", "context", "--rationale", "Synthetic CLI check"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["evidence"]["kind"] == "telemetry_run"
    assert result["evidence"]["applicability"] == "current"
    assert (project / ".playtrace" / "experiment-evidence.json").exists()


def test_engine_and_design_provenance_changes_are_stale(project):
    snapshot = UnrealLocalAdapter(project).snapshot()
    for key, changed in [("engine_version", "5.9.0-test"), ("GDDDigest", "unrecorded")]:
        events = events_for(project)
        events[0]["data"][key] = changed
        bundle = unreal_evidence(snapshot, load_unreal_capture(write_capture(project, events)),
            hypothesis(), EvidenceRelation.CONTEXT, "Different basis")
        assert bundle.evidence.applicability is ExperimentApplicability.STALE
