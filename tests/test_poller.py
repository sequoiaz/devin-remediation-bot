import logging
from unittest.mock import MagicMock

from app.devin_client import DevinAPIError
from app.github_client import GitHubAPIError
from app.poller import extract_pr, poll_once, resolve_pr

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


def test_resolve_pr_follows_a_session_that_delegated_to_a_child():
    """A session that spawns children keeps no pull request of its own."""
    parent = {"session_id": "p", "pull_requests": [], "child_session_ids": ["c"]}
    devin = MagicMock()
    devin.get_session.return_value = finished_session("c")
    assert resolve_pr(devin, parent, REPO)["url"] == f"https://github.com/{REPO}/pull/9"
    devin.get_session.assert_called_once_with("c")


def test_resolve_pr_prefers_the_parents_own_pull_request():
    devin = MagicMock()
    parent = dict(finished_session("p"), child_session_ids=["c"])
    assert resolve_pr(devin, parent, REPO)["url"] == f"https://github.com/{REPO}/pull/9"
    devin.get_session.assert_not_called()


def test_resolve_pr_survives_an_unreadable_child():
    parent = {"session_id": "p", "pull_requests": [], "child_session_ids": ["c", "d"]}
    devin = MagicMock()
    devin.get_session.side_effect = [
        DevinAPIError("gone"),
        finished_session("d"),
    ]
    assert resolve_pr(devin, parent, REPO)["url"] == f"https://github.com/{REPO}/pull/9"


