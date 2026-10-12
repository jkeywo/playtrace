from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime, time, timezone
from enum import StrEnum
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel

from playtrace.adapters import SyntheticProjectAdapter
from playtrace.canonical import (
    CANONICAL_IDENTITY_SCHEMA,
    CANONICAL_IDENTITY_VERSION,
    CanonicalEncodingError,
    canonical_digest,
    canonical_json,
)
from playtrace.extensions import ExtensionKind, propose_extension
from playtrace.models import ChangeOperation, ChangeSet, SourceMedium
from playtrace.patches import DocumentTextPatch, SemanticPatchSet
from playtrace.proposals import DesignProposal, ProposalBasis, create_proposal
from playtrace.regressions import RegressionSuiteRun
from playtrace.simulation import snapshot_content_digest
from playtrace.storage import PlayTraceStore


def _legacy_digest(payload: dict[str, object], *, excluded: set[str]) -> str:
    content = {key: value for key, value in payload.items() if key not in excluded}
    return hashlib.sha256(
        json.dumps(
            content,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _seed_legacy_identity_database(
    database: Path,
    *,
    invalid_proposal_history: bool = False,
    corrupt_extension_digest: bool = False,
) -> tuple[str, str, str]:
    record = _proposal_record()
    proposal = record.proposal.model_dump(mode="json")
    proposal.pop("digest_schema")
    proposal.pop("canonical_seal")
    proposal["basis_digest"] = _legacy_digest(
        proposal, excluded={"basis_digest", "state", "created_at"}
    )
    extension_model = propose_extension(
        id="extension:legacy-epoch",
        project_id="game:test",
        kind=ExtensionKind.RULE_IR,
        title="Legacy epoch extension",
        rationale="Exercise database-wide identity adoption.",
        source_text="legacy rule",
        proposed_tests=("remains inert",),
        basis_ids=("finding:legacy",),
        generator="legacy-generator",
    )
    extension = extension_model.model_dump(mode="json")
    extension.pop("digest_schema")
    extension.pop("canonical_seal")
    extension["digest"] = _legacy_digest(
        extension, excluded={"digest", "state", "history", "created_at"}
    )
    if corrupt_extension_digest:
        extension["digest"] = "0" * 64
    run: dict[str, object] = {
        "id": "regression:legacy-epoch",
        "suite_id": "suite:legacy-epoch",
        "project_id": "game:test",
        "snapshot_id": "snapshot:legacy",
        "passed": True,
        "case_results": [],
        "digest": "",
    }
    run["digest"] = _legacy_digest(run, excluded={"digest"})

    with PlayTraceStore(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM identity_metadata WHERE key = 'identity_adoption_complete'"
        )
        connection.execute(
            """INSERT INTO proposal_records(id, project_id, basis_digest, payload)
               VALUES (?, ?, ?, ?)""",
            (record.proposal.id, record.proposal.project_id, proposal["basis_digest"], json.dumps(proposal)),
        )
        transition = record.history[0].model_dump(mode="json")
        if invalid_proposal_history:
            transition["sequence"] = 2
        connection.execute(
            """INSERT INTO proposal_transitions(proposal_id, sequence, payload)
               VALUES (?, ?, ?)""",
            (record.proposal.id, transition["sequence"], json.dumps(transition)),
        )
        connection.execute(
            "INSERT INTO extension_proposals(id, project_id, payload) VALUES (?, ?, ?)",
            (extension_model.id, extension_model.project_id, json.dumps(extension)),
        )
        connection.execute(
            "INSERT INTO regression_runs(id, suite_id, payload) VALUES (?, ?, ?)",
            (run["id"], run["suite_id"], json.dumps(run)),
        )
    return record.proposal.id, extension_model.id, str(run["id"])


def test_envelope_is_versioned_domain_separated_and_order_independent() -> None:
    left = {"values": frozenset({"third", "first", "second"}), "count": 3}
    right = {"count": 3, "values": frozenset({"second", "third", "first"})}

    assert canonical_digest(left, domain="test-identity") == canonical_digest(
        right, domain="test-identity"
    )
    assert canonical_digest(left, domain="test-identity") != canonical_digest(
        left, domain="different-identity"
    )
    envelope = json.loads(canonical_json(left, domain="test-identity"))
    assert envelope["schema"] == CANONICAL_IDENTITY_SCHEMA
    assert envelope["version"] == CANONICAL_IDENTITY_VERSION
    assert envelope["domain"] == "test-identity"
    assert envelope["value"]["t"] == "map"
    values = _map_member(envelope["value"], "values")
    assert values["t"] == "frozenset"
    assert [item["v"] for item in values["v"]] == ["first", "second", "third"]


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        datetime(2026, 1, 1),
        time(12, 30),
        time(12, 30, tzinfo=timezone.utc),
        {1: "non-string-key"},
        "\ud800",
    ],
)
def test_ambiguous_or_nonportable_identity_values_are_rejected(value: object) -> None:
    with pytest.raises(CanonicalEncodingError):
        canonical_digest(value, domain="invalid-value")


