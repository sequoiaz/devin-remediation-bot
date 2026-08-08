def test_upsert_is_idempotent_on_issue_and_session(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", issue_title="T", status="running")
    database.upsert_session(issue_number=42, devin_session_id="s1", status="finished", status_enum="finished")
    rows = database.list_sessions()
    assert len(rows) == 1
    assert rows[0]["status_enum"] == "finished"
    assert rows[0]["issue_title"] == "T"
    assert rows[0]["completed_at"]


def test_distinct_sessions_create_distinct_rows(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", status="running")
    database.upsert_session(issue_number=42, devin_session_id="s2", status="running")
    assert len(database.list_sessions()) == 2


def test_non_terminal_listing(database):
    database.upsert_session(issue_number=1, devin_session_id="a", status="running", status_enum="running")
    database.upsert_session(issue_number=2, devin_session_id="b", status="finished", status_enum="finished")
    pending = database.list_non_terminal_sessions()
    assert [row["devin_session_id"] for row in pending] == ["a"]
