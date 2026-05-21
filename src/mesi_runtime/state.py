from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .constants import ABSENT_VERSION, events_path, state_path


class State:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.db_path = state_path(self.project_root)
        self.events_file = events_path(self.project_root)
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    workspace_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS heads (
                    path TEXT PRIMARY KEY,
                    version TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workspace_base (
                    agent_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    version TEXT NOT NULL,
                    PRIMARY KEY (agent_id, path)
                );

                CREATE TABLE IF NOT EXISTS read_set (
                    agent_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    version TEXT NOT NULL,
                    PRIMARY KEY (agent_id, path)
                );

                CREATE TABLE IF NOT EXISTS stale (
                    agent_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    old_version TEXT NOT NULL,
                    new_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (agent_id, path)
                );

                CREATE TABLE IF NOT EXISTS bash_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    type TEXT NOT NULL,
                    agent TEXT,
                    path TEXT,
                    old_version TEXT,
                    new_version TEXT,
                    reason TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('version', '1')")

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()

    def reset(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                DELETE FROM events;
                DELETE FROM bash_snapshots;
                DELETE FROM stale;
                DELETE FROM read_set;
                DELETE FROM workspace_base;
                DELETE FROM heads;
                DELETE FROM agents;
                DELETE FROM sqlite_sequence WHERE name = 'events';
                """
            )
        if self.events_file.exists():
            self.events_file.unlink()

    def emit(
        self,
        event_type: str,
        *,
        agent: str | None = None,
        path: str | None = None,
        old_version: str | None = None,
        new_version: str | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        metadata = metadata or {}
        timestamp = self.now()

        def _insert(active_conn: sqlite3.Connection) -> dict[str, Any]:
            cur = active_conn.execute(
                """
                INSERT INTO events(timestamp, type, agent, path, old_version, new_version, reason, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    event_type,
                    agent,
                    path,
                    old_version,
                    new_version,
                    reason,
                    json.dumps(metadata, sort_keys=True),
                ),
            )
            event = {
                "seq": int(cur.lastrowid),
                "timestamp": timestamp,
                "type": event_type,
                "agent": agent,
                "path": path,
                "old_version": old_version,
                "new_version": new_version,
                "reason": reason,
                "metadata": metadata,
            }
            self.events_file.parent.mkdir(parents=True, exist_ok=True)
            with self.events_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, sort_keys=True) + "\n")
            return event

        if conn is not None:
            return _insert(conn)
        with self._lock, self.connect() as active_conn:
            return _insert(active_conn)

    def events_after(self, after: int = 0) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT seq, timestamp, type, agent, path, old_version, new_version, reason, metadata_json
                FROM events
                WHERE seq > ?
                ORDER BY seq ASC
                """,
                (after,),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": row["seq"],
            "timestamp": row["timestamp"],
            "type": row["type"],
            "agent": row["agent"],
            "path": row["path"],
            "old_version": row["old_version"],
            "new_version": row["new_version"],
            "reason": row["reason"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }

    def upsert_head(self, path: str, version: str, *, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO heads(path, version) VALUES (?, ?)
            ON CONFLICT(path) DO UPDATE SET version = excluded.version
            """,
            (path, version),
        )

    def get_head(self, path: str, *, conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT version FROM heads WHERE path = ?", (path,)).fetchone()
        return row["version"] if row else ABSENT_VERSION

    def all_heads(self, *, conn: sqlite3.Connection) -> dict[str, str]:
        rows = conn.execute("SELECT path, version FROM heads ORDER BY path ASC").fetchall()
        return {row["path"]: row["version"] for row in rows}

    def agent_exists(self, agent: str, *, conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT 1 FROM agents WHERE agent_id = ? LIMIT 1", (agent,)).fetchone()
        return row is not None

    def set_workspace_base(self, agent: str, path: str, version: str, *, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO workspace_base(agent_id, path, version) VALUES (?, ?, ?)
            ON CONFLICT(agent_id, path) DO UPDATE SET version = excluded.version
            """,
            (agent, path, version),
        )

    def get_workspace_base(self, agent: str, path: str, *, conn: sqlite3.Connection) -> str | None:
        row = conn.execute(
            "SELECT version FROM workspace_base WHERE agent_id = ? AND path = ?",
            (agent, path),
        ).fetchone()
        return row["version"] if row else None

    def set_read(self, agent: str, path: str, version: str, *, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO read_set(agent_id, path, version) VALUES (?, ?, ?)
            ON CONFLICT(agent_id, path) DO UPDATE SET version = excluded.version
            """,
            (agent, path, version),
        )

    def stale_for_agent(self, agent: str, *, conn: sqlite3.Connection | None = None) -> list[dict[str, str]]:
        def _fetch(active_conn: sqlite3.Connection) -> list[dict[str, str]]:
            rows = active_conn.execute(
                """
                SELECT agent_id, path, old_version, new_version, created_at
                FROM stale
                WHERE agent_id = ?
                ORDER BY created_at ASC, path ASC
                """,
                (agent,),
            ).fetchall()
            return [dict(row) for row in rows]

        if conn is not None:
            return _fetch(conn)
        with self.connect() as active_conn:
            return _fetch(active_conn)

    def has_stale(self, agent: str, *, conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT 1 FROM stale WHERE agent_id = ? LIMIT 1", (agent,)).fetchone()
        return row is not None

    def clear_stale(self, agent: str, paths: Iterable[str], *, conn: sqlite3.Connection) -> None:
        conn.executemany("DELETE FROM stale WHERE agent_id = ? AND path = ?", [(agent, path) for path in paths])

    def mark_stale_readers(
        self,
        *,
        writer: str,
        path: str,
        old_version: str,
        new_version: str,
        conn: sqlite3.Connection,
    ) -> list[str]:
        rows = conn.execute(
            """
            SELECT agent_id FROM read_set
            WHERE path = ? AND version = ? AND agent_id != ?
            ORDER BY agent_id ASC
            """,
            (path, old_version, writer),
        ).fetchall()
        stale_agents: list[str] = []
        for row in rows:
            agent = row["agent_id"]
            stale_agents.append(agent)
            existing = conn.execute(
                "SELECT old_version FROM stale WHERE agent_id = ? AND path = ?",
                (agent, path),
            ).fetchone()
            old = existing["old_version"] if existing else old_version
            conn.execute(
                """
                INSERT INTO stale(agent_id, path, old_version, new_version, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, path) DO UPDATE SET new_version = excluded.new_version
                """,
                (agent, path, old, new_version, self.now()),
            )
            self.emit(
                "stale",
                agent=agent,
                path=path,
                old_version=old,
                new_version=new_version,
                reason="head_advanced",
                metadata={"writer": writer},
                conn=conn,
            )
        existing_stale_rows = conn.execute(
            """
            SELECT agent_id, old_version FROM stale
            WHERE path = ? AND agent_id != ?
            ORDER BY agent_id ASC
            """,
            (path, writer),
        ).fetchall()
        for row in existing_stale_rows:
            agent = row["agent_id"]
            if agent in stale_agents:
                continue
            conn.execute(
                "UPDATE stale SET new_version = ? WHERE agent_id = ? AND path = ?",
                (new_version, agent, path),
            )
            self.emit(
                "stale",
                agent=agent,
                path=path,
                old_version=row["old_version"],
                new_version=new_version,
                reason="head_advanced",
                metadata={"writer": writer},
                conn=conn,
            )
            stale_agents.append(agent)
        return stale_agents