def test_datetime_and_path_have_portable_identity_forms() -> None:
    envelope = json.loads(
        canonical_json(
            {
                "when": datetime(2026, 8, 26, 12, 30, tzinfo=UTC),
                "where": Path("design") / "rules" / "core.md",
            },
            domain="portable-values",
        )
    )
    assert _map_member(envelope["value"], "when") == {
        "t": "datetime",
        "v": "2026-08-26T12:30:00Z",
    }
    assert _map_member(envelope["value"], "where") == {
        "t": "path",
        "v": "design/rules/core.md",
    }


class _IdentityEnum(StrEnum):
    VALUE = "same"


class _IdentityModel(BaseModel):
    value: str


@pytest.mark.parametrize(
    ("typed", "user_json"),
    [
        (
            datetime(2026, 8, 26, 12, 30, tzinfo=UTC),
            {"t": "datetime", "v": "2026-08-26T12:30:00Z"},
        ),
        (date(2026, 8, 26), {"t": "date", "v": "2026-08-26"}),
        (frozenset({"same"}), ["same"]),
        ({"same"}, ["same"]),
        (("same",), ["same"]),
        (b"same", {"t": "bytes", "v": "c2FtZQ=="}),
        (
            UUID("00000000-0000-0000-0000-000000000001"),
            {"t": "uuid", "v": "00000000-0000-0000-0000-000000000001"},
        ),
        (Path("same"), {"t": "path", "v": "same"}),
        (_IdentityEnum.VALUE, "same"),
        (_IdentityModel(value="same"), {"value": "same"}),
        (None, {"t": "none"}),
        (True, 1),
        (1, 1.0),
    ],
)
def test_tagged_algebra_cannot_collide_with_user_json(
    typed: object, user_json: object
) -> None:
    assert canonical_digest(typed, domain="collision") != canonical_digest(
        user_json, domain="collision"
    )


def test_every_collection_has_an_explicit_distinct_tag() -> None:
    nodes = {
        name: json.loads(canonical_json(value, domain="collections"))["value"]
        for name, value in {
            "map": {"item": "same"},
            "list": ["same"],
            "tuple": ("same",),
            "set": {"same"},
            "frozenset": frozenset({"same"}),
        }.items()
    }
    assert {name: node["t"] for name, node in nodes.items()} == {
        "map": "map",
        "list": "list",
        "tuple": "tuple",
        "set": "set",
        "frozenset": "frozenset",
    }
    assert len({canonical_digest(value, domain="collections") for value in (
        ["same"],
        ("same",),
        {"same"},
        frozenset({"same"}),
    )}) == 4


