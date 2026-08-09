import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Devin v3 `status` values: new, claimed, running, exit, error, suspended, resuming.
# Only `exit` and `error` end the session lifecycle; `suspended` can still resume.
TERMINAL_STATUSES = {"exit", "error"}

# A session reports `status_detail == "finished"` while `status` is still `running`
# once the agent considers the task complete, which is what the dashboard cares about.
# It can report that before the PR shows up in `pull_requests`, so a finished session
# only counts as done once its PR has been captured; otherwise it keeps being polled
# until the lifecycle ends and the PR link would never reach the dashboard.
FINISHED_DETAIL = "finished"

# `status_detail` values that mean the session cannot make progress on its own.
STALLED_DETAILS = {"waiting_for_user", "waiting_for_approval"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    issue_title TEXT,
    devin_session_id TEXT NOT NULL,
    devin_session_url TEXT,
    status TEXT,
    status_detail TEXT,
    pr_url TEXT,
    pr_state TEXT,
    acus_consumed REAL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    last_polled_at TEXT,
    last_poll_error TEXT,
    UNIQUE (issue_number, devin_session_id)
);
"""

# Rows that are done: the lifecycle ended, or the agent reported the task finished
# and we already know its pull request.
DONE_PREDICATE = (
    "(COALESCE(status, '') IN ({terminal})"
    " OR (COALESCE(status_detail, '') = ? AND COALESCE(pr_url, '') != ''))"
).format(terminal=", ".join("?" for _ in TERMINAL_STATUSES))
DONE_PARAMS = (*TERMINAL_STATUSES, FINISHED_DETAIL)

NEW_COLUMNS = {
    # Databases created before the v3 field names were fixed have the invented
    # `status_enum` column instead of `status_detail`.
    "status_detail": "TEXT",
    "last_polled_at": "TEXT",
    "last_poll_error": "TEXT",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_done(
    status: Optional[str],
    status_detail: Optional[str] = None,
    pr_url: Optional[str] = None,
) -> bool:
    if (status or "") in TERMINAL_STATUSES:
        return True
    return (status_detail or "") == FINISHED_DETAIL and bool(pr_url)


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
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(sessions)")
            }
            for name, column_type in NEW_COLUMNS.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE sessions ADD COLUMN {name} {column_type}"
                    )

    def upsert_session(
        self,
        issue_number: int,
        devin_session_id: str,
        issue_title: Optional[str] = None,
        devin_session_url: Optional[str] = None,
        status: Optional[str] = None,
        status_detail: Optional[str] = None,
        pr_url: Optional[str] = None,
        pr_state: Optional[str] = None,
        acus_consumed: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Insert a session row, or update it in place when the
        (issue_number, devin_session_id) pair already exists."""
        now = utcnow()
        completed_at = now if is_done(status, status_detail, pr_url) else None
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    issue_number, issue_title, devin_session_id, devin_session_url,
                    status, status_detail, pr_url, pr_state, acus_consumed,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (issue_number, devin_session_id) DO UPDATE SET
                    issue_title = COALESCE(excluded.issue_title, sessions.issue_title),
                    devin_session_url = COALESCE(excluded.devin_session_url, sessions.devin_session_url),
                    status = COALESCE(excluded.status, sessions.status),
                    status_detail = excluded.status_detail,
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
                    status_detail,
                    pr_url,
                    pr_state,
                    acus_consumed,
                    now,
                    now,
                    completed_at,
                ),
            )
        return self.get_session(issue_number, devin_session_id)

    def set_pr_state(
        self, issue_number: int, devin_session_id: str, pr_state: str
    ) -> None:
        """Update only the pull request state.

        `upsert_session` overwrites `status_detail` unconditionally, so a caller that
        knows nothing but the pull request cannot go through it.
        """
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET pr_state = ?, updated_at = ? "
                "WHERE issue_number = ? AND devin_session_id = ?",
                (pr_state, utcnow(), issue_number, devin_session_id),
            )

    def record_poll(
        self, issue_number: int, devin_session_id: str, error: Optional[str] = None
    ) -> None:
        """Stamp the outcome of a poll cycle so failures are visible on the dashboard."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_polled_at = ?, last_poll_error = ? "
                "WHERE issue_number = ? AND devin_session_id = ?",
                (utcnow(), error, issue_number, devin_session_id),
            )

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
        """Most recent still-in-flight session for an issue, if one exists."""
        query = (
            f"SELECT * FROM sessions WHERE issue_number = ? AND NOT {DONE_PREDICATE} "
            "ORDER BY datetime(created_at) DESC, id DESC LIMIT 1"
        )
        with self.connect() as conn:
            row = conn.execute(query, (issue_number, *DONE_PARAMS)).fetchone()
        return dict(row) if row else None

    def list_non_terminal_sessions(self) -> List[Dict[str, Any]]:
        query = f"SELECT * FROM sessions WHERE NOT {DONE_PREDICATE}"
        with self.connect() as conn:
            rows = conn.execute(query, DONE_PARAMS).fetchall()
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
