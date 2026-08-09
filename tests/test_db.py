import sqlite3

from app.db import Database


def test_upsert_is_idempotent_on_issue_and_session(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", issue_title="T", status="running")
    database.upsert_session(issue_number=42, devin_session_id="s1", status="exit")
    rows = database.list_sessions()
    assert len(rows) == 1
    assert rows[0]["status"] == "exit"
    assert rows[0]["issue_title"] == "T"
    assert rows[0]["completed_at"]


def test_distinct_sessions_create_distinct_rows(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", status="running")
    database.upsert_session(issue_number=42, devin_session_id="s2", status="running")
    assert len(database.list_sessions()) == 2


def test_active_session_lookup_for_issue(database):
    assert database.get_active_session_for_issue(42) is None
    database.upsert_session(issue_number=42, devin_session_id="a", status="running")
    assert database.get_active_session_for_issue(42)["devin_session_id"] == "a"
    database.upsert_session(issue_number=42, devin_session_id="a", status="exit")
    assert database.get_active_session_for_issue(42) is None


def test_finished_detail_completes_a_still_running_session(database):
    database.upsert_session(issue_number=42, devin_session_id="a", status="running")
    assert database.get_active_session_for_issue(42) is not None
    database.upsert_session(
        issue_number=42,
        devin_session_id="a",
        status="running",
        status_detail="finished",
        pr_url="https://github.com/o/r/pull/1",
    )
    assert database.get_active_session_for_issue(42) is None
    assert database.get_session(42, "a")["completed_at"]


def test_finished_detail_without_a_pr_keeps_being_polled(database):
    """A session can report `finished` before its pull request is published."""
    database.upsert_session(
        issue_number=42, devin_session_id="a", status="running", status_detail="finished"
    )
    assert [row["devin_session_id"] for row in database.list_non_terminal_sessions()] == ["a"]
    assert database.get_session(42, "a")["completed_at"] is None


def test_record_poll_stores_and_clears_the_error(database):
    database.upsert_session(issue_number=42, devin_session_id="a", status="running")
    database.record_poll(42, "a", error="HTTP 401")
    row = database.get_session(42, "a")
    assert row["last_poll_error"] == "HTTP 401"
    assert row["last_polled_at"]
    database.record_poll(42, "a")
    assert database.get_session(42, "a")["last_poll_error"] is None


def test_suspended_session_is_not_treated_as_done(database):
    database.upsert_session(issue_number=42, devin_session_id="a", status="suspended")
    assert database.get_active_session_for_issue(42)["devin_session_id"] == "a"


def test_status_detail_can_be_cleared_when_a_session_resumes(database):
    database.upsert_session(
        issue_number=42, devin_session_id="a", status="running", status_detail="waiting_for_user"
    )
    database.upsert_session(issue_number=42, devin_session_id="a", status="running")
    assert database.get_session(42, "a")["status_detail"] is None


def test_non_terminal_listing(database):
    database.upsert_session(issue_number=1, devin_session_id="a", status="running")
    database.upsert_session(issue_number=2, devin_session_id="b", status="exit")
    pending = database.list_non_terminal_sessions()
    assert [row["devin_session_id"] for row in pending] == ["a"]


def test_legacy_database_gains_status_detail_column(tmp_path):
    """A database written before the v3 field-name fix must keep working."""
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
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
            INSERT INTO sessions (issue_number, devin_session_id, status, status_enum,
                                  created_at, updated_at)
            VALUES (7, 'old', 'running', 'running', '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00');
            """
        )

    db = Database(path)
    row = db.get_session(7, "old")
    assert row["status_detail"] is None
    assert row["last_poll_error"] is None
    db.upsert_session(issue_number=7, devin_session_id="old", status="exit")
    assert db.get_session(7, "old")["completed_at"]