def test_digest_is_identical_across_python_hash_seeds() -> None:
    script = """
from playtrace.canonical import canonical_digest
value = {'capabilities': frozenset({'rules', 'simulation', 'evidence', 'review'})}
print(canonical_digest(value, domain='proposal-approval'))
"""
    results = []
    for seed in ("1", "2", "17", "random"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        results.append(result.stdout.strip())
    assert len(set(results)) == 1


def test_snapshot_identity_survives_json_and_sqlite_roundtrips(tmp_path: Path) -> None:
    snapshot = SyntheticProjectAdapter(SourceMedium.SYNTHETIC).snapshot()
    json_copy = type(snapshot).model_validate_json(snapshot.model_dump_json())
    assert snapshot_content_digest(json_copy) == snapshot_content_digest(snapshot)

    with PlayTraceStore(tmp_path / "identity.db") as store:
        store.save_snapshot(snapshot)
        sqlite_copy = store.load_snapshot(snapshot.id)
    assert sqlite_copy is not None
    assert snapshot_content_digest(sqlite_copy) == snapshot_content_digest(snapshot)


def test_proposal_approval_identity_survives_json_roundtrip() -> None:
    proposal = _proposal_record().proposal
    restored = DesignProposal.model_validate_json(proposal.model_dump_json())
    assert restored.approval_digest() == proposal.approval_digest()
    assert restored.canonical_seal == proposal.canonical_seal


def test_legacy_proposal_is_sealed_once_and_future_tamper_fails(tmp_path: Path) -> None:
    record = _proposal_record()
    proposal_payload = record.proposal.model_dump(mode="json")
    proposal_payload.pop("digest_schema")
    proposal_payload.pop("canonical_seal")
    proposal_payload["basis_digest"] = _legacy_digest(
        proposal_payload,
        excluded={"basis_digest", "state", "created_at"},
    )
    database = tmp_path / "legacy-proposal.db"
    with PlayTraceStore(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM identity_metadata WHERE key = 'identity_adoption_complete'"
        )
        connection.execute(
                "INSERT INTO proposal_records(id, project_id, basis_digest, payload) VALUES (?, ?, ?, ?)",
                (
                    record.proposal.id,
                    record.proposal.project_id,
                    proposal_payload["basis_digest"],
                    json.dumps(proposal_payload),
                ),
            )
        for transition in record.history:
            connection.execute(
                "INSERT INTO proposal_transitions(proposal_id, sequence, payload) VALUES (?, ?, ?)",
                (
                    record.proposal.id,
                    transition.sequence,
                    transition.model_dump_json(),
                ),
            )

    with PlayTraceStore(database) as store:
        adopted = store.load_proposal(record.proposal.id)
        assert adopted is not None
        assert adopted.proposal.basis_digest == proposal_payload["basis_digest"]
        assert adopted.proposal.digest_schema == "legacy-json-sha256-v0"
        assert adopted.proposal.canonical_seal

        stored = json.loads(
            store.connection.execute(
                "SELECT payload FROM proposal_records WHERE id = ?",
                (record.proposal.id,),
            ).fetchone()[0]
        )
        stored["title"] = "tampered after adoption"
        stored.pop("canonical_seal")
        with store.connection:
            store.connection.execute(
                "UPDATE proposal_records SET payload = ? WHERE id = ?",
                (json.dumps(stored), record.proposal.id),
            )
        with pytest.raises(ValueError, match="external integrity seal"):
            store.load_proposal(record.proposal.id)

        with store.connection:
            store.connection.execute(
                "UPDATE proposal_records SET payload = ? WHERE id = ?",
                (adopted.proposal.model_dump_json(), record.proposal.id),
            )
            store.connection.execute(
                "DELETE FROM artifact_integrity WHERE kind = ? AND id = ?",
                ("design_proposal", record.proposal.id),
            )
        with pytest.raises(ValueError, match="missing its external integrity seal"):
            store.load_proposal(record.proposal.id)


def test_canonical_proposal_cannot_downgrade_or_replace_external_seal(
    tmp_path: Path,
) -> None:
    record = _proposal_record()
    with PlayTraceStore(tmp_path / "canonical-proposal.db") as store:
        store.save_proposal(record)
        integrity = store.connection.execute(
            "SELECT schema, seal FROM artifact_integrity WHERE kind = ? AND id = ?",
            ("design_proposal", record.proposal.id),
        ).fetchone()
        assert integrity is not None
        assert integrity["schema"] == record.proposal.digest_schema
        assert integrity["seal"] == record.proposal.canonical_seal

        payload = json.loads(
            store.connection.execute(
                "SELECT payload FROM proposal_records WHERE id = ?",
                (record.proposal.id,),
            ).fetchone()[0]
        )
        payload["digest_schema"] = "legacy-json-sha256-v0"
        payload.pop("canonical_seal")
        with store.connection:
            store.connection.execute(
                "UPDATE proposal_records SET payload = ? WHERE id = ?",
                (json.dumps(payload), record.proposal.id),
            )
        with pytest.raises(ValueError, match="external integrity seal"):
            store.load_proposal(record.proposal.id)

        with store.connection:
            store.connection.execute(
                "UPDATE proposal_records SET payload = ? WHERE id = ?",
                (record.proposal.model_dump_json(), record.proposal.id),
            )
            store.connection.execute(
                "UPDATE artifact_integrity SET seal = ? WHERE kind = ? AND id = ?",
                ("tampered-database-row", "design_proposal", record.proposal.id),
            )
        with pytest.raises(ValueError, match="external integrity seal"):
            store.load_proposal(record.proposal.id)


def test_legacy_extension_and_regression_run_are_adopted_then_sealed(
    tmp_path: Path,
) -> None:
    extension = propose_extension(
        id="extension:legacy",
        project_id="game:test",
        kind=ExtensionKind.RULE_IR,
        title="Legacy extension",
        rationale="Exercise one-time adoption.",
        source_text="legacy rule",
        proposed_tests=("remains inert",),
        basis_ids=("finding:legacy",),
        generator="legacy-generator",
    )
    extension_payload = extension.model_dump(mode="json")
    extension_payload.pop("digest_schema")
    extension_payload.pop("canonical_seal")
    extension_payload["digest"] = _legacy_digest(
        extension_payload,
        excluded={"digest", "state", "history", "created_at"},
    )
    run_payload = {
        "id": "regression:legacy",
        "suite_id": "suite:legacy",
        "project_id": "game:test",
        "snapshot_id": "snapshot:legacy",
        "passed": True,
        "case_results": [],
        "digest": "",
    }
    run_payload["digest"] = _legacy_digest(run_payload, excluded={"digest"})
    database = tmp_path / "legacy-records.db"
    with PlayTraceStore(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM identity_metadata WHERE key = 'identity_adoption_complete'"
        )
        connection.execute(
                "INSERT INTO extension_proposals(id, project_id, payload) VALUES (?, ?, ?)",
                (extension.id, extension.project_id, json.dumps(extension_payload)),
            )
        connection.execute(
                "INSERT INTO regression_runs(id, suite_id, payload) VALUES (?, ?, ?)",
                (run_payload["id"], run_payload["suite_id"], json.dumps(run_payload)),
            )

    with PlayTraceStore(database) as store:
        adopted_extension = store.load_extension_proposal(extension.id)
        adopted_run = store.load_regression_run(run_payload["id"])
        assert adopted_extension is not None and adopted_extension.canonical_seal
        assert adopted_extension.digest == extension_payload["digest"]
        assert isinstance(adopted_run, RegressionSuiteRun)
        assert adopted_run.canonical_seal
        assert adopted_run.digest == run_payload["digest"]
        integrity_rows = store.connection.execute(
            "SELECT kind, id, schema, seal FROM artifact_integrity ORDER BY kind"
        ).fetchall()
        assert {(row["kind"], row["id"]) for row in integrity_rows} == {
            ("extension_proposal", extension.id),
            ("regression_run", run_payload["id"]),
        }

        tampered = json.loads(
            store.connection.execute(
                "SELECT payload FROM extension_proposals WHERE id = ?", (extension.id,)
            ).fetchone()[0]
        )
        tampered["source_text"] = "changed after sealing"
        tampered.pop("canonical_seal")
        with store.connection:
            store.connection.execute(
                "UPDATE extension_proposals SET payload = ? WHERE id = ?",
                (json.dumps(tampered), extension.id),
            )
        with pytest.raises(ValueError, match="external integrity seal"):
            store.load_extension_proposal(extension.id)

        tampered_run = json.loads(
            store.connection.execute(
                "SELECT payload FROM regression_runs WHERE id = ?", (run_payload["id"],)
            ).fetchone()[0]
        )
        tampered_run["passed"] = False
        tampered_run.pop("canonical_seal")
        with store.connection:
            store.connection.execute(
                "UPDATE regression_runs SET payload = ? WHERE id = ?",
                (json.dumps(tampered_run), run_payload["id"]),
            )
        with pytest.raises(ValueError, match="external integrity seal"):
            store.load_regression_run(run_payload["id"])


@pytest.mark.parametrize(
    ("table", "kind", "artifact_index"),
    (
        ("proposal_records", "design_proposal", 0),
        ("extension_proposals", "extension_proposal", 1),
        ("regression_runs", "regression_run", 2),
    ),
)
def test_closed_adoption_epoch_rejects_coordinated_downgrade_for_every_artifact(
    tmp_path: Path, table: str, kind: str, artifact_index: int
) -> None:
    database = tmp_path / f"closed-{kind}.db"
    artifact_ids = _seed_legacy_identity_database(database)
    with PlayTraceStore(database):
        pass

    artifact_id = artifact_ids[artifact_index]
    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute(
                f"SELECT payload FROM {table} WHERE id = ?", (artifact_id,)
            ).fetchone()[0]
        )
        payload.pop("canonical_seal")
        payload.pop("digest_schema", None)
        connection.execute(
            f"UPDATE {table} SET payload = ? WHERE id = ?",
            (json.dumps(payload), artifact_id),
        )
        connection.execute(
            "DELETE FROM artifact_integrity WHERE kind = ? AND id = ?",
            (kind, artifact_id),
        )

    with pytest.raises(ValueError, match="identity validation|integrity seal"):
        PlayTraceStore(database)