def test_transitions_to_terminal_and_comments_once(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", issue_title="T", status="running")
    devin = MagicMock()
    devin.get_session.return_value = finished_session()
    github = MagicMock()

    assert poll_once(database, devin, github, REPO).updated == 1

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
    assert poll_once(database, devin, github, REPO).updated == 0
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
    assert poll_once(database, devin, github, REPO).updated == 1


def test_finished_without_a_pr_is_polled_until_the_pr_appears(database):
    database.upsert_session(issue_number=42, devin_session_id="s1", status="running")
    devin = MagicMock()
    devin.get_session.return_value = {
        "session_id": "s1", "status": "running", "status_detail": "finished",
        "pull_requests": [], "acus_consumed": 3.0,
    }
    github = MagicMock()
    assert poll_once(database, devin, github, REPO).updated == 1
    assert not database.get_session(42, "s1")["completed_at"]

    devin.get_session.return_value = finished_session()
    assert poll_once(database, devin, github, REPO).updated == 1
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
    assert poll_once(database, devin, github, REPO).updated == 0
    row = database.get_session(42, "s1")
    assert row["status"] == "running"
    assert row["last_poll_error"] == "boom"
    github.post_comment.assert_not_called()

    devin.get_session.side_effect = None
    devin.get_session.return_value = finished_session()
    poll_once(database, devin, github, REPO)
    assert database.get_session(42, "s1")["last_poll_error"] is None


def test_unchanged_sessions_do_not_log_every_cycle(database, caplog):
    """The poller runs every 20s; a static session should not narrate each pass."""
    database.upsert_session(
        issue_number=42, devin_session_id="s1", status="suspended",
        status_detail="inactivity",
    )
    devin = MagicMock()
    devin.get_session.return_value = {
        "session_id": "s1", "status": "suspended", "status_detail": "inactivity",
        "pull_requests": [], "acus_consumed": 0.0,
    }
    with caplog.at_level(logging.INFO, logger="app.poller"):
        poll_once(database, devin, MagicMock(), REPO)
    assert caplog.records == []


def test_completion_is_announced_when_only_the_pr_appears(database, caplog):
    """The row becomes done via its PR, with status and detail both unchanged."""
    database.upsert_session(
        issue_number=42, devin_session_id="s1", status="running",
        status_detail="finished",
    )
    devin = MagicMock()
    devin.get_session.return_value = finished_session()
    with caplog.at_level(logging.INFO, logger="app.poller"):
        poll_once(database, devin, MagicMock(), REPO)
    assert any("is done" in record.getMessage() for record in caplog.records)


def test_simulated_sessions_are_never_polled_against_the_api(database):
    """DRY_RUN rows outlive the dry run and would 403 forever against the API."""
    database.upsert_session(
        issue_number=42, devin_session_id="devin-dryrun-abc123", status="running"
    )
    devin = MagicMock()
    devin.dry_run = False
    report = poll_once(database, devin, MagicMock(), REPO)
    devin.get_session.assert_not_called()
    assert report.errors == []
    assert database.get_session(42, "devin-dryrun-abc123")["last_poll_error"] is None


def test_forced_poll_does_not_stick_an_error_on_a_done_row(database):
    """Nothing polls a done row again, so its error would never be cleared."""
    database.upsert_session(issue_number=42, devin_session_id="s1", status="exit")
    devin = MagicMock()
    devin.get_session.side_effect = DevinAPIError("session no longer exists")
    report = poll_once(database, devin, MagicMock(), REPO, force=True)
    assert report.updated == 0
    assert database.get_session(42, "s1")["last_poll_error"] is None
    # The caller still learns about it, so a forced refresh is never silent.
    assert report.errors == [
        {
            "issue_number": 42,
            "devin_session_id": "s1",
            "error": "session no longer exists",
        }
    ]


PR = "https://github.com/fake-org/fake-repo/pull/9"
PR_OPENED_AT = "2026-08-09T06:30:00+00:00"


def github_with_pr(state="open", created_at=PR_OPENED_AT):
    github = MagicMock()
    github.get_pr.return_value = {"state": state, "created_at": created_at}
    return github


def test_pr_state_is_refreshed_after_the_pull_request_is_merged(database):
    """A done row is never polled again, so its PR state has to come from GitHub."""
    database.upsert_session(
        issue_number=42, devin_session_id="s1", status="exit", pr_url=PR,
        pr_state="open",
    )
    github = github_with_pr("merged")
    poll_once(database, MagicMock(), github, REPO)
    github.get_pr.assert_called_once_with(PR)
    row = database.get_session(42, "s1")
    assert (row["pr_state"], row["pr_created_at"]) == ("merged", PR_OPENED_AT)


def test_a_settled_pr_is_not_queried_again(database):
    database.upsert_session(
        issue_number=42, devin_session_id="s1", status="exit", pr_url=PR,
        pr_state="merged",
    )
    database.record_pr(42, "s1", pr_created_at=PR_OPENED_AT)
    github = MagicMock()
    poll_once(database, MagicMock(), github, REPO)
    github.get_pr.assert_not_called()


def test_an_unreadable_pr_state_is_reported_and_leaves_the_row_alone(database):
    database.upsert_session(
        issue_number=42, devin_session_id="s1", status="exit", pr_url=PR,
        pr_state="open",
    )
    github = MagicMock()
    github.get_pr.side_effect = GitHubAPIError("HTTP 404")
    report = poll_once(database, MagicMock(), github, REPO)
    assert report.errors == [
        {"issue_number": 42, "devin_session_id": "s1", "error": "HTTP 404"}
    ]
    assert database.get_session(42, "s1")["pr_state"] == "open"


def test_refreshing_the_pr_state_keeps_the_row_done(database):
    """`upsert_session` clears status_detail, which would un-finish the row."""
    database.upsert_session(
        issue_number=42, devin_session_id="s1", status="running",
        status_detail="finished", pr_url=PR, pr_state="open",
    )
    devin = MagicMock()
    devin.get_session.return_value = finished_session()
    poll_once(database, devin, github_with_pr("merged"), REPO)

    row = database.get_session(42, "s1")
    assert (row["status_detail"], row["pr_state"]) == ("finished", "merged")
    # Still done, so the next cycle leaves it alone instead of re-polling forever.
    assert database.list_non_terminal_sessions() == []


def test_a_session_cannot_reopen_a_merged_pull_request(database):
    """Devin keeps reporting `open`; GitHub is the authority once the PR settles."""
    database.upsert_session(
        issue_number=42, devin_session_id="s1", status="running", pr_url=PR,
        pr_state="merged",
    )
    devin = MagicMock()
    devin.get_session.return_value = finished_session()
    poll_once(database, devin, MagicMock(), REPO)
    assert database.get_session(42, "s1")["pr_state"] == "merged"
