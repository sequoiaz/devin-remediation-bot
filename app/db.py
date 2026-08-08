import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

TERMINAL_STATUSES = {"finished", "blocked", "expired", "stopped", "failed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    issue_title TEXT,
    devin_session_id TEXT NOT NULL,
    devin_session_url TEXT,
    status TEXT,
    status_enum TEXT,
    pr_url TEXT,
    pr_state TEXT,
    acus_consumed REAL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (issue_number, devin_session_id)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def upsert_session(
        self,
        issue_number: int,
        devin_session_id: str,
        issue_title: Optional[str] = None,
        devin_session_url: Optional[str] = None,
        status: Optional[str] = None,
        status_enum: Optional[str] = None,
        pr_url: Optional[str] = None,
        pr_state: Optional[str] = None,
        acus_consumed: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Insert a session row, or update it in place when the
        (issue_number, devin_session_id) pair already exists."""
        now = utcnow()
        completed_at = now if (status_enum or status) in TERMINAL_STATUSES else None
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    issue_number, issue_title, devin_session_id, devin_session_url,
                    status, status_enum, pr_url, pr_state, acus_consumed,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (issue_number, devin_session_id) DO UPDATE SET
                    issue_title = COALESCE(excluded.issue_title, sessions.issue_title),
                    devin_session_url = COALESCE(excluded.devin_session_url, sessions.devin_session_url),
                    status = COALESCE(excluded.status, sessions.status),
                    status_enum = COALESCE(excluded.status_enum, sessions.status_enum),
                    pr_url = COALESCE(excluded.pr_url, sessions.pr_url),
                    pr_state = COALESCE(excluded.pr_state, sessions.pr_state),
                    acus_consumed = COALESCE(excluded.acus_consumed, sessions.acus_consumed),
                    updated_at = excluded.updated_at,
                    completed_at = COALESCE(sessions.completed_at, excluded.completed_at)
                """,
                (
                    issue_number,
                    issue_title,
                    devin_session_id,
                    devin_session_url,
                    status,
                    status_enum,
                    pr_url,
                    pr_state,
                    acus_consumed,
                    now,
                    now,
                    completed_at,
                ),
            )
        return self.get_session(issue_number, devin_session_id)

    def get_session(self, issue_number: int, devin_session_id: str) -> Dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE issue_number = ? AND devin_session_id = ?",
                (issue_number, devin_session_id),
            ).fetchone()
        return dict(row) if row else {}

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY datetime(created_at) DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_active_session_for_issue(self, issue_number: int) -> Optional[Dict[str, Any]]:
        """Most recent non-terminal session for an issue, if one is already running."""
        placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
        query = (
            "SELECT * FROM sessions WHERE issue_number = ? "
            f"AND COALESCE(status_enum, status, '') NOT IN ({placeholders}) "
            "ORDER BY datetime(created_at) DESC, id DESC LIMIT 1"
        )
        with self.connect() as conn:
            row = conn.execute(query, (issue_number, *TERMINAL_STATUSES)).fetchone()
        return dict(row) if row else None

    def list_non_terminal_sessions(self) -> List[Dict[str, Any]]:
        placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
        query = (
            "SELECT * FROM sessions WHERE COALESCE(status_enum, status, '') "
            f"NOT IN ({placeholders})"
        )
        with self.connect() as conn:
            rows = conn.execute(query, tuple(TERMINAL_STATUSES)).fetchall()
        return [dict(row) for row in rows]


_db: Optional[Database] = None


def get_db(path: Optional[str] = None) -> Database:
    global _db
    if path is not None:
        _db = Database(path)
    elif _db is None:
        _db = Database(os.getenv("DATABASE_PATH", "data/sessions.db"))
    return _db


def reset_db() -> None:
    global _db
    _db = None