def test_missing_epoch_rejects_partial_embedded_proposal_seal(tmp_path: Path) -> None:
    database = tmp_path / "partial-proposal-seal.db"
    proposal_id, _, _ = _seed_legacy_identity_database(database)
    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM proposal_records WHERE id = ?", (proposal_id,)
            ).fetchone()[0]
        )
        payload["canonical_seal"] = "interrupted-adoption"
        connection.execute(
            "UPDATE proposal_records SET payload = ? WHERE id = ?",
            (json.dumps(payload), proposal_id),
        )

    with pytest.raises(ValueError, match="non-pristine.*embedded identity marker"):
        PlayTraceStore(database)

    with sqlite3.connect(database) as connection:
        stored = json.loads(
            connection.execute(
                "SELECT payload FROM proposal_records WHERE id = ?", (proposal_id,)
            ).fetchone()[0]
        )
        assert stored["canonical_seal"] == "interrupted-adoption"
        assert connection.execute("SELECT COUNT(*) FROM artifact_integrity").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM identity_metadata"
        ).fetchone()[0] == 0


def test_missing_epoch_rejects_sidecar_without_repair(tmp_path: Path) -> None:
    database = tmp_path / "sidecar-only.db"
    proposal_id, _, _ = _seed_legacy_identity_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO artifact_integrity(kind, id, schema, seal) VALUES (?, ?, ?, ?)",
            ("design_proposal", proposal_id, "playtrace.identity/1", "partial-seal"),
        )

    with pytest.raises(ValueError, match="non-pristine.*integrity sidecars"):
        PlayTraceStore(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_integrity").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM identity_metadata"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("table", "artifact_index"),
    (
        ("proposal_records", 0),
        ("extension_proposals", 1),
        ("regression_runs", 2),
    ),
)
def test_missing_epoch_rejects_partial_schema_marker_across_artifact_kinds(
    tmp_path: Path, table: str, artifact_index: int
) -> None:
    database = tmp_path / f"partial-marker-{table}.db"
    artifact_ids = _seed_legacy_identity_database(database)
    artifact_id = artifact_ids[artifact_index]
    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute(
                f"SELECT payload FROM {table} WHERE id = ?", (artifact_id,)
            ).fetchone()[0]
        )
        payload["digest_schema"] = "playtrace.identity/1"
        connection.execute(
            f"UPDATE {table} SET payload = ? WHERE id = ?",
            (json.dumps(payload), artifact_id),
        )

    with pytest.raises(ValueError, match="non-pristine.*embedded identity marker"):
        PlayTraceStore(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_integrity").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM identity_metadata"
        ).fetchone()[0] == 0


