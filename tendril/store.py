"""SQLite persistence for goals, their event log, approvals, memory notes and notifications."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',  -- active | running | idle | paused | done
    interval_s  INTEGER,
    next_run_at REAL,
    run_count   INTEGER NOT NULL DEFAULT 0,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    goal_id  INTEGER NOT NULL,
    ts       REAL NOT NULL,
    kind     TEXT NOT NULL,  -- run | user | agent | tool_call | tool_result | approval | notify | error
    content  TEXT NOT NULL,
    data     TEXT
);
CREATE INDEX IF NOT EXISTS events_goal ON events(goal_id, id);
CREATE TABLE IF NOT EXISTS approvals (
    id       INTEGER PRIMARY KEY,
    goal_id  INTEGER NOT NULL,
    ts       REAL NOT NULL,
    tool     TEXT NOT NULL,
    args     TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | executed | denied
    result   TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id       INTEGER PRIMARY KEY,
    goal_id  INTEGER,  -- NULL = shared across goals
    ts       REAL NOT NULL,
    content  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id       INTEGER PRIMARY KEY,
    goal_id  INTEGER,
    ts       REAL NOT NULL,
    message  TEXT NOT NULL,
    read     INTEGER NOT NULL DEFAULT 0
);
"""

GOAL_FIELDS = {"text", "status", "interval_s", "next_run_at", "run_count", "fail_count", "last_error"}


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            return self.db.execute(sql, args).lastrowid

    # --- goals ---------------------------------------------------------------

    def create_goal(self, text: str, interval_s: int | None = None) -> int:
        now = time.time()
        return self._exec(
            "INSERT INTO goals (text, interval_s, next_run_at, created_at, updated_at) VALUES (?,?,?,?,?)",
            (text, interval_s, now, now, now),
        )

    def get_goal(self, goal_id: int) -> dict | None:
        return self._one("SELECT * FROM goals WHERE id=?", (goal_id,))

    def list_goals(self) -> list[dict]:
        return self._all(
            "SELECT g.*, (SELECT COUNT(*) FROM approvals a WHERE a.goal_id=g.id AND a.status='pending')"
            " AS pending_approvals FROM goals g ORDER BY g.id DESC"
        )

    def update_goal(self, goal_id: int, **fields: Any) -> None:
        unknown = set(fields) - GOAL_FIELDS
        if unknown:
            raise ValueError(f"unknown goal fields: {unknown}")
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE goals SET {cols} WHERE id=?", (*fields.values(), goal_id))

    def delete_goal(self, goal_id: int) -> None:
        with self.lock:
            for table in ("events", "approvals", "notes", "notifications"):
                self.db.execute(f"DELETE FROM {table} WHERE goal_id=?", (goal_id,))
            self.db.execute("DELETE FROM goals WHERE id=?", (goal_id,))

    def due_goals(self, now: float) -> list[dict]:
        return self._all(
            "SELECT * FROM goals WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at<=?"
            " ORDER BY next_run_at",
            (now,),
        )

    def reset_running(self) -> None:
        """After a crash/restart, goals stuck in 'running' get re-run."""
        self._exec("UPDATE goals SET status='active', next_run_at=? WHERE status='running'", (time.time(),))

    def wake(self, goal_id: int, reopen: bool = False) -> bool:
        """Ask for the goal to run as soon as possible. Returns False if it can't be woken."""
        goal = self.get_goal(goal_id)
        if not goal or goal["status"] == "paused" or (goal["status"] == "done" and not reopen):
            return False
        status = "running" if goal["status"] == "running" else "active"
        self.update_goal(goal_id, status=status, next_run_at=time.time())
        return True

    # --- events --------------------------------------------------------------

    def add_event(self, goal_id: int, kind: str, content: str, data: Any = None) -> int:
        return self._exec(
            "INSERT INTO events (goal_id, ts, kind, content, data) VALUES (?,?,?,?,?)",
            (goal_id, time.time(), kind, content, json.dumps(data) if data is not None else None),
        )

    def events(self, goal_id: int, limit: int = 200) -> list[dict]:
        rows = self._all("SELECT * FROM events WHERE goal_id=? ORDER BY id DESC LIMIT ?", (goal_id, limit))
        return rows[::-1]

    # --- approvals -----------------------------------------------------------

    def add_approval(self, goal_id: int, tool: str, args: dict) -> int:
        return self._exec(
            "INSERT INTO approvals (goal_id, ts, tool, args) VALUES (?,?,?,?)",
            (goal_id, time.time(), tool, json.dumps(args, ensure_ascii=False)),
        )

    def get_approval(self, approval_id: int) -> dict | None:
        return self._one("SELECT * FROM approvals WHERE id=?", (approval_id,))

    def list_approvals(self, status: str | None = None, goal_id: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM approvals WHERE 1=1", []
        if status:
            sql, args = sql + " AND status=?", [*args, status]
        if goal_id is not None:
            sql, args = sql + " AND goal_id=?", [*args, goal_id]
        return self._all(sql + " ORDER BY id DESC", tuple(args))

    def claim_approval(self, approval_id: int, status: str) -> bool:
        """Atomically move a pending approval to `status`; False if it wasn't pending."""
        with self.lock:
            cur = self.db.execute(
                "UPDATE approvals SET status=? WHERE id=? AND status='pending'", (status, approval_id)
            )
            return cur.rowcount == 1

    def finish_approval(self, approval_id: int, result: str) -> None:
        self._exec("UPDATE approvals SET status='executed', result=? WHERE id=?", (result, approval_id))

    # --- memory --------------------------------------------------------------

    def add_note(self, content: str, goal_id: int | None) -> int:
        return self._exec("INSERT INTO notes (goal_id, ts, content) VALUES (?,?,?)", (goal_id, time.time(), content))

    def notes(self, goal_id: int | None, limit: int = 30) -> list[dict]:
        if goal_id is None:
            sql, args = "SELECT * FROM notes WHERE goal_id IS NULL", ()
        else:
            sql, args = "SELECT * FROM notes WHERE goal_id=?", (goal_id,)
        return self._all(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))[::-1]

    def search_notes(self, query: str, limit: int = 20) -> list[dict]:
        return self._all(
            "SELECT * FROM notes WHERE content LIKE ? ORDER BY id DESC LIMIT ?", (f"%{query}%", limit)
        )

    # --- notifications -------------------------------------------------------

    def notify(self, goal_id: int | None, message: str) -> int:
        return self._exec(
            "INSERT INTO notifications (goal_id, ts, message) VALUES (?,?,?)", (goal_id, time.time(), message)
        )

    def notifications(self, unread_only: bool = False, limit: int = 100) -> list[dict]:
        where = "WHERE read=0" if unread_only else ""
        return self._all(f"SELECT * FROM notifications {where} ORDER BY id DESC LIMIT ?", (limit,))

    def mark_read(self) -> None:
        self._exec("UPDATE notifications SET read=1 WHERE read=0")
