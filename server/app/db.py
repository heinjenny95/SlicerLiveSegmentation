from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS live_rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    volume_signature TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 3,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_operations (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    room_id TEXT NOT NULL REFERENCES live_rooms(id) ON DELETE CASCADE,
    client_operation_id TEXT NOT NULL,
    author TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    segment_name TEXT NOT NULL,
    color_hex TEXT NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('patch', 'snapshot')),
    volume_shape TEXT NOT NULL,
    voxel_bbox TEXT NOT NULL,
    encoding TEXT NOT NULL,
    payload TEXT NOT NULL,
    base_sequence INTEGER NOT NULL DEFAULT 0,
    snapshot_group_id TEXT,
    snapshot_group_index INTEGER,
    snapshot_group_count INTEGER,
    system_snapshot INTEGER NOT NULL DEFAULT 0,
    snapshot_label TEXT,
    segment_deleted INTEGER NOT NULL DEFAULT 0,
    metadata_update INTEGER NOT NULL DEFAULT 0,
    undo_of_sequence INTEGER,
    changed_voxels INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (room_id, author, client_operation_id)
);

CREATE INDEX IF NOT EXISTS idx_live_operations_room_sequence
    ON live_operations(room_id, sequence);

CREATE TABLE IF NOT EXISTS live_chat_messages (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    room_id TEXT NOT NULL REFERENCES live_rooms(id) ON DELETE CASCADE,
    client_message_id TEXT NOT NULL,
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    anchor TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (room_id, author, client_message_id)
);

CREATE INDEX IF NOT EXISTS idx_live_chat_room_sequence
    ON live_chat_messages(room_id, sequence);

CREATE TABLE IF NOT EXISTS live_segment_locks (
    room_id TEXT NOT NULL REFERENCES live_rooms(id) ON DELETE CASCADE,
    segment_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    expires_at TEXT,
    expires_epoch REAL NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (room_id, segment_id)
);

CREATE TABLE IF NOT EXISTS live_room_roles (
    room_id TEXT NOT NULL REFERENCES live_rooms(id) ON DELETE CASCADE,
    user TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer','editor','reviewer','admin')),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (room_id, user)
);

CREATE TABLE IF NOT EXISTS live_reviews (
    room_id TEXT NOT NULL REFERENCES live_rooms(id) ON DELETE CASCADE,
    segment_id TEXT NOT NULL,
    state TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (room_id, segment_id)
);

CREATE TABLE IF NOT EXISTS live_access_requests (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES live_rooms(id) ON DELETE CASCADE,
    segment_id TEXT NOT NULL,
    requester TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_conflicts (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES live_rooms(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    author TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    other_author TEXT NOT NULL,
    other_sequence INTEGER NOT NULL,
    overlap_voxels INTEGER NOT NULL,
    resolution TEXT NOT NULL DEFAULT 'unresolved',
    resolved_by TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_material_templates (
    room_id TEXT PRIMARY KEY REFERENCES live_rooms(id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    room_id TEXT NOT NULL REFERENCES live_rooms(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    user TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            # Rooms created by an older server have scene-local label IDs and
            # cannot be made collision-safe retroactively. Mark those migrated
            # rows as legacy; new inserts always specify schema 3 explicitly.
            self._ensure_column(
                connection,
                "live_rooms",
                "schema_version",
                "INTEGER NOT NULL DEFAULT 2",
            )
            self._ensure_column(connection, "live_operations", "base_sequence", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "live_operations", "snapshot_group_id", "TEXT")
            self._ensure_column(connection, "live_operations", "snapshot_group_index", "INTEGER")
            self._ensure_column(connection, "live_operations", "snapshot_group_count", "INTEGER")
            self._ensure_column(connection, "live_operations", "system_snapshot", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "live_operations", "snapshot_label", "TEXT")
            self._ensure_column(
                connection,
                "live_operations",
                "segment_deleted",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "live_operations", "changed_voxels", "INTEGER")
            self._ensure_column(
                connection,
                "live_operations",
                "metadata_update",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "live_operations", "undo_of_sequence", "INTEGER")
            self._ensure_column(connection, "live_chat_messages", "anchor", "TEXT")
            self._ensure_column(connection, "live_segment_locks", "expires_at", "TEXT")
            self._ensure_column(connection, "live_segment_locks", "expires_epoch", "REAL NOT NULL DEFAULT 0")

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