def test_deleting_epoch_from_fully_adopted_database_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "deleted-adoption-epoch.db"
    _seed_legacy_identity_database(database)
    with PlayTraceStore(database) as store:
        sidecars_before = tuple(
            tuple(row)
            for row in store.connection.execute(
                "SELECT kind, id, schema, seal FROM artifact_integrity ORDER BY kind, id"
            ).fetchall()
        )
        payloads_before = tuple(
            row[0]
            for table in ("proposal_records", "extension_proposals", "regression_runs")
            for row in store.connection.execute(
                f"SELECT payload FROM {table} ORDER BY id"
            ).fetchall()
        )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM identity_metadata WHERE key = 'identity_adoption_complete'"
        )

    with pytest.raises(ValueError, match="non-pristine.*integrity sidecars"):
        PlayTraceStore(database)

    with sqlite3.connect(database) as connection:
        assert tuple(
            connection.execute(
                "SELECT kind, id, schema, seal FROM artifact_integrity ORDER BY kind, id"
            ).fetchall()
        ) == sidecars_before
        assert tuple(
            row[0]
            for table in ("proposal_records", "extension_proposals", "regression_runs")
            for row in connection.execute(
                f"SELECT payload FROM {table} ORDER BY id"
            ).fetchall()
        ) == payloads_before
        assert connection.execute(
            "SELECT COUNT(*) FROM identity_metadata"
        ).fetchone()[0] == 0


