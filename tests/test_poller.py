from unittest.mock import MagicMock

from app.devin_client import DevinAPIError
from app.poller import poll_once

REPO = "fake-org/fake-repo"


def finished_session(session_id="s1", pr_url="https://github.com/fake-org/fake-repo/pull/9"):
    return {
        "session_id": session_id,
        "url": f"https://app.devin.ai/sessions/{session_id}",
        "status": "finished",
        "status_enum": "finished",
        "pull_requests": [{"url": pr_url, "state": "open"}],
        "acus_consumed": 4.25,
    }


def test_transitions_to_terminal_and_comments_once(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", issue_title="T", status="running", status_enum="running")
    devin = MagicMock()
    devin.get_session.return_value = finished_session()
    github = MagicMock()

    assert poll_once(database, devin, github, REPO) == 1

    row = database.get_session(42, "s1")
    assert row["status_enum"] == "finished"
    assert row["pr_url"] == "https://github.com/fake-org/fake-repo/pull/9"
    assert row["pr_state"] == "open"
    assert row["acus_consumed"] == 4.25
    assert row["completed_at"]
    github.post_comment.assert_called_once()
    assert row["pr_url"] in github.post_comment.call_args[0][2]

    # Second cycle: row is terminal, so it is not polled and no second comment happens.
    assert poll_once(database, devin, github, REPO) == 0
    assert devin.get_session.call_count == 1
    assert github.post_comment.call_count == 1


def test_no_comment_when_no_pr_yet(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", status="running", status_enum="running")
    devin = MagicMock()
    devin.get_session.return_value = {
        "session_id": "s1", "status": "running", "status_enum": "running",
        "pull_requests": [], "acus_consumed": 1.0,
    }
    github = MagicMock()
    poll_once(database, devin, github, REPO)
    github.post_comment.assert_not_called()
    assert database.get_session(42, "s1")["status_enum"] == "running"


def test_devin_error_leaves_row_untouched(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", status="running", status_enum="running")
    devin = MagicMock()
    devin.get_session.side_effect = DevinAPIError("boom")
    github = MagicMock()
    assert poll_once(database, devin, github, REPO) == 0
    assert database.get_session(42, "s1")["status_enum"] == "running"
    github.post_comment.assert_not_called()
