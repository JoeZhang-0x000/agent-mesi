from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from .constants import ABSENT_VERSION, store_dir, workspace_dir
from .errors import Blocked, Conflict, NotFound, Unsupported
from .paths import (
    copy_file,
    copy_tree_contents,
    file_version,
    iter_managed_files,
    managed_target,
    normalize_managed_path,
    snapshot,
    write_text_file,
)
from .state import State


class Runtime:
    def __init__(self, project_root: Path | str):
        self.project_root = Path(project_root).expanduser().resolve()
        self.state = State(self.project_root)

    @property
    def store(self) -> Path:
        return store_dir(self.project_root)

    def workspace(self, agent: str) -> Path:
        return workspace_dir(self.project_root, agent)

    def init_project(self, source_root: Path | str | None = None) -> dict[str, Any]:
        source = Path(source_root or self.project_root).expanduser().resolve()
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.store.mkdir(parents=True, exist_ok=True)
        self.state.reset()

        if self.store.exists():
            shutil.rmtree(self.store)
        self.store.mkdir(parents=True, exist_ok=True)

        for rel_path in iter_managed_files(source):
            copy_file(source, self.store, rel_path)

        with self.state._lock, self.state.connect() as conn:
            heads = snapshot(self.store)
            for rel_path, version in heads.items():
                self.state.upsert_head(rel_path, version, conn=conn)
            event = self.state.emit("init", metadata={"files": len(heads)}, conn=conn)
        return {"ok": True, "files": len(heads), "event": event}

    def create_agent(self, agent: str) -> dict[str, Any]:
        self._validate_agent(agent)
        ws = self.workspace(agent)
        if ws.exists():
            shutil.rmtree(ws)
        ws.mkdir(parents=True, exist_ok=True)
        copy_tree_contents(self.store, ws)

        with self.state._lock, self.state.connect() as conn:
            now = self.state.now()
            conn.execute("DELETE FROM bash_snapshots WHERE agent_id = ?", (agent,))
            conn.execute("DELETE FROM stale WHERE agent_id = ?", (agent,))
            conn.execute("DELETE FROM read_set WHERE agent_id = ?", (agent,))
            conn.execute("DELETE FROM workspace_base WHERE agent_id = ?", (agent,))
            conn.execute(
                """
                INSERT INTO agents(agent_id, workspace_path, created_at) VALUES (?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET workspace_path = excluded.workspace_path
                """,
                (agent, str(ws), now),
            )
            for rel_path, version in self.state.all_heads(conn=conn).items():
                self.state.set_workspace_base(agent, rel_path, version, conn=conn)
            event = self.state.emit("agent_created", agent=agent, metadata={"workspace": str(ws)}, conn=conn)
        return {"ok": True, "agent": agent, "workspace": str(ws), "event": event}

    def read(self, agent: str, path: str) -> dict[str, Any]:
        self._validate_agent(agent)
        rel = normalize_managed_path(path)
        ws = self.workspace(agent)
        store_target = managed_target(self.store, rel)
        with self.state._lock, self.state.connect() as conn:
            self._ensure_agent_exists(agent, conn=conn)
            head = self.state.get_head(rel, conn=conn)
            if head == ABSENT_VERSION or not store_target.exists():
                self.state.set_workspace_base(agent, rel, ABSENT_VERSION, conn=conn)
                self.state.set_read(agent, rel, ABSENT_VERSION, conn=conn)
                self._resolve_if_current(agent, rel, ABSENT_VERSION, conn=conn)
                event = self.state.emit(
                    "read_not_found",
                    agent=agent,
                    path=rel,
                    new_version=ABSENT_VERSION,
                    reason="absent",
                    conn=conn,
                )
                return {"ok": False, "path": rel, "version": ABSENT_VERSION, "content": None, "event": event}

            copy_file(self.store, ws, rel)
            content = managed_target(self.store, rel).read_text(encoding="utf-8")
            self.state.set_workspace_base(agent, rel, head, conn=conn)
            self.state.set_read(agent, rel, head, conn=conn)
            self._resolve_if_current(agent, rel, head, conn=conn)
            event = self.state.emit("read", agent=agent, path=rel, new_version=head, conn=conn)
            return {"ok": True, "path": rel, "version": head, "content": content, "event": event}

    def write(self, agent: str, path: str, content: str, kind: str = "write") -> dict[str, Any]:
        self._validate_agent(agent)
        rel = normalize_managed_path(path)
        with self.state._lock, self.state.connect() as conn:
            self._ensure_agent_exists(agent, conn=conn)
            self._ensure_can_write(agent, rel, conn=conn)
            ws = self.workspace(agent)
            old = self.state.get_head(rel, conn=conn)
            new = write_text_file(ws, rel, content)
            write_text_file(self.store, rel, content)
            self.state.upsert_head(rel, new, conn=conn)
            self.state.set_workspace_base(agent, rel, new, conn=conn)
            self.state.set_read(agent, rel, new, conn=conn)
            event = self.state.emit(
                "write",
                agent=agent,
                path=rel,
                old_version=old,
                new_version=new,
                metadata={"kind": kind},
                conn=conn,
            )
            stale_agents = self.state.mark_stale_readers(
                writer=agent,
                path=rel,
                old_version=old,
                new_version=new,
                conn=conn,
            )
            return {"ok": True, "path": rel, "old_version": old, "new_version": new, "stale_agents": stale_agents, "event": event}

    def refresh(self, agent: str, paths: list[str] | None = None) -> dict[str, Any]:
        self._validate_agent(agent)
        with self.state._lock, self.state.connect() as conn:
            self._ensure_agent_exists(agent, conn=conn)
            stale_rows = self.state.stale_for_agent(agent, conn=conn)
            if paths:
                requested = [normalize_managed_path(path) for path in paths]
            else:
                requested = [row["path"] for row in stale_rows]
            refreshed: list[dict[str, str]] = []
            resolved: list[str] = []
            for rel in requested:
                head = self.state.get_head(rel, conn=conn)
                copy_file(self.store, self.workspace(agent), rel)
                self.state.set_workspace_base(agent, rel, head, conn=conn)
                self.state.set_read(agent, rel, head, conn=conn)
                refreshed.append({"path": rel, "version": head})
                if any(row["path"] == rel and row["new_version"] == head for row in stale_rows):
                    resolved.append(rel)
            self.state.clear_stale(agent, resolved, conn=conn)
            event = self.state.emit(
                "refresh",
                agent=agent,
                metadata={"paths": refreshed, "resolved": resolved},
                conn=conn,
            )
            return {"ok": True, "agent": agent, "refreshed": refreshed, "resolved": resolved, "event": event}

    def stale(self, agent: str) -> dict[str, Any]:
        self._validate_agent(agent)
        with self.state.connect() as conn:
            self._ensure_agent_exists(agent, conn=conn)
            rows = self.state.stale_for_agent(agent, conn=conn)
            return {"ok": True, "agent": agent, "stale": rows}

    def bash_begin(self, agent: str, command: str) -> dict[str, Any]:
        self._validate_agent(agent)
        with self.state._lock, self.state.connect() as conn:
            self._ensure_agent_exists(agent, conn=conn)
            if self.state.has_stale(agent, conn=conn):
                rows = self.state.stale_for_agent(agent, conn=conn)
                self.state.emit("bash_blocked", agent=agent, reason="unresolved_stale", metadata={"stale": rows}, conn=conn)
                conn.commit()
                raise Blocked(f"Agent {agent} has unresolved stale notices.")
            snapshot_id = uuid.uuid4().hex
            snap = snapshot(self.workspace(agent))
            conn.execute(
                """
                INSERT INTO bash_snapshots(snapshot_id, agent_id, command, snapshot_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (snapshot_id, agent, command, json.dumps(snap, sort_keys=True), self.state.now()),
            )
            event = self.state.emit(
                "bash_begin",
                agent=agent,
                metadata={"command": command, "snapshot_id": snapshot_id, "files": len(snap)},
                conn=conn,
            )
            return {"ok": True, "snapshot_id": snapshot_id, "event": event}

    def bash_end(self, agent: str, snapshot_id: str, exit_code: int = 0) -> dict[str, Any]:
        self._validate_agent(agent)
        with self.state._lock, self.state.connect() as conn:
            self._ensure_agent_exists(agent, conn=conn)
            row = conn.execute(
                "SELECT snapshot_json, command FROM bash_snapshots WHERE snapshot_id = ? AND agent_id = ?",
                (snapshot_id, agent),
            ).fetchone()
            if row is None:
                raise NotFound(f"Unknown bash snapshot: {snapshot_id}")
            before = json.loads(row["snapshot_json"])
            after = snapshot(self.workspace(agent))
            changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
            deleted = [path for path in changed if path not in after]

            if deleted:
                event = self.state.emit(
                    "observed_write_blocked",
                    agent=agent,
                    reason="delete_not_supported",
                    metadata={"paths": deleted, "snapshot_id": snapshot_id, "exit_code": exit_code},
                    conn=conn,
                )
                conn.execute("DELETE FROM bash_snapshots WHERE snapshot_id = ?", (snapshot_id,))
                return {"ok": False, "changed": changed, "reason": "delete_not_supported", "event": event}

            if self.state.has_stale(agent, conn=conn):
                rows = self.state.stale_for_agent(agent, conn=conn)
                event = self.state.emit(
                    "dirty_conflict",
                    agent=agent,
                    reason="unresolved_stale_after_bash",
                    metadata={"changed": changed, "stale": rows, "snapshot_id": snapshot_id},
                    conn=conn,
                )
                conn.execute("DELETE FROM bash_snapshots WHERE snapshot_id = ?", (snapshot_id,))
                return {"ok": False, "changed": changed, "reason": "unresolved_stale_after_bash", "event": event}

            conflicts = []
            for rel in changed:
                base = self._workspace_base_or_absent(agent, rel, conn=conn)
                head = self.state.get_head(rel, conn=conn)
                if base != head:
                    conflicts.append({"path": rel, "base": base, "head": head})
            if conflicts:
                event = self.state.emit(
                    "dirty_conflict",
                    agent=agent,
                    reason="workspace_base_mismatch",
                    metadata={"conflicts": conflicts, "snapshot_id": snapshot_id},
                    conn=conn,
                )
                conn.execute("DELETE FROM bash_snapshots WHERE snapshot_id = ?", (snapshot_id,))
                return {"ok": False, "changed": changed, "reason": "workspace_base_mismatch", "conflicts": conflicts, "event": event}

            committed = []
            stale_agents: dict[str, list[str]] = {}
            for rel in changed:
                ws_file = managed_target(self.workspace(agent), rel)
                if not ws_file.exists():
                    continue
                old = self.state.get_head(rel, conn=conn)
                copy_file(self.workspace(agent), self.store, rel)
                new = file_version(managed_target(self.store, rel))
                self.state.upsert_head(rel, new, conn=conn)
                self.state.set_workspace_base(agent, rel, new, conn=conn)
                self.state.set_read(agent, rel, new, conn=conn)
                self.state.emit(
                    "observed_write",
                    agent=agent,
                    path=rel,
                    old_version=old,
                    new_version=new,
                    metadata={"kind": "bash", "snapshot_id": snapshot_id},
                    conn=conn,
                )
                stale_agents[rel] = self.state.mark_stale_readers(
                    writer=agent,
                    path=rel,
                    old_version=old,
                    new_version=new,
                    conn=conn,
                )
                committed.append({"path": rel, "old_version": old, "new_version": new})
            conn.execute("DELETE FROM bash_snapshots WHERE snapshot_id = ?", (snapshot_id,))
            event = self.state.emit(
                "bash_end",
                agent=agent,
                reason="ok",
                metadata={"snapshot_id": snapshot_id, "exit_code": exit_code, "changed": changed},
                conn=conn,
            )
            return {"ok": True, "changed": changed, "committed": committed, "stale_agents": stale_agents, "event": event}

    def events(self, after: int = 0) -> list[dict[str, Any]]:
        return self.state.events_after(after)

    def _ensure_can_write(self, agent: str, rel: str, *, conn) -> None:
        if self.state.has_stale(agent, conn=conn):
            rows = self.state.stale_for_agent(agent, conn=conn)
            self.state.emit(
                "write_blocked",
                agent=agent,
                path=rel,
                reason="unresolved_stale",
                metadata={"stale": rows},
                conn=conn,
            )
            conn.commit()
            raise Blocked(f"Agent {agent} has unresolved stale notices.")
        base = self._workspace_base_or_absent(agent, rel, conn=conn)
        head = self.state.get_head(rel, conn=conn)
        if base != head:
            self.state.emit(
                "write_blocked",
                agent=agent,
                path=rel,
                reason="workspace_base_mismatch",
                metadata={"base": base, "head": head},
                conn=conn,
            )
            conn.commit()
            raise Conflict(f"Workspace base mismatch for {rel}: base={base} head={head}")

    def _ensure_agent_exists(self, agent: str, *, conn) -> None:
        if not self.state.agent_exists(agent, conn=conn):
            raise NotFound(f"Unknown agent: {agent}. Run `mesi agent create {agent}` first.")

    def _workspace_base_or_absent(self, agent: str, rel: str, *, conn) -> str:
        base = self.state.get_workspace_base(agent, rel, conn=conn)
        if base is not None:
            return base
        head = self.state.get_head(rel, conn=conn)
        if head == ABSENT_VERSION:
            self.state.set_workspace_base(agent, rel, ABSENT_VERSION, conn=conn)
            return ABSENT_VERSION
        return ABSENT_VERSION

    def _resolve_if_current(self, agent: str, rel: str, version: str, *, conn) -> None:
        head = self.state.get_head(rel, conn=conn)
        if head == version:
            had_stale = any(row["path"] == rel for row in self.state.stale_for_agent(agent, conn=conn))
            self.state.clear_stale(agent, [rel], conn=conn)
            if had_stale:
                self.state.emit("stale_resolved", agent=agent, path=rel, new_version=version, conn=conn)

    @staticmethod
    def _validate_agent(agent: str) -> None:
        if not agent or any(ch in agent for ch in "/\\\0"):
            raise Unsupported(f"Invalid agent id: {agent!r}")
