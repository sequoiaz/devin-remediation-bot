import pytest
from unittest.mock import MagicMock, patch

from app.github_client import GitHubAPIError, GitHubClient


def response(status_code, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.text = "error body"
    return resp


def test_get_issue_calls_github():
    client = GitHubClient(token="t", backoff_base=0)
    with patch("app.github_client.requests.request", return_value=response(200, {"number": 7})) as req:
        assert client.get_issue("fake-org/fake-repo", 7)["number"] == 7
    assert req.call_args[0] == ("GET", "https://api.github.com/repos/fake-org/fake-repo/issues/7")


def test_post_comment_calls_github():
    client = GitHubClient(token="t", backoff_base=0)
    with patch("app.github_client.requests.request", return_value=response(201, {"id": 1})) as req:
        client.post_comment("fake-org/fake-repo", 7, "hello")
    assert req.call_args[0] == (
        "POST", "https://api.github.com/repos/fake-org/fake-repo/issues/7/comments"
    )
    assert req.call_args[1]["json"] == {"body": "hello"}


def test_retries_then_raises_custom_error():
    client = GitHubClient(token="t", backoff_base=0)
    with patch("app.github_client.requests.request", return_value=response(500)) as req:
        with pytest.raises(GitHubAPIError):
            client.get_issue("fake-org/fake-repo", 7)
    assert req.call_count == 3


def test_dry_run_makes_no_network_call():
    client = GitHubClient(token="t", dry_run=True)
    with patch("app.github_client.requests.request") as req:
        issue = client.get_issue("fake-org/fake-repo", 3)
        client.post_comment("fake-org/fake-repo", 3, "hi")
    req.assert_not_called()
    assert issue["number"] == 3
    assert issue["title"]


def test_get_pr_state_reports_merged_separately_from_closed():
    client = GitHubClient(token="t")
    with patch.object(
        client, "_request", return_value={"state": "closed", "merged": True}
    ):
        assert client.get_pr_state("https://github.com/o/r/pull/7") == "merged"
    with patch.object(
        client, "_request", return_value={"state": "closed", "merged": False}
    ):
        assert client.get_pr_state("https://github.com/o/r/pull/7") == "closed"


def test_get_pr_state_ignores_a_url_that_is_not_a_pull_request():
    client = GitHubClient(token="t")
    assert client.get_pr_state("https://example.com/whatever") is None