def test_missing_epoch_rejects_orphan_identity_metadata_marker(tmp_path: Path) -> None:
    database = tmp_path / "orphan-identity-marker.db"
    _seed_legacy_identity_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO identity_metadata(key, value) VALUES (?, ?)",
            ("unexpected_identity_marker", "partial"),
        )

    with pytest.raises(ValueError, match="non-pristine.*identity marker"):
        PlayTraceStore(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT key, value FROM identity_metadata"
        ).fetchall() == [("unexpected_identity_marker", "partial")]


def test_invalid_legacy_lifecycle_rolls_back_entire_adoption_epoch(tmp_path: Path) -> None:
    database = tmp_path / "invalid-lifecycle.db"
    proposal_id, _, _ = _seed_legacy_identity_database(
        database, invalid_proposal_history=True
    )

    with pytest.raises(ValueError, match="sequence must be contiguous"):
        PlayTraceStore(database)

    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM proposal_records WHERE id = ?", (proposal_id,)
            ).fetchone()[0]
        )
        assert "canonical_seal" not in payload
        assert connection.execute("SELECT COUNT(*) FROM artifact_integrity").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM identity_metadata WHERE key = 'identity_adoption_complete'"
        ).fetchone()[0] == 0


def test_late_invalid_artifact_rolls_back_earlier_staged_adoptions(tmp_path: Path) -> None:
    database = tmp_path / "partial-adoption.db"
    proposal_id, _, _ = _seed_legacy_identity_database(
        database, corrupt_extension_digest=True
    )

    with pytest.raises(ValueError, match="historical digest"):
        PlayTraceStore(database)

    with sqlite3.connect(database) as connection:
        proposal = json.loads(
            connection.execute(
                "SELECT payload FROM proposal_records WHERE id = ?", (proposal_id,)
            ).fetchone()[0]
        )
        assert "canonical_seal" not in proposal
        assert connection.execute("SELECT COUNT(*) FROM artifact_integrity").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_metadata").fetchone()[0] == 0


