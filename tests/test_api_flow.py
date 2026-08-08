import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from app.db import get_db
from tests.conftest import TARGET_REPO, WEBHOOK_SECRET


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def fake_created_session(session_id="sess-1"):
    return {
        "session_id": session_id,
        "url": f"https://app.devin.ai/sessions/{session_id}",
        "status": "running",
        "status_enum": "running",
        "pull_requests": [],
        "acus_consumed": 0.0,
    }


def test_simulate_creates_row(client, env, sample_issue):
    devin = MagicMock()
    devin.create_session.return_value = fake_created_session()
    github = MagicMock()
    github.get_issue.return_value = sample_issue

    with patch("app.main.get_devin_client", return_value=devin), patch(
        "app.main.get_github_client", return_value=github
    ):
        response = client.post("/simulate", json={"issue_number": 42})

    assert response.status_code == 200
    assert response.json()["devin_session_id"] == "sess-1"
    github.get_issue.assert_called_once_with(TARGET_REPO, 42)
    devin.create_session.assert_called_once_with(sample_issue)

    rows = get_db(env).list_sessions()
    assert len(rows) == 1
    assert rows[0]["issue_number"] == 42
    assert rows[0]["issue_title"] == sample_issue["title"]


def test_duplicate_webhook_delivery_does_not_duplicate_row(client, env, sample_issue):
    devin = MagicMock()
    devin.create_session.return_value = fake_created_session()
    github = MagicMock()

    body = json.dumps(
        {"action": "labeled", "label": {"name": "devin-remediate"}, "issue": sample_issue}
    ).encode()
    headers = {"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}

    with patch("app.main.get_devin_client", return_value=devin), patch(
        "app.main.get_github_client", return_value=github
    ):
        assert client.post("/webhook/github", content=body, headers=headers).status_code == 200
        assert client.post("/webhook/github", content=body, headers=headers).status_code == 200

    # The replay is short-circuited before Devin is called, so no second session
    # is started and no duplicate row appears.
    assert devin.create_session.call_count == 1
    rows = get_db(env).list_sessions()
    assert len(rows) == 1
    assert rows[0]["devin_session_id"] == "sess-1"


def test_replay_with_new_session_id_still_does_not_duplicate(client, env, sample_issue):
    """Even if the client mints a fresh session id per call, an in-flight session
    for the same issue blocks a second Devin session."""
    devin = MagicMock()
    devin.create_session.side_effect = [
        fake_created_session("sess-a"),
        fake_created_session("sess-b"),
    ]
    github = MagicMock()

    body = json.dumps(
        {"action": "labeled", "label": {"name": "devin-remediate"}, "issue": sample_issue}
    ).encode()
    headers = {"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}

    with patch("app.main.get_devin_client", return_value=devin), patch(
        "app.main.get_github_client", return_value=github
    ):
        for _ in range(4):
            client.post("/webhook/github", content=body, headers=headers)

    assert devin.create_session.call_count == 1
    assert len(get_db(env).list_sessions()) == 1
    assert github.post_comment.call_count == 1


def test_new_session_created_once_previous_one_is_terminal(client, env, sample_issue):
    db = get_db(env)
    db.upsert_session(
        issue_number=sample_issue["number"], devin_session_id="old",
        status="finished", status_enum="finished",
    )
    devin = MagicMock()
    devin.create_session.return_value = fake_created_session("sess-new")
    github = MagicMock()
    github.get_issue.return_value = sample_issue

    with patch("app.main.get_devin_client", return_value=devin), patch(
        "app.main.get_github_client", return_value=github
    ):
        response = client.post("/simulate", json={"issue_number": sample_issue["number"]})

    assert response.json()["devin_session_id"] == "sess-new"
    assert len(db.list_sessions()) == 2


def test_simulate_rejects_bad_input(client, env):
    devin = MagicMock()
    github = MagicMock()
    with patch("app.main.get_devin_client", return_value=devin), patch(
        "app.main.get_github_client", return_value=github
    ):
        assert client.post("/simulate", json={}).status_code == 400
        non_integer = client.post("/simulate", json={"issue_number": "garbage"})
        assert non_integer.status_code == 400
        assert "integer" in non_integer.json()["detail"]
        malformed = client.post(
            "/simulate",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert malformed.status_code == 400
        assert client.post("/simulate", json=[1, 2]).status_code == 400

    github.get_issue.assert_not_called()
    devin.create_session.assert_not_called()
    assert get_db(env).list_sessions() == []


def test_simulate_accepts_numeric_string_issue_number(client, env, sample_issue):
    devin = MagicMock()
    devin.create_session.return_value = fake_created_session()
    github = MagicMock()
    github.get_issue.return_value = sample_issue
    with patch("app.main.get_devin_client", return_value=devin), patch(
        "app.main.get_github_client", return_value=github
    ):
        assert client.post("/simulate", json={"issue_number": "42"}).status_code == 200
    github.get_issue.assert_called_once_with(TARGET_REPO, 42)


def test_metrics_and_dashboard_shape(client, env):
    db = get_db(env)
    db.upsert_session(
        issue_number=1, devin_session_id="s1", issue_title="Fix the parser",
        devin_session_url="https://app.devin.ai/sessions/s1",
        status="finished", status_enum="finished",
        pr_url="https://github.com/fake-org/fake-repo/pull/9", pr_state="open",
        acus_consumed=3.5,
    )
    db.upsert_session(
        issue_number=2, devin_session_id="s2", issue_title="Blocked one",
        status="blocked", status_enum="blocked", acus_consumed=1.5,
    )

    payload = client.get("/metrics").json()
    summary = payload["summary"]
    assert summary["total_triggered"] == 2
    assert summary["completed_with_pr"] == 1
    assert summary["failed_or_blocked"] == 1
    assert summary["success_rate_pct"] == 50.0
    assert summary["total_acus_consumed"] == 5.0
    assert summary["average_time_to_pr_seconds"] is not None
    assert {s["issue_number"] for s in payload["sessions"]} == {1, 2}
    assert all("time_to_pr_seconds" in s for s in payload["sessions"])

    html = client.get("/dashboard").text
    assert "Fix the parser" in html
    assert "Blocked one" in html
    assert "https://github.com/fake-org/fake-repo/pull/9" in html
    assert "Total triggered" in html
    assert "Success rate" in html
    assert "50.0%" in html
    assert "3.5" in html


def test_metrics_empty(client):
    summary = client.get("/metrics").json()["summary"]
    assert summary["total_triggered"] == 0
    assert summary["success_rate_pct"] == 0.0
    assert summary["average_time_to_pr_seconds"] is None
    assert "No sessions yet." in client.get("/dashboard").text
