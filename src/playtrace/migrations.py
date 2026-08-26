"""Versioned, transactional SQLite schema migrations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable


LATEST_SCHEMA_VERSION = 3


class DatabaseVersionError(RuntimeError):
    """The database cannot safely be opened by this Play Trace build."""


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]


M6_SCHEMA = Migration(
    version=2,
    statements=(
        """CREATE TABLE IF NOT EXISTS workspace_cache (
               project_id TEXT NOT NULL,
               file_id TEXT NOT NULL,
               revision TEXT NOT NULL,
               metadata TEXT NOT NULL,
               payload TEXT NOT NULL,
               observed_at TEXT NOT NULL,
               PRIMARY KEY (project_id, file_id, revision)
           )""",
        """CREATE TABLE IF NOT EXISTS workspace_cursors (
               project_id TEXT PRIMARY KEY,
               cursor TEXT NOT NULL,
               observed_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS write_sagas (
               id TEXT PRIMARY KEY,
               proposal_id TEXT NOT NULL,
               attempt_id TEXT NOT NULL,
               status TEXT NOT NULL,
               payload TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS critic_runs (
               id TEXT PRIMARY KEY,
               project_id TEXT NOT NULL,
               payload TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS local_jobs (
               id TEXT PRIMARY KEY,
               kind TEXT NOT NULL,
               status TEXT NOT NULL,
               payload TEXT NOT NULL
           )""",
    ),
)


M6A_INTEGRITY_SCHEMA = Migration(
    version=3,
    statements=(
        """CREATE TABLE IF NOT EXISTS artifact_integrity (
               kind TEXT NOT NULL,
               id TEXT NOT NULL,
               schema TEXT NOT NULL,
               seal TEXT NOT NULL,
               PRIMARY KEY (kind, id)
           )""",
        """CREATE TABLE IF NOT EXISTS identity_metadata (
               key TEXT PRIMARY KEY,
               value TEXT NOT NULL
           )""",
    ),
)


class MigrationManager:
    """Apply each schema version in one explicit SQLite transaction."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        initial_statements: Iterable[str],
    ) -> None:
        self.connection = connection
        self.initial_statements = tuple(initial_statements)

    def current_version(self) -> int:
        return validate_database_version(self.connection)

    def migrate(self, target: int = LATEST_SCHEMA_VERSION) -> int:
        current = self.current_version()
        if current > LATEST_SCHEMA_VERSION:
            raise DatabaseVersionError(
                f"database schema {current} is newer than supported schema "
                f"{LATEST_SCHEMA_VERSION}; refusing to open"
            )
        if target < current or target > LATEST_SCHEMA_VERSION:
            raise DatabaseVersionError(
                f"invalid migration target {target} for current schema {current}"
            )
        if current == 0 and target >= 1:
            # Version-zero databases may be empty or pre-M6 stores. CREATE IF NOT
            # EXISTS adopts the latter without rewriting their working records.
            self._apply(1, self.initial_statements)
            current = 1
        if current == 1 and target >= 2:
            self._apply(M6_SCHEMA.version, M6_SCHEMA.statements)
            current = 2
        if current == 2 and target >= 3:
            self._apply(M6A_INTEGRITY_SCHEMA.version, M6A_INTEGRITY_SCHEMA.statements)
            current = 3
        return current

    def _apply(self, version: int, statements: Iterable[str]) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                if statement.strip():
                    self.connection.execute(statement)
            self.connection.execute(f"PRAGMA user_version = {version}")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


def split_schema_statements(script: str) -> tuple[str, ...]:
    """Split Play Trace's simple schema script without executing implicit commits."""

    statements: list[str] = []
    buffer = ""
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("PRAGMA foreign_keys"):
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip().rstrip(";"))
            buffer = ""
    if buffer.strip():
        raise ValueError("incomplete SQLite schema statement")
    return tuple(statements)


def validate_database_version(connection: sqlite3.Connection) -> int:
    """Refuse databases written by a newer Play Trace before reading records."""

    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > LATEST_SCHEMA_VERSION:
        raise DatabaseVersionError(
            f"database schema {current} is newer than supported schema "
            f"{LATEST_SCHEMA_VERSION}; refusing to open"
        )
    return current