def test_identity_adoption_reopen_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "idempotent-adoption.db"
    _seed_legacy_identity_database(database)
    with PlayTraceStore(database) as store:
        before = tuple(
            tuple(row)
            for row in store.connection.execute(
                "SELECT kind, id, schema, seal FROM artifact_integrity ORDER BY kind, id"
            ).fetchall()
        )
        payloads_before = tuple(
            store.connection.execute(f"SELECT payload FROM {table} ORDER BY id").fetchall()[0][0]
            for table in ("proposal_records", "extension_proposals", "regression_runs")
        )

    with PlayTraceStore(database) as reopened:
        after = tuple(
            tuple(row)
            for row in reopened.connection.execute(
                "SELECT kind, id, schema, seal FROM artifact_integrity ORDER BY kind, id"
            ).fetchall()
        )
        payloads_after = tuple(
            reopened.connection.execute(f"SELECT payload FROM {table} ORDER BY id").fetchall()[0][0]
            for table in ("proposal_records", "extension_proposals", "regression_runs")
        )
        epoch = reopened.connection.execute(
            "SELECT value FROM identity_metadata WHERE key = 'identity_adoption_complete'"
        ).fetchone()[0]
    assert after == before
    assert payloads_after == payloads_before
    assert epoch == "playtrace.identity/1"


def test_m6a_store_initializes_without_optional_m6c_migrations(tmp_path: Path) -> None:
    database = tmp_path / "isolated-m6a.db"
    script = """
import sqlite3
import sys
from pathlib import Path
sys.modules['playtrace.migrations'] = None
from playtrace.storage import PlayTraceStore
database = Path(sys.argv[1])
with PlayTraceStore(database) as store:
    print(store.schema_version)
    print(store.connection.execute(
        "SELECT value FROM identity_metadata WHERE key = 'identity_adoption_complete'"
    ).fetchone()[0])
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", script, str(database)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.stdout.splitlines() == ["1", "playtrace.identity/1"]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_integrity"
        ).fetchone()[0] == 0


def _proposal_record():
    snapshot = SyntheticProjectAdapter(SourceMedium.SYNTHETIC).snapshot()
    change_set = ChangeSet(
        id="change:identity",
        intent_id="intent:identity",
        base_snapshot_id=snapshot.id,
        operations=(
            ChangeOperation(
                target_id="source:rules",
                operation="replace_text",
                before="Old",
                after="New",
            ),
        ),
        rationale_claim_ids=(),
        validation=("identity",),
    )
    patch_set = SemanticPatchSet(
        id="patch:identity",
        base_snapshot_id=snapshot.id,
        document_patches=(
            DocumentTextPatch(
                source_id="source:rules",
                file_id="doc-1",
                required_revision_id="r1",
                tab_id="t.0",
                start_index=1,
                end_index=4,
                expected_text="Old",
                replacement_text="New",
            ),
        ),
    )
    return create_proposal(
        id="proposal:identity",
        project_id=snapshot.project_id,
        title="Canonical identity",
        question="Should this identity remain stable?",
        rationale="Approval must survive interpreter hash randomization.",
        alternatives=("Keep old", "Use new"),
        criteria=("Stable identity",),
        change_set=change_set,
        patch_set=patch_set,
        basis=ProposalBasis(snapshot=snapshot),
        created_by="test",
    )


def _map_member(node: dict[str, object], key: str) -> object:
    assert node["t"] == "map"
    for encoded_key, encoded_value in node["v"]:  # type: ignore[union-attr]
        if encoded_key == {"t": "str", "v": key}:
            return encoded_value
    raise AssertionError(f"missing canonical map key {key!r}")
