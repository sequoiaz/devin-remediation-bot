from unittest.mock import MagicMock

from app.devin_client import DevinAPIError
from app.poller import extract_pr, poll_once

REPO = "fake-org/fake-repo"


def finished_session(session_id="s1", pr_url="https://github.com/fake-org/fake-repo/pull/9"):
    """A v3 session whose agent reported the task complete."""
    return {
        "session_id": session_id,
        "url": f"https://app.devin.ai/sessions/{session_id}",
        "status": "running",
        "status_detail": "finished",
        "pull_requests": [{"pr_url": pr_url, "pr_state": "open"}],
        "acus_consumed": 4.25,
    }


def test_extract_pr_reads_v3_field_names():
    session = {"pull_requests": [{"pr_url": "https://example.com/pull/1", "pr_state": "open"}]}
    assert extract_pr(session) == {"url": "https://example.com/pull/1", "state": "open"}
    assert extract_pr({"pull_requests": []}) is None
    assert extract_pr({}) is None


def test_extract_pr_prefers_the_target_repo():
    session = {
        "pull_requests": [
            {"pr_url": "https://github.com/other-org/tooling/pull/2", "pr_state": "merged"},
            {"pr_url": f"https://github.com/{REPO}/pull/9", "pr_state": "open"},
        ]
    }
    assert extract_pr(session, REPO) == {
        "url": f"https://github.com/{REPO}/pull/9",
        "state": "open",
    }
    # No match for the target repo falls back to the first pull request.
    assert extract_pr(session, "unrelated/repo")["url"].endswith("/tooling/pull/2")


def test_transitions_to_terminal_and_comments_once(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", issue_title="T", status="running")
    devin = MagicMock()
    devin.get_session.return_value = finished_session()
    github = MagicMock()

    assert poll_once(database, devin, github, REPO) == 1

    row = database.get_session(42, "s1")
    assert row["status"] == "running"
    assert row["status_detail"] == "finished"
    assert row["pr_url"] == "https://github.com/fake-org/fake-repo/pull/9"
    assert row["pr_state"] == "open"
    assert row["acus_consumed"] == 4.25
    assert row["completed_at"]
    github.post_comment.assert_called_once()
    assert row["pr_url"] in github.post_comment.call_args[0][2]

    # Second cycle: row is done, so it is not polled and no second comment happens.
    assert poll_once(database, devin, github, REPO) == 0
    assert devin.get_session.call_count == 1
    assert github.post_comment.call_count == 1


def test_exited_session_is_terminal(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", status="running")
    devin = MagicMock()
    devin.get_session.return_value = {
        "session_id": "s1", "status": "exit", "status_detail": None,
        "pull_requests": [], "acus_consumed": 2.0,
    }
    poll_once(database, devin, MagicMock(), REPO)

    assert database.get_session(42, "s1")["completed_at"]
    assert database.list_non_terminal_sessions() == []


def test_session_waiting_for_user_keeps_being_polled(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", status="running")
    devin = MagicMock()
    devin.get_session.return_value = {
        "session_id": "s1", "status": "running", "status_detail": "waiting_for_user",
        "pull_requests": [], "acus_consumed": 1.0,
    }
    github = MagicMock()
    poll_once(database, devin, github, REPO)

    row = database.get_session(42, "s1")
    assert row["status_detail"] == "waiting_for_user"
    assert not row["completed_at"]
    github.post_comment.assert_not_called()
    assert poll_once(database, devin, github, REPO) == 1


def test_finished_without_a_pr_is_polled_until_the_pr_appears(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", status="running")
    devin = MagicMock()
    devin.get_session.return_value = {
        "session_id": "s1", "status": "running", "status_detail": "finished",
        "pull_requests": [], "acus_consumed": 3.0,
    }
    github = MagicMock()
    assert poll_once(database, devin, github, REPO) == 1
    assert not database.get_session(42, "s1")["completed_at"]

    devin.get_session.return_value = finished_session()
    assert poll_once(database, devin, github, REPO) == 1
    row = database.get_session(42, "s1")
    assert row["pr_url"] == "https://github.com/fake-org/fake-repo/pull/9"
    assert row["completed_at"]
    github.post_comment.assert_called_once()


def test_no_comment_when_no_pr_yet(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", status="running")
    devin = MagicMock()
    devin.get_session.return_value = {
        "session_id": "s1", "status": "running", "status_detail": "working",
        "pull_requests": [], "acus_consumed": 1.0,
    }
    github = MagicMock()
    poll_once(database, devin, github, REPO)
    github.post_comment.assert_not_called()
    assert database.get_session(42, "s1")["status"] == "running"


def test_devin_error_leaves_row_untouched(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", status="running")
    devin = MagicMock()
    devin.get_session.side_effect = DevinAPIError("boom")
    github = MagicMock()
    assert poll_once(database, devin, github, REPO) == 0
    row = database.get_session(42, "s1")
    assert row["status"] == "running"
    assert row["last_poll_error"] == "boom"
    github.post_comment.assert_not_called()

    devin.get_session.side_effect = None
    devin.get_session.return_value = finished_session()
    poll_once(database, devin, github, REPO)
    assert database.get_session(42, "s1")["last_poll_error"] is None
